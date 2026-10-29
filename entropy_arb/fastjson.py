"""JSON decoding for the websocket hot path.

Uses orjson when it is installed (several times faster than the standard
library on order-book frames) and falls back to json otherwise. If orjson
rejects a frame the standard library would accept (for example an integer
wider than 64 bits), the frame is decoded with json instead, so switching
backends never turns a valid message into an error.

Only decoding goes through orjson: outgoing frames keep json.dumps, because
orjson.dumps returns bytes and websockets would send those as binary frames.
"""
from __future__ import annotations

import json

try:
    import orjson as _orjson
except ImportError:  # optional dependency
    _orjson = None

BACKEND = "orjson" if _orjson is not None else "json"

if _orjson is not None:
    _OrjsonError = _orjson.JSONDecodeError

    def loads(raw):
        try:
            return _orjson.loads(raw)
        except _OrjsonError:
            return json.loads(raw)
else:
    loads = json.loads
