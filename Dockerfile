# ────────────────────────────────────────────────────────────
# Dockerfile — nabu-trader on Fly.io
# ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# System deps (sqlite3 for DB, ca-certificates for HTTPS)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (exclude secrets — use Fly secrets instead)
COPY .env.example .env.example
COPY config.yaml .
COPY src/ src/

# Create persistent volume mount points
RUN mkdir -p /data/sessions /data/logs

# Signal Fly mode to the app
ENV FLY_MODE=1
ENV DATA_ROOT=/data
ENV PYTHONUNBUFFERED=1

CMD ["python", "src/main.py"]
