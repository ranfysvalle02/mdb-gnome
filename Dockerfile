# --- Stage 1: Build dependencies ---  
FROM python:3.10-slim-bookworm as builder  
WORKDIR /app  
  
# (Optional) Install build deps  
RUN apt-get update && apt-get install -y build-essential && rm -rf /var/lib/apt/lists/*  
  
COPY requirements.txt /app/  
RUN python -m venv /opt/venv && \  
    . /opt/venv/bin/activate && \  
    pip install --upgrade pip && \  
    pip install -r requirements.txt  
  
# --- Stage 2: Final image ---  
FROM python:3.10-slim-bookworm  
WORKDIR /app  
  
# Copy venv from builder  
COPY --from=builder /opt/venv /opt/venv  
ENV PATH="/opt/venv/bin:$PATH"  
  
# Copy your actual code  
COPY . .  
  
# Create non-root user  
RUN addgroup --system app && adduser --system --group app  
RUN chown -R app:app /app  
USER app  
  
#  
# Build ARG and ENV for port  
# ----------------------------------------  
# ARG is set when you do “docker build --build-arg APP_PORT=xxxx .”  
# That ARG is then saved into an ENV variable inside the image.  
#  
ARG APP_PORT=10000  
ENV PORT=$APP_PORT  
  
# EXPOSE does not expand environment variables at run time,  
# but this will work at *build time*, using $APP_PORT.  
EXPOSE ${APP_PORT}  
  
# Default command: Gunicorn on $PORT  
CMD ["gunicorn", "main:app", "--workers", "1", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:${PORT}"] 