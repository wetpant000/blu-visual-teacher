#!/usr/bin/env bash
cd "$(dirname "$0")/.."
[ -d .venv ] && . .venv/bin/activate
[ -d frontend/dist ] || (cd frontend && npm run build)
PORT="${BLU_PORT:-8000}"
export BLU_FRONTEND_URL="${BLU_FRONTEND_URL:-http://localhost:${PORT}}"
echo "Open http://localhost:${PORT}"
exec uvicorn backend.main:app --host 0.0.0.0 --port "$PORT"
