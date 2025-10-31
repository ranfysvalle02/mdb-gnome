#!/bin/bash
set -e

# --- Configuration ---
# Define a safe, user-writable log path
RAY_LOG_FILE="/var/log/app/ray_head.log"
RAY_WAIT_SECONDS=30 # Increased wait time for cloud stability
# The rest of the ENV is sourced from the Dockerfile

# --- 1. Activate Virtual Environment ---
# The PATH is already set by the Dockerfile, but explicit activation is safest.
. /opt/venv/bin/activate
echo "--- Virtual environment activated ---"


# --- 2. Start Co-located Ray Head Node ---
echo "--- Starting Co-located Ray Head Node (PID: $$) ---"

# Use 'nohup' to detach Ray and redirect logs to the user-owned file
nohup ray start --head --dashboard-host=0.0.0.0 --port=6379 2>&1 > ${RAY_LOG_FILE} &
RAY_PID=$!
echo "Ray Head Node started in background with PID: ${RAY_PID}. Logs: ${RAY_LOG_FILE}"


# --- 3. Wait for Ray GCS to Stabilize ---
echo "Waiting for Ray head to stabilize (up to ${RAY_WAIT_SECONDS} seconds)..."
sleep 5 # Initial wait before trying the connection

# Wait loop for Ray to be queryable
# We check with 'ray status' which confirms cluster health, not just process existence.
SECONDS_ELAPSED=0
STATUS_CHECK_INTERVAL=5
while ! ray status > /dev/null 2>&1; do
    if [ ${SECONDS_ELAPSED} -ge ${RAY_WAIT_SECONDS} ]; then
        echo "CRITICAL: Ray Head Node failed to start after ${RAY_WAIT_SECONDS} seconds."
        echo "Ray logs (from ${RAY_LOG_FILE}):"
        if [ -f "${RAY_LOG_FILE}" ]; then
            cat "${RAY_LOG_FILE}"
        else
            echo "Log file not found."
        fi
        kill ${RAY_PID} 2>/dev/null || true # Attempt to clean up
        exit 1
    fi
    echo "Ray not yet stable. Waiting ${STATUS_CHECK_INTERVAL} more seconds..."
    sleep ${STATUS_CHECK_INTERVAL}
    SECONDS_ELAPSED=$((SECONDS_ELAPSED + STATUS_CHECK_INTERVAL))
done

echo "Ray Head Node is running and stable."


# --- 4. Configure Client Connection ---
# This environment variable is critical for main.py to connect to the local Ray Head.
export RAY_ADDRESS="ray://127.0.0.1:10001"
echo "Set RAY_ADDRESS=${RAY_ADDRESS} for the FastAPI client."


# --- 5. Start FastAPI Application (Gunicorn) ---
echo "--- Starting FastAPI Application (Gunicorn) on port ${PORT} ---"

# 'exec' replaces the current shell with the Gunicorn process, making it the main PID (1).
# This is necessary for proper signal handling (SIGTERM) from container orchestrators (like Render).
exec "$@"