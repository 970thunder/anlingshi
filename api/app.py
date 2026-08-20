from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("MATCH_DB", ROOT / "data" / "matches.sqlite3"))
WRITE_TOKEN = os.getenv("WRITE_TOKEN", "change-me-local")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "")
ADMIN_ENCRYPTION_KEY = os.getenv("ADMIN_ENCRYPTION_KEY", "")
SESSION_COOKIE = "admij_session"
SESSION_TTL_HOURS = int(os.getenv("ADMIN_SESSION_TTL_HOURS", "12"))
COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "0") == "1"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
prediction_tasks: set[asyncio.Task[Any]] = set()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS matches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              match_key TEXT NOT NULL UNIQUE,
              round_id TEXT,
              winner TEXT NOT NULL CHECK(winner IN ('red','blue')),
              occurred_at TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              confidence REAL NOT NULL DEFAULT 1.0,
              source TEXT NOT NULL DEFAULT 'collector',
              stake INTEGER,
              reward INTEGER
            );
            CREATE TABLE IF NOT EXISTS raw_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              match_key TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ingest_errors (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              captured_at TEXT NOT NULL,
              message TEXT NOT NULL,
              payload TEXT
            );
            CREATE TABLE IF NOT EXISTS admin_sessions (
              token_hash TEXT PRIMARY KEY,
              csrf_token TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS model_configs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              display_name TEXT NOT NULL,
              base_url TEXT NOT NULL DEFAULT '',
              api_key_ciphertext TEXT NOT NULL DEFAULT '',
              model_name TEXT NOT NULL DEFAULT '',
              enabled INTEGER NOT NULL DEFAULT 0,
              weight REAL NOT NULL DEFAULT 1.0,
              timeout_seconds REAL NOT NULL DEFAULT 15,
              config_version INTEGER NOT NULL DEFAULT 1,
              last_status TEXT,
              last_latency_ms INTEGER,
              last_error TEXT,
              last_checked_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              action TEXT NOT NULL,
              model_name TEXT,
              detail TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collector_devices (
              device_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              token_hash TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              last_seen_at TEXT,
              last_credential_at TEXT
            );
            CREATE TABLE IF NOT EXISTS collector_credentials (
              device_id TEXT PRIMARY KEY,
              jwt_ciphertext TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              captured_at TEXT NOT NULL,
              FOREIGN KEY(device_id) REFERENCES collector_devices(device_id)
            );
            CREATE TABLE IF NOT EXISTS prediction_batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              anchor_match_id INTEGER NOT NULL,
              target_match_key TEXT,
              target_round_id TEXT,
              status TEXT NOT NULL DEFAULT 'queued',
              actual_winner TEXT,
              created_at TEXT NOT NULL,
              resolved_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_prediction_anchor ON prediction_batches(anchor_match_id);
            CREATE TABLE IF NOT EXISTS predictions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL,
              model_config_id INTEGER,
              model_name TEXT NOT NULL,
              config_version INTEGER NOT NULL DEFAULT 1,
              predicted_side TEXT,
              probability REAL,
              sample_size INTEGER NOT NULL DEFAULT 0,
              method TEXT NOT NULL,
              latency_ms INTEGER,
              status TEXT NOT NULL DEFAULT 'queued',
              error TEXT,
              actual_side TEXT,
              correct INTEGER,
              brier_score REAL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(batch_id) REFERENCES prediction_batches(id)
            );
            CREATE INDEX IF NOT EXISTS idx_matches_occurred ON matches(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_predictions_batch ON predictions(batch_id);
            """
        )
        now = iso_now()
        defaults = [
            ("bayesian_frequency", "Bayesian frequency", 1, 1.0),
            ("recent_trend", "Recent trend", 1, 1.0),
            ("transition_markov", "Transition Markov", 1, 1.0),
            ("deepseek", "DeepSeek", 0, 1.0),
            ("qwen", "Qwen", 0, 1.0),
            ("gpt", "GPT", 0, 1.0),
        ]
        for name, display, enabled, weight in defaults:
            db.execute(
                "INSERT OR IGNORE INTO model_configs(name,display_name,enabled,weight,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (name, display, enabled, weight, now, now),
            )
        db.execute("DELETE FROM admin_sessions WHERE expires_at < ?", (now,))


class ResultIn(BaseModel):
    round_id: str | None = Field(default=None, max_length=128)
    winner: str
    occurred_at: datetime | None = None
    captured_at: datetime | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    source: str = Field(default="collector", max_length=64)
    stake: int | None = Field(default=None, ge=0)
    reward: int | None = Field(default=None, ge=0)
    raw_event: dict[str, Any] | None = None
    match_key: str | None = Field(default=None, max_length=256)


class AdminLoginIn(BaseModel):
    username: str
    password: str


class ModelConfigIn(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(default="", max_length=500)
    api_key: str | None = Field(default=None, max_length=1000)
    model_name: str = Field(default="", max_length=200)
    enabled: bool = False
    weight: float = Field(default=1.0, ge=0, le=100)
    timeout_seconds: float = Field(default=15, ge=1, le=120)


class CollectorDeviceIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class CollectorCredentialIn(BaseModel):
    jwt: str = Field(min_length=30, max_length=4096)
    expires_at: datetime | None = None


def normalize_winner(value: str) -> str:
    value = value.strip().lower()
    aliases = {"red": "red", "r": "red", "2": "red", "\u7ea2": "red", "blue": "blue", "b": "blue", "1": "blue", "\u84dd": "blue"}
    if value not in aliases:
        raise ValueError("winner must be red/blue or 1/2")
    return aliases[value]


def row_json(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def get_fernet() -> Fernet | None:
    if not ADMIN_ENCRYPTION_KEY:
        return None
    try:
        return Fernet(ADMIN_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError):
        return None


def encrypt_key(value: str) -> str:
    fernet = get_fernet()
    if not fernet:
        raise HTTPException(status_code=503, detail="ADMIN_ENCRYPTION_KEY is not configured")
    return fernet.encrypt(value.encode()).decode()


def decrypt_key(value: str) -> str:
    if not value:
        return ""
    fernet = get_fernet()
    if not fernet:
        raise RuntimeError("encryption key unavailable")
    try:
        return fernet.decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        raise RuntimeError("stored API key cannot be decrypted")


def hash_session(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def jwt_expiry(token: str) -> datetime:
    """Read only the unsigned expiry claim; signature validation remains server-owned."""
    try:
        part = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        exp = json.loads(decoded)["exp"]
        return datetime.fromtimestamp(int(exp), timezone.utc)
    except (IndexError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JWT expiry") from exc


def device_auth(device_id: str | None, device_token: str | None) -> sqlite3.Row:
    if not device_id or not device_token:
        raise HTTPException(status_code=401, detail="device credentials required")
    with connect() as db:
        row = db.execute("SELECT * FROM collector_devices WHERE device_id=? AND enabled=1", (device_id,)).fetchone()
    if row is None or not secrets.compare_digest(row["token_hash"], hash_session(device_token)):
        raise HTTPException(status_code=401, detail="invalid device credentials")
    return row


def audit(action: str, model_name: str | None = None, detail: str | None = None) -> None:
    safe_detail = (detail or "")[:500]
    with connect() as db:
        db.execute("INSERT INTO admin_audit_logs(action,model_name,detail,created_at) VALUES(?,?,?,?)", (action, model_name, safe_detail, iso_now()))


def admin_session(session_token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> sqlite3.Row:
    if not session_token:
        raise HTTPException(status_code=401, detail="admin login required")
    with connect() as db:
        row = db.execute("SELECT * FROM admin_sessions WHERE token_hash=? AND expires_at > ?", (hash_session(session_token), iso_now())).fetchone()
    if row is None:
        raise HTTPException(status_code=401, detail="admin session expired")
    return row


def require_csrf(x_csrf_token: str | None, session: sqlite3.Row) -> None:
    if not x_csrf_token or not secrets.compare_digest(x_csrf_token, session["csrf_token"]):
        raise HTTPException(status_code=403, detail="invalid csrf token")


def validate_base_url(base_url: str) -> None:
    if not base_url:
        return
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="base_url must be an http(s) URL")


def safe_model(row: sqlite3.Row) -> dict[str, Any]:
    api_key = row["api_key_ciphertext"]
    configured = bool(api_key)
    suffix = ""
    if configured:
        try:
            plain = decrypt_key(api_key)
            suffix = plain[-4:] if len(plain) >= 4 else plain
        except RuntimeError:
            suffix = "????"
    return {
        "id": row["id"], "name": row["name"], "display_name": row["display_name"],
        "base_url": row["base_url"], "model_name": row["model_name"], "enabled": bool(row["enabled"]),
        "weight": row["weight"], "timeout_seconds": row["timeout_seconds"], "config_version": row["config_version"],
        "key_configured": configured, "key_suffix": suffix, "last_status": row["last_status"],
        "last_latency_ms": row["last_latency_ms"], "last_error": row["last_error"], "last_checked_at": row["last_checked_at"],
    }


def read_matches(limit: int) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM matches ORDER BY occurred_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def sequence(window: int) -> list[dict[str, Any]]:
    rows = list(reversed(read_matches(window)))
    return [{"id": row["id"], "round_id": row["round_id"], "winner": row["winner"], "occurred_at": row["occurred_at"], "confidence": row["confidence"]} for row in rows]


def stats_for(window: int) -> dict[str, Any]:
    items = sequence(window)
    counts = {"red": 0, "blue": 0}
    for item in items:
        counts[item["winner"]] += 1
    current = items[-1]["winner"] if items else None
    longest = {"red": 0, "blue": 0}
    last = None
    run = 0
    for item in items:
        side = item["winner"]
        run = run + 1 if side == last else 1
        longest[side] = max(longest[side], run)
        last = side
    total = len(items)
    return {"window": window, "total": total, "counts": counts, "rates": {side: counts[side] / total if total else 0 for side in counts}, "current": current, "current_streak": run if last else 0, "longest_streak": longest, "sequence": items}


def local_prediction(name: str, stats: dict[str, Any]) -> dict[str, Any]:
    sequence_items = stats["sequence"]
    n = stats["total"]
    if name == "bayesian_frequency":
        red = (stats["counts"]["red"] + 1) / (n + 2)
        blue = (stats["counts"]["blue"] + 1) / (n + 2)
        method = "bayesian_frequency"
    elif name == "recent_trend":
        weights = {"red": 1.0, "blue": 1.0}
        decay = 0.82
        for item in reversed(sequence_items):
            weights[item["winner"]] += decay
            decay *= 0.82
        total = weights["red"] + weights["blue"]
        red, blue = weights["red"] / total, weights["blue"] / total
        method = "recent_trend"
    else:
        transitions = {"red": {"red": 1.0, "blue": 1.0}, "blue": {"red": 1.0, "blue": 1.0}}
        for previous, current in zip(sequence_items, sequence_items[1:]):
            transitions[previous["winner"]][current["winner"]] += 1
        last = sequence_items[-1]["winner"] if sequence_items else "red"
        total = sum(transitions[last].values())
        red, blue = transitions[last]["red"] / total, transitions[last]["blue"] / total
        method = "transition_markov"
    side = "red" if red >= blue else "blue"
    return {"predicted_side": side if n else None, "probability": max(red, blue) if n else 0, "probabilities": {"red": red, "blue": blue}, "sample_size": n, "method": method, "status": "ready" if n else "insufficient"}


def prediction_for(window: int) -> dict[str, Any]:
    stats = stats_for(window)
    models = [local_prediction(name, stats) for name in ("bayesian_frequency", "recent_trend", "transition_markov")]
    weights = [item["probability"] for item in models if item["status"] == "ready"]
    red = sum(item["probabilities"]["red"] for item in models) / len(models) if models else 0
    blue = sum(item["probabilities"]["blue"] for item in models) / len(models) if models else 0
    return {"window": window, "predicted_side": "red" if red >= blue else "blue" if stats["total"] else None, "probability": max(red, blue), "probabilities": {"red": red, "blue": blue}, "sample_size": stats["total"], "method": "ensemble", "ready": stats["total"] >= 10, "models": models}


def model_rows(enabled_only: bool = False) -> list[sqlite3.Row]:
    with connect() as db:
        query = "SELECT * FROM model_configs"
        if enabled_only:
            query += " WHERE enabled=1"
        return db.execute(query + " ORDER BY id").fetchall()


def parse_model_content(content: str) -> tuple[str, float]:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.I)
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        raise ValueError("model response is not JSON")
    data = json.loads(match.group(0))
    side = normalize_winner(str(data.get("predicted_side", "")))
    probability = float(data.get("probability"))
    if not 0 <= probability <= 1:
        raise ValueError("probability must be between 0 and 1")
    return side, probability


async def cloud_prediction(row: sqlite3.Row, stats: dict[str, Any]) -> dict[str, Any]:
    key = decrypt_key(row["api_key_ciphertext"])
    if not key or not row["base_url"] or not row["model_name"]:
        raise RuntimeError("model URL, key, and model name are required")
    endpoint = row["base_url"].rstrip("/")
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    sequence_values = ["1" if item["winner"] == "blue" else "2" for item in stats["sequence"]]
    prompt = {
        "sequence": sequence_values[-100:],
        "counts": stats["counts"],
        "rates": stats["rates"],
        "current": stats["current"],
        "current_streak": stats["current_streak"],
    }
    system_prompt = 'Predict the next binary game outcome. Return JSON only: {"predicted_side":"red|blue","probability":0.0}. This is statistical research, not certainty.'
    body = {"model": row["model_name"], "temperature": 0, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}]}
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=row["timeout_seconds"]) as client:
        response = await client.post(endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, json=body)
        response.raise_for_status()
        payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    side, probability = parse_model_content(content)
    return {"predicted_side": side, "probability": probability, "sample_size": stats["total"], "method": row["name"], "status": "ready", "latency_ms": int((time.perf_counter() - started) * 1000)}


async def generate_predictions(batch_id: int, window: int = 50) -> None:
    stats = stats_for(window)
    rows = model_rows(enabled_only=True)
    results: list[dict[str, Any]] = []
    for row in rows:
        started = time.perf_counter()
        try:
            if row["name"] in {"bayesian_frequency", "recent_trend", "transition_markov"}:
                result = local_prediction(row["name"], stats)
                result["latency_ms"] = int((time.perf_counter() - started) * 1000)
            else:
                result = await cloud_prediction(row, stats)
            result.update({"model_config_id": row["id"], "model_name": row["name"], "config_version": row["config_version"], "error": None})
        except Exception as exc:
            result = {"model_config_id": row["id"], "model_name": row["name"], "config_version": row["config_version"], "predicted_side": None, "probability": None, "sample_size": stats["total"], "method": row["name"], "status": "error", "error": str(exc)[:300], "latency_ms": int((time.perf_counter() - started) * 1000)}
        results.append(result)
        with connect() as db:
            db.execute("UPDATE model_configs SET last_status=?,last_latency_ms=?,last_error=?,last_checked_at=? WHERE id=?", (result["status"], result.get("latency_ms"), result.get("error"), iso_now(), row["id"]))
    ready = [(result, row["weight"]) for result, row in zip(results, rows) if result.get("status") == "ready" and result.get("probability") is not None]
    if ready:
        total_weight = sum(weight for _, weight in ready) or 1.0
        red_probability = sum((result["probability"] if result["predicted_side"] == "red" else 1 - result["probability"]) * weight for result, weight in ready) / total_weight
        ensemble_side = "red" if red_probability >= 0.5 else "blue"
        results.append({"model_config_id": None, "model_name": "ensemble", "config_version": 1, "predicted_side": ensemble_side, "probability": red_probability if ensemble_side == "red" else 1 - red_probability, "sample_size": stats["total"], "method": "weighted_ensemble", "latency_ms": 0, "status": "ready", "error": None})
    with connect() as db:
        for result in results:
            db.execute("INSERT INTO predictions(batch_id,model_config_id,model_name,config_version,predicted_side,probability,sample_size,method,latency_ms,status,error,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (batch_id, result.get("model_config_id"), result["model_name"], result["config_version"], result.get("predicted_side"), result.get("probability"), result["sample_size"], result["method"], result.get("latency_ms"), result["status"], result.get("error"), iso_now()))
        db.execute("UPDATE prediction_batches SET status='ready' WHERE id=? AND status='queued'", (batch_id,))
        batch = db.execute("SELECT * FROM prediction_batches WHERE id=?", (batch_id,)).fetchone()
    if batch and batch["actual_winner"]:
        resolve_batch(batch_id, batch["actual_winner"], batch["target_match_key"])
    await publish({"_event": "prediction", "batch_id": batch_id})


def resolve_batch(batch_id: int, winner: str, match_key: str) -> None:
    with connect() as db:
        db.execute("UPDATE prediction_batches SET target_match_key=?,actual_winner=?,status='resolved',resolved_at=? WHERE id=?", (match_key, winner, iso_now(), batch_id))
        rows = db.execute("SELECT id,predicted_side,probability,status FROM predictions WHERE batch_id=?", (batch_id,)).fetchall()
        for row in rows:
            correct = int(row["predicted_side"] == winner) if row["predicted_side"] else None
            probability = row["probability"]
            brier = ((probability if winner == "red" else 1 - probability) - 1) ** 2 if probability is not None else None
            db.execute("UPDATE predictions SET actual_side=?,correct=?,brier_score=? WHERE id=?", (winner, correct, brier, row["id"]))


async def publish(event: dict[str, Any]) -> None:
    for queue in list(subscribers):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await queue.put(event)


def schedule_predictions(anchor_match_id: int) -> None:
    with connect() as db:
        cursor = db.execute("INSERT OR IGNORE INTO prediction_batches(anchor_match_id,created_at) VALUES(?,?)", (anchor_match_id, iso_now()))
        batch = db.execute("SELECT id FROM prediction_batches WHERE anchor_match_id=?", (anchor_match_id,)).fetchone()
    if cursor.rowcount == 1 and batch:
        task = asyncio.create_task(generate_predictions(batch["id"]))
        prediction_tasks.add(task)
        task.add_done_callback(prediction_tasks.discard)


def resolve_previous_batch(previous_match_id: int | None, winner: str, match_key: str) -> None:
    if previous_match_id is None:
        return
    with connect() as db:
        batch = db.execute("SELECT id,status FROM prediction_batches WHERE anchor_match_id=? AND actual_winner IS NULL", (previous_match_id,)).fetchone()
    if batch:
        resolve_batch(batch["id"], winner, match_key)


def predictions_payload(limit: int) -> list[dict[str, Any]]:
    with connect() as db:
        batches = db.execute("SELECT * FROM prediction_batches ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        output = []
        for batch in batches:
            items = db.execute("SELECT model_name,predicted_side,probability,sample_size,method,status,error,actual_side,correct,brier_score FROM predictions WHERE batch_id=? ORDER BY id", (batch["id"],)).fetchall()
            output.append({"id": batch["id"], "anchor_match_id": batch["anchor_match_id"], "target_match_key": batch["target_match_key"], "target_round_id": batch["target_round_id"], "status": batch["status"], "actual_winner": batch["actual_winner"], "created_at": batch["created_at"], "resolved_at": batch["resolved_at"], "models": [dict(item) for item in items]})
    return output


def model_stats(window: int) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT p.model_name,p.correct,p.brier_score,p.status FROM predictions p JOIN prediction_batches b ON b.id=p.batch_id WHERE b.status='resolved' ORDER BY b.id DESC LIMIT ?", (window * 20,)).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["model_name"], []).append(row)
    output = []
    for name, values in grouped.items():
        valid = [row for row in values if row["correct"] is not None]
        output.append({"model_name": name, "samples": len(valid), "correct": sum(row["correct"] for row in valid), "accuracy": sum(row["correct"] for row in valid) / len(valid) if valid else 0, "coverage": len(valid) / len(values) if values else 0, "brier_score": sum(row["brier_score"] for row in valid if row["brier_score"] is not None) / len(valid) if valid else None, "last_status": values[0]["status"] if values else None})
    return output


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield
    for task in prediction_tasks:
        task.cancel()


app = FastAPI(title="Match prediction API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


@app.get("/admij", include_in_schema=False)
@app.get("/admij/login", include_in_schema=False)
def admin_home() -> FileResponse:
    return FileResponse(ROOT / "web" / "admin.html")


@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    with connect() as db:
        total = db.execute("SELECT COUNT(*) FROM matches").fetchone()[0]
    return {"status": "ok", "matches": total, "db": str(DB_PATH)}


@app.post("/api/v1/results")
async def create_result(result: ResultIn, x_write_token: str | None = Header(default=None)) -> dict[str, Any]:
    if x_write_token != WRITE_TOKEN:
        raise HTTPException(status_code=401, detail="invalid write token")
    try:
        winner = normalize_winner(result.winner)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    occurred = (result.occurred_at or utc_now()).astimezone(timezone.utc).isoformat()
    captured = (result.captured_at or utc_now()).astimezone(timezone.utc).isoformat()
    key = result.match_key or result.round_id or hashlib.sha256(f"{winner}|{occurred[:19]}".encode()).hexdigest()
    with connect() as db:
        previous = db.execute("SELECT id FROM matches ORDER BY id DESC LIMIT 1").fetchone()
        cursor = db.execute("INSERT OR IGNORE INTO matches(match_key,round_id,winner,occurred_at,captured_at,confidence,source,stake,reward) VALUES(?,?,?,?,?,?,?,?,?)", (key, result.round_id, winner, occurred, captured, result.confidence, result.source, result.stake, result.reward))
        db.execute("INSERT INTO raw_events(match_key,captured_at,payload) VALUES(?,?,?)", (key, captured, json.dumps(result.raw_event or {}, ensure_ascii=False)[:200000]))
        row = db.execute("SELECT * FROM matches WHERE match_key=?", (key,)).fetchone()
    if cursor.rowcount == 1 and row:
        resolve_previous_batch(previous["id"] if previous else None, winner, key)
        schedule_predictions(row["id"])
    event = row_json(row) or {}
    await publish({"_event": "match", **event})
    return {"created": cursor.rowcount == 1, "duplicate_safe": True, "match": event}


@app.get("/api/v1/results")
def results(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    return {"items": read_matches(limit)}


@app.get("/api/v1/stats")
def stats(window: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    if window not in (50, 100):
        raise HTTPException(status_code=422, detail="window must be 50 or 100")
    return stats_for(window)


@app.get("/api/v1/prediction")
def prediction(window: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    if window not in (50, 100):
        raise HTTPException(status_code=422, detail="window must be 50 or 100")
    return prediction_for(window)


@app.get("/api/v1/predictions")
def predictions(limit: int = Query(default=100, ge=1, le=200)) -> dict[str, Any]:
    return {"items": predictions_payload(limit)}


@app.get("/api/v1/model-stats")
def get_model_stats(window: int = Query(default=50, ge=1, le=100)) -> dict[str, Any]:
    if window not in (50, 100):
        raise HTTPException(status_code=422, detail="window must be 50 or 100")
    return {"window": window, "items": model_stats(window)}


@app.post("/api/v1/admin/login")
def admin_login(credentials: AdminLoginIn, response: Response) -> dict[str, Any]:
    if not ADMIN_PASSWORD or not ADMIN_SESSION_SECRET or not secrets.compare_digest(credentials.username, ADMIN_USERNAME) or not secrets.compare_digest(credentials.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="invalid admin credentials")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    created = utc_now()
    expires = created + timedelta(hours=SESSION_TTL_HOURS)
    with connect() as db:
        db.execute("INSERT INTO admin_sessions(token_hash,csrf_token,created_at,expires_at) VALUES(?,?,?,?)", (hash_session(token), csrf, created.isoformat(), expires.isoformat()))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL_HOURS * 3600, httponly=True, secure=COOKIE_SECURE, samesite="lax", path="/")
    audit("login")
    return {"ok": True, "csrf_token": csrf, "expires_at": expires.isoformat()}


@app.post("/api/v1/admin/logout")
def admin_logout(response: Response, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, bool]:
    require_csrf(x_csrf_token, session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    with connect() as db:
        db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (session["token_hash"],))
    audit("logout")
    return {"ok": True}


@app.get("/api/v1/admin/models")
def admin_models(_: sqlite3.Row = Depends(admin_session)) -> dict[str, Any]:
    return {"items": [safe_model(row) for row in model_rows()]}


def save_model_config(payload: ModelConfigIn, existing: sqlite3.Row | None = None) -> dict[str, Any]:
    validate_base_url(payload.base_url)
    if payload.enabled and payload.name not in {"bayesian_frequency", "recent_trend", "transition_markov"} and not payload.api_key and not (existing and existing["api_key_ciphertext"]):
        raise HTTPException(status_code=422, detail="enabled remote model requires api_key")
    encrypted = existing["api_key_ciphertext"] if existing else ""
    if payload.api_key is not None and payload.api_key.strip():
        encrypted = encrypt_key(payload.api_key.strip())
    now = iso_now()
    with connect() as db:
        if existing:
            db.execute("UPDATE model_configs SET display_name=?,base_url=?,api_key_ciphertext=?,model_name=?,enabled=?,weight=?,timeout_seconds=?,config_version=config_version+1,updated_at=? WHERE id=?", (payload.display_name, payload.base_url.rstrip("/"), encrypted, payload.model_name, int(payload.enabled), payload.weight, payload.timeout_seconds, now, existing["id"]))
            row = db.execute("SELECT * FROM model_configs WHERE id=?", (existing["id"],)).fetchone()
        else:
            try:
                db.execute("INSERT INTO model_configs(name,display_name,base_url,api_key_ciphertext,model_name,enabled,weight,timeout_seconds,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (payload.name, payload.display_name, payload.base_url.rstrip("/"), encrypted, payload.model_name, int(payload.enabled), payload.weight, payload.timeout_seconds, now, now))
            except sqlite3.IntegrityError as exc:
                raise HTTPException(status_code=409, detail="model name already exists") from exc
            row = db.execute("SELECT * FROM model_configs WHERE name=?", (payload.name,)).fetchone()
    audit("model_update" if existing else "model_create", payload.name)
    return safe_model(row)


@app.post("/api/v1/admin/models")
def admin_create_model(payload: ModelConfigIn, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_csrf(x_csrf_token, session)
    return {"model": save_model_config(payload)}


@app.put("/api/v1/admin/models/{model_id}")
def admin_update_model(model_id: int, payload: ModelConfigIn, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_csrf(x_csrf_token, session)
    with connect() as db:
        existing = db.execute("SELECT * FROM model_configs WHERE id=?", (model_id,)).fetchone()
    if existing is None:
        raise HTTPException(status_code=404, detail="model not found")
    return {"model": save_model_config(payload, existing)}


@app.delete("/api/v1/admin/models/{model_id}")
def admin_delete_model(model_id: int, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, bool]:
    require_csrf(x_csrf_token, session)
    with connect() as db:
        row = db.execute("SELECT name FROM model_configs WHERE id=?", (model_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="model not found")
        db.execute("DELETE FROM model_configs WHERE id=?", (model_id,))
    audit("model_delete", row["name"])
    return {"ok": True}


@app.post("/api/v1/admin/models/{model_id}/test")
async def admin_test_model(model_id: int, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_csrf(x_csrf_token, session)
    with connect() as db:
        row = db.execute("SELECT * FROM model_configs WHERE id=?", (model_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="model not found")
    started = time.perf_counter()
    try:
        if row["name"] in {"bayesian_frequency", "recent_trend", "transition_markov"}:
            result = local_prediction(row["name"], stats_for(50))
        else:
            result = await cloud_prediction(row, stats_for(50))
        status, error = "ready", None
    except Exception as exc:
        result, status, error = {}, "error", str(exc)[:300]
    latency = int((time.perf_counter() - started) * 1000)
    with connect() as db:
        db.execute("UPDATE model_configs SET last_status=?,last_latency_ms=?,last_error=?,last_checked_at=? WHERE id=?", (status, latency, error, iso_now(), model_id))
    audit("model_test", row["name"], error or "ok")
    if error:
        raise HTTPException(status_code=502, detail=error)
    return {"status": status, "latency_ms": latency, "result": result}


@app.get("/api/v1/admin/audit")
def admin_audit(_: sqlite3.Row = Depends(admin_session), limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT action,model_name,detail,created_at FROM admin_audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"items": [dict(row) for row in rows]}


@app.get("/api/v1/admin/devices")
def admin_devices(_: sqlite3.Row = Depends(admin_session)) -> dict[str, Any]:
    with connect() as db:
        rows = db.execute("SELECT d.device_id,d.name,d.enabled,d.created_at,d.last_seen_at,d.last_credential_at,c.expires_at,c.captured_at FROM collector_devices d LEFT JOIN collector_credentials c ON c.device_id=d.device_id ORDER BY d.created_at DESC").fetchall()
    return {"items": [{**dict(row), "credential_active": bool(row["expires_at"] and row["expires_at"] > iso_now())} for row in rows]}


@app.post("/api/v1/admin/devices")
def admin_create_device(payload: CollectorDeviceIn, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_csrf(x_csrf_token, session)
    device_id = secrets.token_urlsafe(12)
    pairing_token = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("INSERT INTO collector_devices(device_id,name,token_hash,created_at) VALUES(?,?,?,?)", (device_id, payload.name, hash_session(pairing_token), iso_now()))
    audit("device_create", device_id, payload.name)
    return {"device": {"device_id": device_id, "name": payload.name}, "pairing_token": pairing_token}


@app.delete("/api/v1/admin/devices/{device_id}")
def admin_delete_device(device_id: str, session: sqlite3.Row = Depends(admin_session), x_csrf_token: str | None = Header(default=None)) -> dict[str, bool]:
    require_csrf(x_csrf_token, session)
    with connect() as db:
        row = db.execute("SELECT name FROM collector_devices WHERE device_id=?", (device_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="device not found")
        db.execute("DELETE FROM collector_credentials WHERE device_id=?", (device_id,))
        db.execute("DELETE FROM collector_devices WHERE device_id=?", (device_id,))
    audit("device_delete", device_id, row["name"])
    return {"ok": True}


@app.post("/api/v1/device/credentials")
def device_upload_credential(payload: CollectorCredentialIn, x_device_id: str | None = Header(default=None), x_device_token: str | None = Header(default=None)) -> dict[str, Any]:
    device = device_auth(x_device_id, x_device_token)
    try:
        expiry = jwt_expiry(payload.jwt)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if payload.expires_at and abs((payload.expires_at.astimezone(timezone.utc) - expiry).total_seconds()) > 300:
        raise HTTPException(status_code=422, detail="JWT expiry does not match payload")
    if expiry <= utc_now():
        raise HTTPException(status_code=422, detail="JWT is expired")
    ciphertext = encrypt_key(payload.jwt)
    with connect() as db:
        db.execute("INSERT INTO collector_credentials(device_id,jwt_ciphertext,expires_at,captured_at) VALUES(?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET jwt_ciphertext=excluded.jwt_ciphertext,expires_at=excluded.expires_at,captured_at=excluded.captured_at", (device["device_id"], ciphertext, expiry.isoformat(), iso_now()))
        db.execute("UPDATE collector_devices SET last_seen_at=?,last_credential_at=? WHERE device_id=?", (iso_now(), iso_now(), device["device_id"]))
    audit("credential_upload", device["device_id"], f"expires {expiry.isoformat()}")
    return {"ok": True, "expires_at": expiry.isoformat()}


@app.get("/api/v1/admin/collector-status")
def admin_collector_status(_: sqlite3.Row = Depends(admin_session)) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT d.device_id,d.name,d.last_seen_at,c.expires_at,c.captured_at FROM collector_devices d LEFT JOIN collector_credentials c ON c.device_id=d.device_id WHERE d.enabled=1 AND c.expires_at>? ORDER BY c.expires_at DESC LIMIT 1", (iso_now(),)).fetchone()
    return {"active_credential": dict(row) if row else None}


async def event_stream(request: Request) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=30)
    subscribers.add(queue)
    try:
        yield "event: ready\ndata: {}\n\n"
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                event_type = event.get("_event", "match")
                payload = {key: value for key, value in event.items() if key != "_event"}
                yield f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        subscribers.discard(queue)


@app.get("/api/v1/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(event_stream(request), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
