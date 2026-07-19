## Multi-stage Dockerfile for AEOS
## Stages: deps → base → api → worker
## Build with: docker build --target api -t aeos-api .
##             docker build --target worker -t aeos-worker .

# ── Stage 1: dependency builder ────────────────────────────────────────────
FROM python:3.12-slim AS deps
WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Stage 2: shared base ───────────────────────────────────────────────────
FROM python:3.12-slim AS base
WORKDIR /app

# Non-root user (uid 1000 matches securityContext in K8s manifests)
RUN useradd -m -u 1000 -g 1000 aeos && \
    apt-get update && apt-get install -y --no-install-recommends \
      libpq5 curl && \
    rm -rf /var/lib/apt/lists/*

# Copy installed Python packages
COPY --from=deps --chown=aeos:aeos /root/.local /home/aeos/.local

# Copy application source
COPY --chown=aeos:aeos app/ ./app/
COPY --chown=aeos:aeos aeos/ ./aeos/
COPY --chown=aeos:aeos pyproject.toml README.md ./

# Runtime directories (tmp, cache)
RUN mkdir -p /tmp /app/.cache /workspace && \
    chown -R aeos:aeos /app /tmp /workspace

USER aeos

ENV PATH=/home/aeos/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ── Stage 3: API target ────────────────────────────────────────────────────
FROM base AS api

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Graceful shutdown: SIGTERM triggers FastAPI lifespan cleanup
STOPSIGNAL SIGTERM

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-graceful-shutdown", "30", \
     "--access-log"]

# ── Stage 4: Worker target ─────────────────────────────────────────────────
FROM base AS worker

# Workers need workspace for code execution sandboxes
VOLUME ["/workspace"]

EXPOSE 9090

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:9090/health || exit 1

STOPSIGNAL SIGTERM

# Worker entry point — reads WORKER_MODE=true to activate worker loop
CMD ["python", "-m", "app.worker"]
