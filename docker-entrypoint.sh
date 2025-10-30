#!/bin/bash
#
# docker-entrypoint.sh
#
set -e

# Activate the virtual environment
. /opt/venv/bin/activate

# Execute the command passed to the container (e.g., "gunicorn", "ray start", etc.)
exec "$@"