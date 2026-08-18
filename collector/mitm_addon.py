"""Authorized mitmproxy addon for recording and normalizing match results.

Run with: mitmdump -s collector/mitm_addon.py
Environment: TARGET_HOSTS=example.com,api.example.com POST_URL=http://127.0.0.1:8000/api/v1/results WRITE_TOKEN=...
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from mitmproxy import ctx, http

TARGET_HOSTS = {h.strip().lower() for h in os.getenv("TARGET_HOSTS", "").split(",") if h.strip()}
POST_URL = os.getenv("POST_URL", "http://127.0.0.1:8000/api/v1/results")
WRITE_TOKEN = os.getenv("WRITE_TOKEN", "change-me-local")
RAW_PATH = Path(os.getenv("RAW_FLOW_LOG", "data/flows.jsonl"))
CANDIDATE_PATH = Path(os.getenv("CANDIDATE_LOG", "data/match-candidates.jsonl"))
RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
CANDIDATE_PATH.parent.mkdir(parents=True, exist_ok=True)
SECRET_KEYS = re.compile(r"(token|session|openid|authorization|cookie|secret|sign|password)", re.I)
WINDOW_RE = re.compile(rb"(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d).{0,12}(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)")
CODE_RE = re.compile(rb"\[([12])\]")
ROUND_RE = re.compile(rb"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {("redacted" if SECRET_KEYS.search(str(key)) else key): ("[REDACTED]" if SECRET_KEYS.search(str(key)) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def parse_body(content: bytes | None) -> Any:
    if not content:
        return {}
    try:
        return redact(json.loads(content.decode("utf-8", errors="replace")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"non_json_body": True, "size": len(content)}


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        safe_query.append((key, "[REDACTED]" if SECRET_KEYS.search(key) else value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment))


def walk(value: Any, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")
    else:
        yield path, value


def find_first(payload: Any, names: set[str]) -> Any:
    for path, value in walk(payload):
        if path.split(".")[-1].lower() in names and value not in (None, ""):
            return value
    return None


def normalize_side(value: Any) -> str | None:
    text = str(value).strip().lower()
    if text in {"red", "r", "红", "红方", "1"}:
        return "red"
    if text in {"blue", "b", "蓝", "蓝方", "2"}:
        return "blue"
    return None


# Keep collection useful before the UI meaning of each code is confirmed. The
# API still uses its existing red/blue storage contract; the web UI renders
# these values as neutral codes 1/2 until the mapping is known.
CODE_SIDE = {
    1: normalize_side(os.getenv("RESULT_CODE_1", "blue")),
    2: normalize_side(os.getenv("RESULT_CODE_2", "red")),
}


def extract_candidate(payload: bytes, captured_at: str) -> dict[str, Any] | None:
    window = WINDOW_RE.search(payload)
    code = CODE_RE.search(payload)
    if not window or not code:
        return None
    start, end = (value.decode("ascii") for value in window.groups())
    rounds = [value.decode("ascii") for value in ROUND_RE.findall(payload)]
    return {"captured_at": captured_at, "start_time": start, "end_time": end, "result_code": int(code.group(1)), "round_hint": rounds[-1] if rounds else None, "source": "anlingshiapi.mangqu.xin/wss"}


def extract_result(payload: Any, flow_key: str) -> dict[str, Any] | None:
    winner = normalize_side(find_first(payload, {"winner", "win", "winside", "result", "victory", "side"}))
    if not winner:
        return None
    round_id = find_first(payload, {"roundid", "round_id", "matchid", "match_id", "gameid", "game_id", "issue", "period"})
    occurred = find_first(payload, {"occurredat", "occurred_at", "settledat", "settled_at", "time", "timestamp"})
    return {"match_key": str(round_id or flow_key), "round_id": str(round_id) if round_id is not None else None, "winner": winner, "occurred_at": occurred if isinstance(occurred, str) else None, "confidence": 0.85 if round_id else 0.65, "source": "mitmproxy", "raw_event": payload}


class MatchCapture:
    def request(self, flow: http.HTTPFlow) -> None:
        if TARGET_HOSTS and flow.request.host.lower() not in TARGET_HOSTS:
            return
        flow.metadata["capture_key"] = hashlib.sha256(f"{flow.request.method}|{flow.request.pretty_url}|{flow.request.timestamp}".encode()).hexdigest()

    def response(self, flow: http.HTTPFlow) -> None:
        if TARGET_HOSTS and flow.request.host.lower() not in TARGET_HOSTS:
            return
        key = flow.metadata.get("capture_key", hashlib.sha256(flow.request.pretty_url.encode()).hexdigest())
        request_body = parse_body(flow.request.raw_content)
        response_body = parse_body(flow.response.raw_content if flow.response else b"")
        record = {"captured_at": datetime.now(timezone.utc).isoformat(), "host": flow.request.host, "method": flow.request.method, "url": redact_url(flow.request.pretty_url), "status": flow.response.status_code if flow.response else None, "request": request_body, "response": response_body}
        with RAW_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        result = extract_result(response_body, key)
        if result:
            try:
                requests.post(POST_URL, json=result, headers={"X-Write-Token": WRITE_TOKEN}, timeout=3)
            except requests.RequestException as exc:
                ctx.log.warn(f"result submit failed: {exc}")

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if not flow.websocket or (TARGET_HOSTS and flow.request.host.lower() not in TARGET_HOSTS):
            return
        message = flow.websocket.messages[-1]
        content = message.content
        raw_content = content if isinstance(content, bytes) else str(content).encode("utf-8", errors="replace")
        content_preview = raw_content[:4000].decode("utf-8", errors="replace")
        try:
            parsed = redact(json.loads(content_preview))
        except (TypeError, json.JSONDecodeError):
            parsed = {"binary": True, "size": len(raw_content), "utf8_preview": content_preview}
        record = {"captured_at": datetime.now(timezone.utc).isoformat(), "host": flow.request.host, "method": "WEBSOCKET", "url": redact_url(flow.request.pretty_url), "direction": "client" if message.from_client else "server", "message": parsed}
        if isinstance(parsed, dict) and parsed.get("binary"):
            record["payload_b64"] = base64.b64encode(raw_content[:200_000]).decode("ascii")
        with RAW_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        candidate = extract_candidate(raw_content, record["captured_at"])
        if candidate:
            with CANDIDATE_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(candidate, ensure_ascii=False) + "\n")
            mapped_side = CODE_SIDE.get(candidate["result_code"])
            if mapped_side:
                try:
                    requests.post(POST_URL, json={"match_key": f"{candidate['start_time']}|{candidate['end_time']}", "round_id": candidate["round_hint"] or candidate["start_time"], "winner": mapped_side, "occurred_at": candidate["end_time"], "confidence": 0.9, "source": "game-websocket", "raw_event": candidate}, headers={"X-Write-Token": WRITE_TOKEN}, timeout=3)
                except requests.RequestException as exc:
                    ctx.log.warn(f"mapped result submit failed: {exc}")
        result = extract_result(parsed, hashlib.sha256(f"ws|{flow.request.pretty_url}|{record['captured_at']}".encode()).hexdigest())
        if result:
            try:
                requests.post(POST_URL, json=result, headers={"X-Write-Token": WRITE_TOKEN}, timeout=3)
            except requests.RequestException as exc:
                ctx.log.warn(f"websocket result submit failed: {exc}")

    def error(self, flow: http.HTTPFlow) -> None:
        """Keep TLS/proxy failures visible without retaining sensitive payloads."""
        if TARGET_HOSTS and flow.request.host.lower() not in TARGET_HOSTS:
            return
        record = {"captured_at": datetime.now(timezone.utc).isoformat(), "host": flow.request.host, "method": flow.request.method, "url": redact_url(flow.request.pretty_url), "error": flow.error.msg if flow.error else "unknown"}
        with RAW_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


addons = [MatchCapture()]
