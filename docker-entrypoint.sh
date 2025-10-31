#!/bin/bash
set -e

# --- 1. Activate Virtual Environment ---
. /opt/venv/bin/activate
echo "--- Virtual environment activated ---"

# --- 2. Start FastAPI Application (Gunicorn) in Foreground ---
echo "--- Starting FastAPI Application (Gunicorn) on port ${PORT} as Main Process (PID 1) ---"

# 'exec' replaces the shell with Gunicorn, ensuring Gunicorn is PID 1 and receives signals.
exec "$@"