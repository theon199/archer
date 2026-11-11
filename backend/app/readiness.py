"""Bounded local readiness; metadata checks never initiate inference."""

import sqlite3
import time
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.config import settings
from app.storage.database import get_db

router = APIRouter()


def validate_runtime(provider=None):
    version = sqlite3.sqlite_version_info
    fixed = version >= (3, 51, 3) or (3, 50, 7) <= version < (3, 51, 0) or (3, 44, 6) <= version < (3, 45, 0)
    if not fixed or sqlite3.threadsafety != 3:
        raise ValueError("A threadsafe SQLite runtime with the WAL-reset fix is required; see docs/sqlite-runtime.md")
    provider = provider or settings.model_provider
    if provider not in {"local-ollama", "local-sglang"}:
        raise ValueError("Cloud inference is disabled")
    if settings.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Archer must bind a loopback address")
    if settings.expected_model_digest and provider != "local-ollama":
        raise ValueError("Artifact digest verification is implemented only for Ollama")
    base = settings.ollama_base_url if provider == "local-ollama" else settings.sglang_base_url
    parsed = urlparse(base)
    if (
        parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
        or parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
    ):
        raise ValueError("The configured inference endpoint must be local loopback without URL credentials")
    if settings.static_dir is not None and not (settings.static_dir / "index.html").is_file():
        raise ValueError("Configured frontend build is missing")


async def model_ready():
    try:
        validate_runtime()
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            if settings.model_provider == "local-ollama":
                response = await client.get(settings.ollama_base_url.rstrip("/") + "/api/tags")
                response.raise_for_status()
                matches = [m for m in response.json()["models"] if m["name"] == settings.ollama_model]
                return bool(matches) and (
                    not settings.expected_model_digest or matches[0].get("digest") == settings.expected_model_digest
                )
            response = await client.get(settings.sglang_base_url.rstrip("/") + "/models")
            response.raise_for_status()
            return any(m["id"] == settings.sglang_model for m in response.json()["data"])
    except Exception:
        return False


@router.get("/ready")
async def readiness():
    database = False
    worker = False
    try:
        async with get_db() as db:
            database = (await (await db.execute("SELECT 1")).fetchone())[0] == 1
            row = await (await db.execute("SELECT heartbeat FROM worker_status WHERE id=1")).fetchone()
            worker = bool(row and 0 <= time.time() - row["heartbeat"] < 5)
    except Exception:
        pass
    local_model = await model_ready()
    ready = database and worker and local_model
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "database": database,
            "worker": worker,
            "local_model": local_model,
        },
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )
