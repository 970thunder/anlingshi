from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("MATCH_DB", ROOT / "data" / "matches.sqlite3"))
WRITE_TOKEN = os.getenv("WRITE_TOKEN", "change-me-local")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
subscribers: set[asyncio.Queue[dict[str, Any]]] = set()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
            CREATE INDEX IF NOT EXISTS idx_matches_occurred ON matches(occurred_at DESC);
            """
        )


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


def now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_winner(value: str) -> str:
    value = value.strip().lower()
    aliases = {"红": "red", "red": "red", "r": "red", "蓝": "blue", "blue": "blue", "b": "blue"}
    if value not in aliases:
        raise ValueError("winner must be red/blue or 红/蓝")
    return aliases[value]


def row_json(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def read_matches(limit: int) -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM matches ORDER BY occurred_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [row_json(row) for row in rows]


def sequence(window: int) -> list[dict[str, Any]]:
    rows = list(reversed(read_matches(window)))
    return [{"id": row["id"], "round_id": row["round_id"], "winner": row["winner"], "occurred_at": row["occurred_at"], "confidence": row["confidence"]} for row in rows]


def stats_for(window: int) -> dict[str, Any]:
    items = sequence(window)
    counts = {"red": 0, "blue": 0}
    for item in items:
        counts[item["winner"]] += 1
    current = items[-1]["winner"] if items else None
    current_streak = 0
    longest = {"red": 0, "blue": 0}
    last = None
    run = 0
    for item in items:
        winner = item["winner"]
        run = run + 1 if winner == last else 1
        longest[winner] = max(longest[winner], run)
        last = winner
    if last:
        current_streak = run
    total = len(items)
    return {"window": window, "total": total, "counts": counts, "rates": {side: (counts[side] / total if total else 0) for side in counts}, "current": current, "current_streak": current_streak, "longest_streak": longest, "sequence": items}


def prediction_for(window: int) -> dict[str, Any]:
    stats = stats_for(window)
    n = stats["total"]
    # Uniform Beta(1,1) prior keeps a small sample from producing certainty.
    red = (stats["counts"]["red"] + 1) / (n + 2)
    blue = (stats["counts"]["blue"] + 1) / (n + 2)
    side = "red" if red >= blue else "blue"
    return {"window": window, "predicted_side": side if n else None, "probability": max(red, blue) if n else 0, "probabilities": {"red": red, "blue": blue} if n else {"red": 0, "blue": 0}, "sample_size": n, "method": "贝叶斯平滑频率基线", "ready": n >= 10}


async def publish(event: dict[str, Any]) -> None:
    for queue in list(subscribers):
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await queue.put(event)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="黯灵师对局监控 API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    return FileResponse(ROOT / "web" / "index.html")


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
    occurred = (result.occurred_at or now()).astimezone(timezone.utc).isoformat()
    captured = (result.captured_at or now()).astimezone(timezone.utc).isoformat()
    key = result.match_key or result.round_id or hashlib.sha256(f"{winner}|{occurred[:19]}".encode()).hexdigest()
    raw = json.dumps(result.raw_event or {}, ensure_ascii=False)[:200_000]
    with connect() as db:
        cursor = db.execute("INSERT OR IGNORE INTO matches(match_key,round_id,winner,occurred_at,captured_at,confidence,source,stake,reward) VALUES(?,?,?,?,?,?,?,?,?)", (key, result.round_id, winner, occurred, captured, result.confidence, result.source, result.stake, result.reward))
        db.execute("INSERT INTO raw_events(match_key,captured_at,payload) VALUES(?,?,?)", (key, captured, raw))
        row = db.execute("SELECT * FROM matches WHERE match_key = ?", (key,)).fetchone()
    event = row_json(row)
    await publish(event)
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


async def event_stream(request: Request) -> AsyncIterator[str]:
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=20)
    subscribers.add(queue)
    try:
        yield "event: ready\ndata: {}\n\n"
        while not await request.is_disconnected():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                yield f"event: match\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
    finally:
        subscribers.discard(queue)


@app.get("/api/v1/stream")
async def stream(request: Request) -> StreamingResponse:
    return StreamingResponse(event_stream(request), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
