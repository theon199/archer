"""Main entrypoint for Archer Core FastAPI Application."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.gateway.routes import router as api_router
from app.storage.database import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("archer")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Archer storage and database...")
    await init_db()
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API routes
app.include_router(api_router, prefix="/api")


@app.get("/")
async def root():
    return {
        "message": "Archer Core is running on NVIDIA DGX Spark.",
        "documentation": "/docs",
        "health": "/api/health",
        "models": "/api/models/status",
    }
