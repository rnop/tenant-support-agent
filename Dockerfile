# ---- Base ----
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# curl is used by the compose healthcheck; python:3.11-slim ships neither curl
# nor wget. build-essential covers packages without a manylinux wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install from the lock file, not requirements.txt. The lock was uv-compiled for
# x86_64-unknown-linux-gnu / py3.11 — this image exactly — so the resolution is
# reproducible and skips the backtracking the unpinned file is prone to (see the
# version-conflict notes in requirements.txt).
COPY requirements.lock.txt /app/requirements.lock.txt
RUN pip install --upgrade pip && pip install -r requirements.lock.txt

# Copied so the image runs standalone. In dev, compose bind-mounts the repo over
# /app, so edits take effect without a rebuild.
COPY app/ /app/app/
COPY data/ /app/data/
COPY scripts/ /app/scripts/

EXPOSE 8000

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
