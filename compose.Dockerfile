# --- Stage 1: The Builder ---
# Use the full Python image to build dependencies
FROM python:3.10.18 AS builder

# Install Ray's system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Create a virtual environment to isolate dependencies
RUN python -m venv /opt/venv
# Add venv to the path for subsequent commands
ENV PATH="/opt/venv/bin:$PATH"

# Copy and install requirements first to leverage cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# install RAY for distributed processing
RUN pip install --no-cache-dir ray[default]==2.50.0

# --- Stage 2: The Final Runtime Image ---
# Use the minimal 'slim' image for the final container
FROM python:3.10.18-slim

# Set working directory
WORKDIR /app

# Install font package for Pillow (used by watermarker)
RUN apt-get update && apt-get install -y \
    fonts-liberation \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user and group
RUN addgroup --system appgroup && adduser --system --group appuser

# Copy the virtual environment from the builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy the application code (will respect .dockerignore)
COPY . .

# Grant ownership to the non-root user
# Do this *after* copying files
RUN chown -R appuser:appgroup /app

# Set the path to use the venv
ENV PATH="/opt/venv/bin:$PATH"

# Switch to the non-root user
USER appuser

# Expose the port
EXPOSE 10000

# Set a flexible WORKERS environment variable
ENV WORKERS ${WORKERS:-4}

# Run the app with Gunicorn, using the $PORT and $WORKERS env vars
CMD ["gunicorn", "-w", "$WORKERS", "-k", "uvicorn.workers.UvicornWorker", "-b", "0.0.0.0:10000", "main:app"]