"""Main entrypoint for Archer Core FastAPI Application."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.auth import router as auth_router
from app.config import settings
from app.gateway.routes import router as api_router
from app.plugins.permissions import router as permissions_router
from app.readiness import router as readiness_router
from app.readiness import validate_runtime
from app.runs.routes import router as runs_router
from app.storage.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("archer")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Archer storage and database...")
    validate_runtime()
    await init_db()
    from app.plugins.registry import sync_owner_default_grants

    await sync_owner_default_grants()
    logger.info(f"Archer Core online. Active model provider: {settings.model_provider}")
    yield
    logger.info("Archer Core shutting down.")


app = FastAPI(
    title="Archer Core API",
    description="Local-first AI Assistant powering Archer on NVIDIA DGX Spark",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS setup for local and remote development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes
app.include_router(api_router, prefix="/api")
app.include_router(auth_router, prefix="/api/auth")
app.include_router(runs_router, prefix="/api/v1")
app.include_router(permissions_router, prefix="/api")
app.include_router(readiness_router, prefix="/api")


@app.middleware("http")
async def private_cache_policy(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/")
async def root():
    if settings.static_dir is not None:
        return FileResponse(settings.static_dir / "index.html", headers={"Cache-Control": "no-store"})
    return {
        "message": "Archer Core is running on NVIDIA DGX Spark.",
        "documentation": "/docs",
        "health": "/api/health",
        "models": "/api/models/status",
    }


@app.get("/assets/{asset_path:path}")
async def frontend_asset(asset_path: str):
    if settings.static_dir is None:
        raise HTTPException(404, "Frontend asset not found")
    base = (settings.static_dir / "assets").resolve()
    target = (base / asset_path).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "Frontend asset not found")
    return FileResponse(target)
