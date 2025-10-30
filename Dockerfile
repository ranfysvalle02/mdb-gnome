#
# Dockerfile (Portable for Compose, Render, and Anyscale)
#
# --- STAGE 1: Build virtual environment with dependencies ---
# We use a specific, pinned version to guarantee consistency
# Using "bookworm" (Debian 12) as a modern, secure base
FROM python:3.10.13-slim-bookworm AS builder

# Set build-time args for non-interactive install
ARG DEBIAN_FRONTEND=noninteractive

# Install build-essential for packages that compile C extensions (like bcrypt)
RUN apt-get update && apt-get install -y \
    build-essential \
    libopenblas-dev \
    libfreetype-dev \
    libpng-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Create a virtual environment
RUN python -m venv /opt/venv

# Activate venv and install dependencies
# This is cached, so it only re-runs if requirements.txt changes
COPY requirements.txt .
RUN . /opt/venv/bin/activate && \
    pip install --upgrade pip && \
    pip install -r requirements.txt


# --- STAGE 2: Create the final, lean production image ---
FROM python:3.10.13-slim-bookworm

WORKDIR /app

# Create a non-root user for security
RUN addgroup --system app && adduser --system --group app

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy the application code
# This copies main.py, experiments/, templates/, etc.
COPY . .

# Copy the entrypoint script to a standard PATH location
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Change ownership to our non-root user
RUN chown -R app:app /app /opt/venv

# Switch to the non-root user
USER app

# Set the PATH to include the venv
ENV PATH="/opt/venv/bin:$PATH"

# Set the default port your app will run on inside the container
# This matches your docker-compose.yml 'PORT=10000'
# Render.com will also pick this up.
ENV PORT=10000

# Expose all ports. This is just metadata but good practice.
EXPOSE 10000 10001 6379 8265

# The entrypoint activates the venv before running the CMD
ENTRYPOINT ["docker-entrypoint.sh"]

# --- THIS IS THE KEY ---
# This is the default "production" command for your API.
# It will be USED by Render.
# It will be IGNORED & OVERRIDDEN by docker-compose.yml.
# It will be IGNORED by Anyscale.
CMD ["gunicorn", "main:app", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:10000"]