# ---------------------------------------------  
# Stage 1: Builder – Install Dependencies  
# ---------------------------------------------  
FROM python:3.10-slim AS builder  
  
WORKDIR /app  
  
# Install system packages needed to compile wheels for some Python deps  
RUN apt-get update && apt-get install -y \  
    build-essential \  
 && rm -rf /var/lib/apt/lists/*  
  
# Create and activate a Python venv  
RUN python -m venv /opt/venv  
ENV PATH="/opt/venv/bin:$PATH"  
  
# Copy requirements and install  
COPY requirements.txt .  
RUN pip install --no-cache-dir -r requirements.txt  
  
# ---------------------------------------------  
# Stage 2: Final – Runtime Image  
# ---------------------------------------------  
FROM python:3.10-slim  
  
WORKDIR /app  
  
# Copy venv from builder  
COPY --from=builder /opt/venv /opt/venv  
  
# Copy full source (including experiments/) into /app  
COPY . .  
  
# Activate the venv for all future commands  
ENV PATH="/opt/venv/bin:$PATH"  
  
# -------------------------------------------------------------------  
#   TELL UVICORN WORKERS TO TRUST PROXY HEADERS (fix Mixed Content)  
# -------------------------------------------------------------------  
# Note: Gunicorn doesn’t know --proxy-headers, but we can set env  
# vars that Uvicorn workers read at runtime:  
ENV UVICORN_PROXY_HEADERS=true  
ENV UVICORN_FORWARDED_ALLOW_IPS=*  
  
# (Optional) If you need Ray, also install it in the builder or  
# add "ray==2.6.0" to requirements.txt. Then no more Ray warnings.  
# Example:  
# RUN pip install --no-cache-dir ray  
  
# Create a non-root user for security (optional but recommended)  
RUN addgroup --system appgroup && adduser --system --group appuser  
RUN chown -R appuser:appgroup /app  
USER appuser  
  
# Gunicorn expects $PORT from Render or another platform  
# For local dev you can override with e.g. docker run -e PORT=8000 ...  
CMD ["gunicorn", "main:app", \  
     "-k", "uvicorn.workers.UvicornWorker", \  
     "--bind", "0.0.0.0:10000", \  
     "--workers", "4"]  