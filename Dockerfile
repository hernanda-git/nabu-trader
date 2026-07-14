# ────────────────────────────────────────────────────────────
# Dockerfile — nabu-trader on Fly.io
# ────────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Build-time version injection: a PLAIN INTEGER version (e.g. 62).
# Set at deploy via: flyctl deploy --build-arg APP_VERSION=$(python -c "import src.version;print(src.version.__version__)")
# The app (src/main.py) reads APP_VERSION first, so /version + the startup
# banner + traded telemetry report the exact integer version. Falls back to
# src/version.py if no build arg is supplied (local dev).
ARG APP_VERSION=""
ENV APP_VERSION="${APP_VERSION}"

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
