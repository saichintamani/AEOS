# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime
WORKDIR /app

# Create the non-root user up front so we can install into a home it can read.
RUN useradd -m -u 1001 aeos

# Copy user-installed packages into the aeos home (readable by the non-root user).
# NOTE: installing to /root/.local and running as non-root breaks at runtime
# because /root is mode 700 — copy into /home/aeos/.local instead.
COPY --from=builder --chown=aeos:aeos /root/.local /home/aeos/.local

# Copy application source (includes app/static/ — the demo UI)
COPY --chown=aeos:aeos app/ ./app/
COPY --chown=aeos:aeos .env.example ./.env.example

# Data directories: model registry, datasets, and RAG persistence (./data/rag)
RUN mkdir -p /app/data/model_registry /app/data/datasets /app/data/rag && \
    chown -R aeos:aeos /app

USER aeos

# Ensure the non-root user's installed packages are on PATH
ENV PATH=/home/aeos/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

EXPOSE 8000

# Single worker: sentence-transformers model is loaded into memory once.
# Scale horizontally (multiple containers) rather than via workers.
# For persistence across restarts, mount a volume at /app/data/rag.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
