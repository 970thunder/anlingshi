"""Authorized server-side game WebSocket collector.

Run this on the server that hosts the SQLite database and API. It reads the
latest encrypted credential uploaded by a paired desktop agent, reconnects as
needed, and sends normalized settlements to the local API. It never performs
or automates a WeChat login.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import struct
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import websocket
from cryptography.fernet import Fernet, InvalidToken

from collector.game_protocol import extract_candidate

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("MATCH_DB", ROOT / "data" / "matches.sqlite3"))
WS_URL = os.getenv("GAME_WS_URL", "wss://anlingshiapi.mangqu.xin/wss/")
POST_URL = os.getenv("POST_URL", "http://127.0.0.1:8000/api/v1/results")
WRITE_TOKEN = os.getenv("WRITE_TOKEN", "change-me-local")
ENCRYPTION_KEY = os.getenv("ADMIN_ENCRYPTION_KEY", "")
PREFIX = bytes.fromhex(os.getenv("GAME_WS_PREFIX_HEX", "00000000000005cb"))
CLIENT_VERSION = os.getenv("GAME_CLIENT_VERSION", "1.0.23").encode()
HEARTBEAT_SECONDS = max(5, int(os.getenv("GAME_HEARTBEAT_SECONDS", "20")))


def log(message: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {message}", flush=True)


def varint(value: int) -> bytes:
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def build_frame(message_type: int, payload: bytes = b"") -> bytes:
    return PREFIX + struct.pack(">II", message_type, len(payload)) + payload


def login_frame(token: str) -> bytes:
    encoded = token.encode()
    body = b"\x0a" + varint(len(encoded)) + encoded + b"\x12" + varint(len(CLIENT_VERSION)) + CLIENT_VERSION
    return build_frame(2, body)


def heartbeat_frame() -> bytes:
    return build_frame(3)


def current_credential() -> tuple[str, datetime] | None:
    if not ENCRYPTION_KEY:
        raise RuntimeError("ADMIN_ENCRYPTION_KEY is not configured")
    try:
        fernet = Fernet(ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError("ADMIN_ENCRYPTION_KEY is invalid") from exc
    with sqlite3.connect(DB_PATH) as db:
        row = db.execute(
            "SELECT jwt_ciphertext,expires_at FROM collector_credentials WHERE expires_at>? ORDER BY captured_at DESC LIMIT 1",
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchone()
    if row is None:
        return None
    try:
        return fernet.decrypt(row[0].encode()).decode(), datetime.fromisoformat(row[1])
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("stored credential cannot be decrypted") from exc


def submit_candidate(candidate: dict[str, object]) -> None:
    code = int(candidate["result_code"])
    winner = "blue" if code == 1 else "red"
    payload = {
        "match_key": f"{candidate['start_time']}|{candidate['end_time']}",
        "round_id": candidate.get("round_hint") or candidate["start_time"],
        "winner": winner,
        "occurred_at": candidate["end_time"],
        "confidence": 0.9,
        "source": "server-websocket",
        "raw_event": candidate,
    }
    response = requests.post(POST_URL, json=payload, headers={"X-Write-Token": WRITE_TOKEN}, timeout=8)
    response.raise_for_status()


def run() -> None:
    retry_delay = 3
    while True:
        credential = current_credential()
        if credential is None:
            log("no active credential; waiting for desktop authorization")
            time.sleep(30)
            continue
        token, expiry = credential
        if expiry <= datetime.now(timezone.utc):
            time.sleep(10)
            continue
        try:
            log(f"connecting with credential valid until {expiry.isoformat()}")
            ws = websocket.create_connection(WS_URL, timeout=10)
            ws.send(login_frame(token), opcode=websocket.ABNF.OPCODE_BINARY)
            ws.settimeout(1)
            last_heartbeat = time.monotonic()
            retry_delay = 3
            while datetime.now(timezone.utc) < expiry:
                if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                    ws.send(heartbeat_frame(), opcode=websocket.ABNF.OPCODE_BINARY)
                    last_heartbeat = time.monotonic()
                try:
                    message = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(message, bytes):
                    continue
                candidate = extract_candidate(message, datetime.now(timezone.utc).isoformat())
                if candidate:
                    submit_candidate(candidate)
                    log(f"settlement submitted: code {candidate['result_code']}")
            ws.close()
            log("credential expired; waiting for refresh")
        except Exception as exc:
            log(f"connection failed: {type(exc).__name__}; retrying in {retry_delay}s")
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)


if __name__ == "__main__":
    run()
