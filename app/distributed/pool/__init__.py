"""Worker pool: live registry and WorkerView projection."""

from app.distributed.pool.metrics import WorkerSnapshot
from app.distributed.pool.worker_pool import WorkerPool

__all__ = ["WorkerSnapshot", "WorkerPool"]
