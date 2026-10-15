from __future__ import annotations

import json
import os
import sqlite3
import uuid
import asyncio
import base64
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("STUDIO_DATA_DIR", str(Path.home() / "Library/Application Support/WechatContentStudio")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(PROJECT_ROOT / ".env")

DB = DATA_DIR / "studio.sqlite3"
TEXT_API_BASE_URL = os.environ.get("TEXT_API_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
TEXT_API_KEY = os.environ.get("TEXT_API_KEY", "ollama")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "qwen3:8b")
IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "comfyui")
IMAGE_API_BASE_URL = os.environ.get("IMAGE_API_BASE_URL", "http://127.0.0.1:8188").rstrip("/")
IMAGE_WORKFLOW_PATH = os.environ.get("IMAGE_WORKFLOW_PATH", "")
STABILITY_API_KEY = os.environ.get("STABILITY_API_KEY", "")
STABILITY_ENGINE = os.environ.get("STABILITY_ENGINE", "stable-diffusion-xl-1024-v1-0")

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


class TextRequest(BaseModel):
    kind: str = Field(pattern="^(titles|article|humanize)$")
    prompt: str = Field(min_length=1, max_length=30000)
    temperature: float = Field(default=0.85, ge=0, le=1.5)
    max_tokens: int = Field(default=2200, ge=64, le=12000)


class ImageRequest(BaseModel):
    prompts: list[str] = Field(min_length=1, max_length=3)


@app.get("/api/health")
def health():
    return {"data": {"status": "ok", "version": app.version, "storage": str(DB), "text_model": TEXT_MODEL, "image_provider": IMAGE_PROVIDER}}


@app.get("/api/providers")
def providers():
    return {"data": {
        "text": {"base_url": TEXT_API_BASE_URL, "model": TEXT_MODEL, "configured": bool(TEXT_API_BASE_URL and TEXT_MODEL)},
        "image": {"provider": IMAGE_PROVIDER, "configured": bool(STABILITY_API_KEY or IMAGE_WORKFLOW_PATH)},
    }}


async def chat_completion(request: TextRequest) -> str:
    headers = {"Authorization": f"Bearer {TEXT_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": TEXT_MODEL,
        "messages": [
            {"role": "system", "content": "你是中文公众号内容编辑。只输出用户要求的内容，不解释过程，不编造事实。"},
            {"role": "user", "content": request.prompt},
        ],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(f"{TEXT_API_BASE_URL}/chat/completions", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty model response")
            return content.strip()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:500]
        raise HTTPException(502, f"TEXT_PROVIDER_ERROR: {detail}") from exc
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise HTTPException(502, f"TEXT_PROVIDER_UNAVAILABLE: {exc}") from exc


@app.post("/api/generate/text")
async def generate_text(request: TextRequest):
    content = await chat_completion(request)
    return {"data": {"kind": request.kind, "content": content, "model": TEXT_MODEL}}


async def generate_stability(prompt: str) -> str:
    if not STABILITY_API_KEY:
        raise HTTPException(503, "IMAGE_PROVIDER_NOT_CONFIGURED: set STABILITY_API_KEY")
    payload = {
        "text_prompts": [{"text": prompt, "weight": 1}],
        "width": 1024,
        "height": 576,
        "samples": 1,
        "steps": 30,
        "style_preset": "photographic",
    }
    headers = {"Authorization": f"Bearer {STABILITY_API_KEY}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(f"https://api.stability.ai/v1/generation/{STABILITY_ENGINE}/text-to-image", headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
            encoded = data["artifacts"][0]["base64"]
            return f"data:image/png;base64,{encoded}"
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"IMAGE_PROVIDER_ERROR: {exc.response.text[:500]}") from exc
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise HTTPException(502, f"IMAGE_PROVIDER_UNAVAILABLE: {exc}") from exc


async def generate_comfyui(prompt: str) -> str:
    workflow_file = Path(IMAGE_WORKFLOW_PATH).expanduser()
    if not workflow_file.exists():
        raise HTTPException(503, f"IMAGE_WORKFLOW_NOT_FOUND: {workflow_file}")
    try:
        workflow = json.loads(workflow_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(500, f"IMAGE_WORKFLOW_INVALID: {exc}") from exc
    positive_written = False
    for node in workflow.values():
        if not isinstance(node, dict) or "inputs" not in node:
            continue
        class_type = str(node.get("class_type", ""))
        text_value = node["inputs"].get("text")
        if "CLIPTextEncode" in class_type and isinstance(text_value, str) and not positive_written:
            node["inputs"]["text"] = prompt
            positive_written = True
    if not positive_written:
        raise HTTPException(500, "IMAGE_WORKFLOW_INVALID: no CLIPTextEncode text node found")
    client_id = str(uuid.uuid4())
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            queued = await client.post(f"{IMAGE_API_BASE_URL}/prompt", json={"prompt": workflow, "client_id": client_id})
            queued.raise_for_status()
            prompt_id = queued.json()["prompt_id"]
            for _ in range(180):
                await asyncio.sleep(1)
                history_response = await client.get(f"{IMAGE_API_BASE_URL}/history/{prompt_id}")
                history_response.raise_for_status()
                history = history_response.json().get(prompt_id, {})
                outputs = history.get("outputs", {})
                for output in outputs.values():
                    for image in output.get("images", []):
                        image_response = await client.get(
                            f"{IMAGE_API_BASE_URL}/view",
                            params={"filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")},
                        )
                        image_response.raise_for_status()
                        mime = image_response.headers.get("content-type", "image/png")
                        return f"data:{mime};base64,{base64.b64encode(image_response.content).decode('ascii')}"
            raise HTTPException(504, "IMAGE_PROVIDER_TIMEOUT: ComfyUI did not finish within 180 seconds")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"IMAGE_PROVIDER_ERROR: {exc.response.text[:500]}") from exc
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise HTTPException(502, f"IMAGE_PROVIDER_UNAVAILABLE: {exc}") from exc


@app.post("/api/generate/images")
async def generate_images(request: ImageRequest):
    if IMAGE_PROVIDER == "stability":
        images = [await generate_stability(prompt) for prompt in request.prompts]
        return {"data": {"provider": IMAGE_PROVIDER, "images": images}}
    if IMAGE_PROVIDER == "comfyui":
        if not IMAGE_WORKFLOW_PATH:
            raise HTTPException(503, "IMAGE_PROVIDER_NOT_CONFIGURED: set IMAGE_WORKFLOW_PATH to a ComfyUI API workflow")
        images = [await generate_comfyui(prompt) for prompt in request.prompts]
        return {"data": {"provider": IMAGE_PROVIDER, "images": images}}
    raise HTTPException(400, f"UNKNOWN_IMAGE_PROVIDER: {IMAGE_PROVIDER}")


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
