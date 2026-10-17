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
    kill "$FRONTEND_PID" 2>/dev/null || true
    wait "$BACKEND_PID" 2>/dev/null || true
    wait "$FRONTEND_PID" 2>/dev/null || true
    echo "Archer stopped."
}

trap cleanup SIGINT SIGTERM EXIT

# Start Backend
echo "-> Starting Backend on http://127.0.0.1:8000..."
cd "$ARCHER_DIR/backend"
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Start Frontend
echo "-> Starting Frontend on http://127.0.0.1:5173..."
cd "$ARCHER_DIR/frontend"
pnpm dev --host 0.0.0.0 --port 5173 &
FRONTEND_PID=$!

echo ""
echo "Archer is live:"
echo "  Frontend: http://localhost:5173 (or via Tailscale: http://$(hostname -I | awk '{print $1}'):5173)"
echo "  Backend:  http://localhost:8000"
echo "  Docs:     http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to shut down both servers."

wait
