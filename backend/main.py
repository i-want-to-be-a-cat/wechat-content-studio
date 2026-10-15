from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", str(Path.home() / "Library/Application Support/WechatContentStudio")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = DATA_DIR / "studio.sqlite3"

app = FastAPI(title="孤狼智写本地服务", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:4173", "http://localhost:4173"], allow_methods=["*"], allow_headers=["*"])
app.mount("/studio", StaticFiles(directory=ROOT, html=True), name="studio")
# Keep the original URL working so existing bookmarks do not break.
app.mount("/wechat-content-studio", StaticFiles(directory=ROOT, html=True), name="legacy-studio")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS account_profile (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, positioning TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
          total_items INTEGER NOT NULL, completed_items INTEGER NOT NULL DEFAULT 0,
          payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS articles (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
          word_count INTEGER NOT NULL, saved INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)


init_db()


class Account(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    positioning: str = Field(default="", max_length=4000)


class JobRequest(BaseModel):
    topics: list[str] = Field(min_length=1, max_length=12)
    variants_per_title: int = Field(default=1, ge=1, le=10)
    word_limit: int = Field(default=800, ge=300, le=2000)
    prompt: str = Field(default="", max_length=4000)


@app.get("/api/health")
def health():
    return {"data": {"status": "ok", "version": app.version, "storage": str(DB)}}


@app.get("/api/account")
def get_account():
    with connect() as db:
        row = db.execute("SELECT * FROM account_profile LIMIT 1").fetchone()
        return {"data": dict(row) if row else None}


@app.put("/api/account")
def put_account(account: Account):
    account_id = "default"
    with connect() as db:
        db.execute(
            "INSERT INTO account_profile(id,name,positioning,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, positioning=excluded.positioning, updated_at=excluded.updated_at",
            (account_id, account.name, account.positioning, now()),
        )
    return {"data": {"id": account_id, **account.model_dump()}}


@app.post("/api/jobs/articles", status_code=202)
def create_job(request: JobRequest):
    job_id = str(uuid.uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO jobs(id,kind,status,total_items,payload,created_at) VALUES(?,?,?,?,?,?)",
            (job_id, "articles", "queued", len(request.topics) * request.variants_per_title, json.dumps(request.model_dump(), ensure_ascii=False), now()),
        )
    return {"data": {"job_id": job_id, "total_items": len(request.topics) * request.variants_per_title, "status": "queued"}}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with connect() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "JOB_NOT_FOUND")
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return {"data": result}


@app.get("/api/articles")
def list_articles():
    with connect() as db:
        rows = db.execute("SELECT * FROM articles WHERE saved=1 ORDER BY updated_at DESC").fetchall()
    return {"data": [dict(row) for row in rows]}


@app.post("/api/articles")
def save_article(article: dict):
    required = ("title", "body")
    if any(not str(article.get(key, "")).strip() for key in required):
        raise HTTPException(422, "TITLE_AND_BODY_REQUIRED")
    article_id = str(article.get("id") or uuid.uuid4())
    body = str(article["body"])
    with connect() as db:
        db.execute(
            "INSERT INTO articles(id,title,body,word_count,saved,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET title=excluded.title, body=excluded.body, word_count=excluded.word_count, saved=1, updated_at=excluded.updated_at",
            (article_id, str(article["title"]), body, len("".join(body.split())), 1, now(), now()),
        )
    return {"data": {"id": article_id, "title": article["title"], "body": body}}


@app.get("/")
def root():
    return FileResponse(ROOT / "index.html")


@app.exception_handler(Exception)
async def unhandled(_request, exc):
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": str(exc)}})
