# ═══════════════════════════════════════════════════════════════════════════════
# Dockerfile.ml — AEOS ML Training / Inference Worker
# Base: NVIDIA CUDA 12.1 for GPU-accelerated training
# Size: ~4GB (CUDA runtime + PyTorch)
# ═══════════════════════════════════════════════════════════════════════════════

FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04 AS base

ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONPATH=/app \
    TORCH_HOME=/app/.cache/torch

# ─── Builder ──────────────────────────────────────────────────────────────────
FROM base AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-dev \
    python3-pip \
    gcc \
    g++ \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/pip pip /usr/bin/pip3 1

WORKDIR /build
COPY requirements/base.txt requirements/ml.txt ./
RUN pip install --user --no-warn-script-location \
    -r base.txt -r ml.txt

# ─── Production ───────────────────────────────────────────────────────────────
FROM base AS production

ARG VERSION=unknown
LABEL org.opencontainers.image.title="AEOS ML Worker"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3-pip \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1

WORKDIR /app

RUN groupadd -r -g 1000 aeos \
    && useradd -r -u 1000 -g aeos -s /sbin/nologin aeos

COPY --from=builder --chown=aeos:aeos /root/.local /home/aeos/.local
COPY --chown=aeos:aeos ml_platform/ ./ml_platform/
COPY --chown=aeos:aeos app/ml_pipeline/ ./app/ml_pipeline/

RUN mkdir -p /tmp /app/.cache/torch \
    && chown -R aeos:aeos /tmp /app/.cache

USER aeos

ENV PATH="/home/aeos/.local/bin:${PATH}"

# GPU health check: verify CUDA + PyTorch access
HEALTHCHECK --interval=120s \
            --timeout=15s \
            --start-period=60s \
            --retries=3 \
    CMD python3 -c "import torch; assert torch.cuda.is_available(), 'GPU not available'" || exit 1

CMD ["python3", "-m", "ml_platform.pipelines.training_pipeline", \
     "--config", "/config/training.yaml"]
