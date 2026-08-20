"""Shared binary frame parsing for the authorized game collector."""
from __future__ import annotations

import re
from typing import Any

WINDOW_RE = re.compile(rb"(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d).{0,12}(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)")
CODE_RE = re.compile(rb"\[([12])\]")
ROUND_RE = re.compile(rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def extract_candidate(payload: bytes, captured_at: str) -> dict[str, Any] | None:
    window = WINDOW_RE.search(payload)
    code = CODE_RE.search(payload)
    if not window or not code:
        return None
    start, end = (value.decode("ascii") for value in window.groups())
    rounds = [value.decode("ascii") for value in ROUND_RE.findall(payload)]
    return {"captured_at": captured_at, "start_time": start, "end_time": end, "result_code": int(code.group(1)), "round_hint": rounds[-1] if rounds else None, "source": "anlingshiapi.mangqu.xin/wss"}
