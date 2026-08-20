"""mitmproxy addon used only by a paired Windows authorization agent.

It sees a JWT in the game's initial WebSocket frame and immediately uploads it
to the configured server. Tokens are never written to disk or included in log
output. Use only on a device/account you are authorized to operate.
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone

import requests
from mitmproxy import ctx, http

SERVER_URL = os.getenv("DEVICE_SERVER_URL", "").rstrip("/")
DEVICE_ID = os.getenv("DEVICE_ID", "")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "")
TARGET_HOST = os.getenv("TARGET_HOST", "anlingshiapi.mangqu.xin").lower()
JWT_RE = re.compile(rb"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
last_hash = ""


def expires_at(token: str) -> str:
    part = token.split(".")[1]
    raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
    exp = json.loads(raw)["exp"]
    return datetime.fromtimestamp(int(exp), timezone.utc).isoformat()


class TokenRefreshCapture:
    def websocket_message(self, flow: http.HTTPFlow) -> None:
        global last_hash
        if not flow.websocket or flow.request.host.lower() != TARGET_HOST:
            return
        message = flow.websocket.messages[-1]
        if not message.from_client:
            return
        raw = message.content if isinstance(message.content, bytes) else str(message.content).encode()
        found = JWT_RE.search(raw)
        if not found:
            return
        token = found.group().decode("ascii")
        fingerprint = __import__("hashlib").sha256(token.encode()).hexdigest()
        if fingerprint == last_hash:
            return
        if not SERVER_URL or not DEVICE_ID or not DEVICE_TOKEN:
            ctx.log.warn("credential capture skipped: agent pairing is not configured")
            return
        try:
            expiry = expires_at(token)
            response = requests.post(
                f"{SERVER_URL}/api/v1/device/credentials",
                json={"jwt": token, "expires_at": expiry},
                headers={"X-Device-ID": DEVICE_ID, "X-Device-Token": DEVICE_TOKEN},
                timeout=10,
            )
            response.raise_for_status()
            last_hash = fingerprint
            ctx.log.info(f"credential uploaded; expires {expiry}")
        except Exception as exc:
            ctx.log.warn(f"credential upload failed: {type(exc).__name__}")


addons = [TokenRefreshCapture()]
