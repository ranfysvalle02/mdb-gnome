#!/bin/bash
set -e

# Activate the virtual environment
. /opt/venv/bin/activate

echo "--- Starting Co-located Ray Head Node ---"

# Start Ray head in the background (using &)
# --head: Designates this as the cluster head node.
# --port=6379: Standard GCS port (optional, but good practice).
# --dashboard-host=0.0.0.0: Ensures the dashboard is reachable within the container network.
ray start --head --dashboard-host=0.0.0.0 &

# Wait for Ray to initialize (critical step!)
echo "Waiting for Ray head to stabilize (10 seconds)..."
sleep 10

# Verify Ray is running (optional, but helpful for debugging)
if ray status > /dev/null 2>&1; then
    echo "Ray Head Node is running and stable."
else
    echo "CRITICAL: Ray Head Node failed to start. Exiting."
    exit 1
fi

# Set the RAY_ADDRESS to point the FastAPI process to the local head node.
# Ray's client connection port defaults to 10001.
export RAY_ADDRESS="ray://127.0.0.1:10001"
echo "Set RAY_ADDRESS=${RAY_ADDRESS} for the FastAPI client."

echo "--- Starting FastAPI Application (Gunicorn) ---"
# Execute the command passed to the ENTRYPOINT (e.g., the CMD list)
# exec ensures Gunicorn replaces the shell process, allowing signals to pass through.
exec "$@"

# The error you were previously seeing should now be resolved because 
# `ray.init()` in main.py's lifespan will successfully connect to 127.0.0.1:10001.