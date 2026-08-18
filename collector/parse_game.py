"""Extract the game's minute-window result candidates from binary WebSocket logs.

The protocol is protobuf-like and has no public schema. This parser deliberately
keeps the numeric result code (1/2) until it is confirmed against the UI.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

WINDOW_RE = re.compile(rb"(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d).{0,12}(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)")
CODE_RE = re.compile(rb"\[([12])\]")
ROUND_RE = re.compile(rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def parse(path: Path):
    seen: set[tuple[str, str, str]] = set()
    for line in path.open(encoding="utf-8"):
        try:
            item = json.loads(line)
            payload = base64.b64decode(item.get("payload_b64", ""))
        except (json.JSONDecodeError, ValueError):
            continue
        if item.get("direction") != "server" or len(payload) < 80:
            continue
        window = WINDOW_RE.search(payload)
        code = CODE_RE.search(payload)
        if not window or not code:
            continue
        start, end = (part.decode("ascii") for part in window.groups())
        result_code = int(code.group(1))
        key = (start, end, str(result_code))
        if key in seen:
            continue
        seen.add(key)
        rounds = [value.decode("ascii") for value in ROUND_RE.findall(payload)]
        yield {"captured_at": item.get("captured_at"), "start_time": start, "end_time": end, "result_code": result_code, "round_hint": rounds[-1] if rounds else None, "source": "anlingshiapi.mangqu.xin/wss"}


if __name__ == "__main__":
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "data/game-flows.jsonl")
    output = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    records = list(parse(source))
    text = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
    if output:
        output.write_text(text + ("\n" if text else ""), encoding="utf-8")
        print(f"提取 {len(records)} 条候选记录 -> {output}")
    else:
        print(text)
