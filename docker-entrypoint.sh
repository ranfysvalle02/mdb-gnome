#!/bin/bash
set -e

# --- Configuration ---
# Use the safe, user-writable log path established in the Dockerfile
RAY_LOG_FILE="/var/log/app/ray_head.log"
RAY_WAIT_SECONDS=30
RAY_PORT="6379"

# --- 1. Activate Virtual Environment ---
. /opt/venv/bin/activate
echo "--- Virtual environment activated ---"


# --- 2. Start and Detach Ray Head Node ---
echo "--- Starting Ray Head Node in Detached Mode (PID: $$) ---"

# Use a subshell and redirection to fully detach the Ray process.
# We redirect both stdout and stderr to the dedicated log file.
(
    # Set the GCS port explicitly
    ray start --head --dashboard-host=0.0.0.0 --port=${RAY_PORT} --block
) > ${RAY_LOG_FILE} 2>&1 &
RAY_PID=$!
echo "Ray Head Node attempt started in background with PID: ${RAY_PID}. Logs: ${RAY_LOG_FILE}"


# --- 3. Robust Health Check and Wait ---
echo "Waiting up to ${RAY_WAIT_SECONDS} seconds for Ray GCS/Dashboard to stabilize..."

# Wait loop for Ray to become queryable
SECONDS_ELAPSED=0
STATUS_CHECK_INTERVAL=5
while ! ray status > /dev/null 2>&1; do
    if [ ${SECONDS_ELAPSED} -ge ${RAY_WAIT_SECONDS} ]; then
        echo "CRITICAL: Ray Head Node failed to stabilize after ${RAY_WAIT_SECONDS} seconds."
        echo "Recent Ray logs (from ${RAY_LOG_FILE}):"
        tail -n 20 "${RAY_LOG_FILE}" || echo "Log file not readable."
        kill ${RAY_PID} 2>/dev/null || true
        exit 1
    fi
    echo "Ray not yet stable. Waiting ${STATUS_CHECK_INTERVAL} more seconds. Progress: ${SECONDS_ELAPSED}s/${RAY_WAIT_SECONDS}s"
    sleep ${STATUS_CHECK_INTERVAL}
    SECONDS_ELAPSED=$((SECONDS_ELAPSED + STATUS_CHECK_INTERVAL))
done

echo "Ray Head Node is running and stable."


# --- 4. Configure Client Connection ---
# This is what main:app uses to connect to the local head node.
export RAY_ADDRESS="ray://127.0.0.1:10001"
echo "Set RAY_ADDRESS=${RAY_ADDRESS} for the FastAPI client."


# --- 5. Start FastAPI Application (Gunicorn) in Foreground ---
echo "--- Starting FastAPI Application (Gunicorn) on port ${PORT} as Main Process (PID 1) ---"

# 'exec' replaces the shell with Gunicorn, ensuring Gunicorn is PID 1 and receives signals.
# This prevents the Ray background process from becoming the container's primary application.
exec "$@"

# Execution never reaches here if exec succeeds.