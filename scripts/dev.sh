#!/usr/bin/env bash
set -e

ARCHER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=================================================================="
echo " Starting Archer V1 Development Stack on NVIDIA DGX Spark"
echo "=================================================================="

cleanup() {
    echo ""
    echo "Stopping Archer services..."
    kill "$BACKEND_PID" 2>/dev/null || true
    kill "${WORKER_PID:-}" 2>/dev/null || true
    kill "$FRONTEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
    wait "${WORKER_PID:-}" 2>/dev/null || true
    wait "$FRONTEND_PID" 2>/dev/null || true
    echo "Archer stopped."
}

trap cleanup SIGINT SIGTERM EXIT

# Start Backend
echo "-> Starting Backend on http://127.0.0.1:8000..."
cd "$ARCHER_DIR/backend"
uv run --locked uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 &
BACKEND_PID=$!

uv run --locked python -m app.runs.worker &
WORKER_PID=$!

# Start Frontend
echo "-> Starting Frontend on http://127.0.0.1:5173..."
cd "$ARCHER_DIR/frontend"
pnpm dev --host 127.0.0.1 --port 5173 &
FRONTEND_PID=$!

echo ""
echo "Archer is live:"
echo "  Frontend: http://127.0.0.1:5173 (private loopback only)"
echo "  Backend:  http://localhost:8000"
echo "  Docs:     http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to shut down the application, worker and frontend."

wait
