.PHONY: help setup dev dev-backend dev-frontend test check format

help:
	@echo "Archer V1 Development Commands:"
	@echo "  make setup        - Install backend (uv) and frontend (pnpm) dependencies"
	@echo "  make dev          - Run backend (:8000) and frontend (:5173) concurrently"
	@echo "  make dev-backend  - Run FastAPI backend with auto-reload"
	@echo "  make dev-frontend - Run Vite React dev server"
	@echo "  make test         - Run backend test suite (pytest)"
	@echo "  make check        - Run linter (ruff) and TypeScript checks"
	@echo "  make format       - Auto-format backend code with ruff"

setup:
	@echo "Setting up backend..."
	cd backend && uv sync --all-extras
	@echo "Setting up frontend..."
	cd frontend && pnpm install

dev-backend:
	cd backend && uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend:
	cd frontend && pnpm dev --host 0.0.0.0 --port 5173

dev:
	@chmod +x scripts/dev.sh
	./scripts/dev.sh

test:
	cd backend && uv run pytest

check:
	cd backend && uv run ruff check .
	cd frontend && pnpm tsc -b

format:
	cd backend && uv run ruff check --fix --unsafe-fixes . && uv run ruff format .
