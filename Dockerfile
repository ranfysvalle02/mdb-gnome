# FROM python:3.10-slim as builder  
FROM python:3.10-slim-bookworm as builder  
WORKDIR /app  
  
# (Optional) Install build deps  
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*  
  
COPY requirements.txt /app/  
RUN python -m venv /opt/venv && \  
    . /opt/venv/bin/activate && \  
    pip install --upgrade pip && \  
    pip install -r requirements.txt  
  
FROM python:3.10-slim-bookworm  
WORKDIR /app  
  
# Copy venv  
COPY --from=builder /opt/venv /opt/venv  
ENV PATH="/opt/venv/bin:$PATH"  
  
# Copy your actual code  
COPY . .  
  
# Non-root user  
RUN addgroup --system app && adduser --system --group app  
RUN chown -R app:app /app  
USER app  
  
# Default command: Gunicorn on $PORT  
ENV PORT=10000  
EXPOSE 10000  
CMD ["gunicorn", "main:app", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:${PORT}"]  
