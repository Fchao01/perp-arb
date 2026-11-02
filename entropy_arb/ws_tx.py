"""Send signed Lighter transactions over a persistent websocket.

execution.order_transport: ws signs each order locally and sends it as a
`jsonapi/sendtx` frame on an already-open connection, instead of an HTTPS
POST to /api/v1/sendTx. That skips the request/response framing of HTTP
and, because frames on one connection arrive in order, the nonce lock is
released as soon as the frame is written rather than after the reply.

The reply format of jsonapi/sendtx is not documented, so the reply is only
used to fail fast on a rejection; whether the order filled still comes from
the authenticated account_orders stream, exactly as on the HTTP path. The
first replies are logged verbatim so the format can be checked in
engine.log. When the connection is down, orders go over HTTP.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

log = logging.getLogger("ws_tx")

SAMPLE_REPLIES = 5          # raw replies logged at INFO after (re)start
_SUB_PREFIXES = ("update/", "subscribed/", "unsubscribed/")


class WsNotReady(RuntimeError):
    pass


def reply_id(msg: dict) -> Optional[str]:
    for d in (msg, msg.get("data") if isinstance(msg.get("data"), dict) else None):
        if d and d.get("id") not in (None, ""):
            return str(d["id"])
    return None


def looks_like_tx_reply(msg: dict) -> bool:
    t = str(msg.get("type") or "")
    if t in ("ping", "pong", "connected") or t.startswith(_SUB_PREFIXES):
        return False
    if "sendtx" in t:
        return True
    data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
    return any(k in msg or k in data for k in ("tx_hash", "error", "code"))


def reply_error(msg: dict) -> Optional[str]:
    """Error text for a rejected transaction, None when it was accepted."""
    data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
    err = msg.get("error", data.get("error"))
    text = None
    if err:
        if isinstance(err, dict):
            text = (f"code={err.get('code')} "
                    f"msg={err.get('message') or err.get('msg')}")
        else:
            text = str(err)
    else:
        for d in (msg, data):
            code = d.get("code")
            if code in (None, ""):
                continue
            try:
                ok = int(code) in (0, 200)
            except (TypeError, ValueError):
                ok = False
            if not ok:
                text = f"code={code} msg={d.get('message') or d.get('msg')}"
                break
    if text is None:
        return None
    low = text.lower()
    if "rate limit" in low or "429" in text or "too many" in low:
        text = "RATE_LIMITED: " + text
    return text


class WsTxSender:
    """One websocket used only for sending transactions."""

    def __init__(self, name: str, ws_url: str, connect=ws_connect) -> None:
        self.name = name
        self.ws_url = ws_url
        self._connect = connect
        self.ready = asyncio.Event()
        self._ws = None
        self._pending: "OrderedDict[str, asyncio.Future]" = OrderedDict()
        self._seq = 0
        self._prefix = f"ea{int(time.time()) % 100000}-"
        self._samples = 0

    def _fail_pending(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(WsNotReady(why))
        self._pending.clear()

    def on_message(self, msg: dict) -> None:
        rid = reply_id(msg)
        fut = self._pending.pop(rid, None) if rid is not None else None
        if fut is None:
            if not looks_like_tx_reply(msg) or not self._pending:
                return
            # no id echoed: replies come back in send order on one socket
            _, fut = self._pending.popitem(last=False)
        if self._samples < SAMPLE_REPLIES:
            self._samples += 1
            log.info("[%s] ws tx reply sample: %s", self.name,
                     json.dumps(msg)[:400])
        if not fut.done():
            fut.set_result(msg)

    async def send(self, tx_type: int, tx_info: str) -> asyncio.Future:
        """Write one sendtx frame; returns a future for its reply."""
        ws = self._ws
        if ws is None or not self.ready.is_set():
            raise WsNotReady("tx websocket not connected")
        self._seq += 1
        rid = f"{self._prefix}{self._seq}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        frame = json.dumps({"type": "jsonapi/sendtx",
                            "data": {"id": rid, "tx_type": tx_type,
                                     "tx_info": json.loads(tx_info)}})
        try:
            await ws.send(frame)
        except Exception:
            self._pending.pop(rid, None)
            raise
        return fut

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                async with self._connect(self.ws_url, open_timeout=10,
                                         ping_interval=15, ping_timeout=15) as ws:
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        elif t == "connected":
                            self._ws = ws
                            self._samples = 0
                            self.ready.set()
                            log.info("[%s] tx websocket connected (%s)",
                                     self.name, self.ws_url)
                        else:
                            self.on_message(msg)
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] tx websocket error: %s — orders go over "
                            "HTTP, reconnect in %.0fs", self.name, e, backoff)
            finally:
                self.ready.clear()
                self._ws = None
                self._fail_pending("tx websocket closed")
            if stop.is_set():
                break
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
