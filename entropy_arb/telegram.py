"""Best-effort Telegram notifications, outside the order execution path."""
from __future__ import annotations

import asyncio
import contextlib
import logging

import aiohttp

from .config import TelegramConf

log = logging.getLogger("telegram")


def usd(value, decimals=2) -> str:
    return "—" if value is None else f"${value:,.{decimals}f}"


def trade_message(*, symbol, direction, state, buy_name, sell_name,
                  buy_status, sell_status, buy_fill, sell_fill, qty,
                  notional, profit, total_notional, total_profit, hedges,
                  balances, account_delta) -> str:
    lines = [
        f"交易通知 · {symbol}",
        f"状态：{state} · 方向：{direction}",
        f"买入：{buy_name} {buy_status}，成交 {buy_fill:.8g}",
        f"卖出：{sell_name} {sell_status}，成交 {sell_fill:.8g}",
        f"计划数量：{qty:.8g}",
        f"本次成交额（两边合计）：{usd(notional)} · 本次盈利：{usd(profit, 4)}",
        f"累计成交额（两边合计）：{usd(total_notional)} · 累计盈利：{usd(total_profit, 4)}",
        f"对冲笔数：{hedges}",
    ]
    for name, free, equity in balances:
        lines.append(f"{name}余额：可用 {usd(free)} · 权益 {usd(equity)}")
    lines.append(f"账户权益变化：{usd(account_delta, 4)}")
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, conf: TelegramConf) -> None:
        self.conf = conf
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task = None

    def start(self, session: aiohttp.ClientSession) -> None:
        if self.conf.enabled:
            self.task = asyncio.create_task(self._run(session), name="telegram")

    def enqueue(self, message: str) -> None:
        if self.conf.enabled:
            self.queue.put_nowait(message)

    async def _run(self, session: aiohttp.ClientSession) -> None:
        while True:
            message = await self.queue.get()
            try:
                async with session.post(
                        f"https://api.telegram.org/bot{self.conf.bot_token}/sendMessage",
                        json={"chat_id": self.conf.chat_id, "text": message},
                        timeout=aiohttp.ClientTimeout(total=5)) as response:
                    data = await response.json()
                    if response.status != 200 or not data.get("ok"):
                        log.warning("Telegram notification failed (HTTP %s, code %s)",
                                    response.status, data.get("error_code"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # aiohttp exception strings can contain the bot token in the URL.
                log.warning("Telegram notification failed (%s)", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        if self.task is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("Telegram shutdown flush timed out; notifications may be unsent")
        finally:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
