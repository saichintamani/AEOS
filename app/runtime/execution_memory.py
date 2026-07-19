"""
app/runtime/execution_memory.py

Execution Memory Store — persistent record of every workflow execution
for self-improvement.

Captures per-execution:
  - Workflow topology (task graph shape)
  - Scheduling choices (which worker, why)
  - Resource usage (CPU%, memory MB, network bytes)
  - Latency breakdown (queue wait, dispatch, execution, checkpoint, total)
  - Cost proxy (resource × time)
  - Success/failure outcome
  - Recovery events (what failed, how long to recover)

This store is the "long-term memory" of the scheduler.
The PatternMiner reads from it to derive scheduling heuristics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class ResourceUsage:
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    network_bytes_sent: int = 0
    network_bytes_recv: int = 0
    disk_read_bytes: int = 0
    disk_write_bytes: int = 0


@dataclass
class LatencyBreakdown:
    queue_wait_ms: float = 0.0
    dispatch_ms: float = 0.0
    execution_ms: float = 0.0
    checkpoint_ms: float = 0.0
    total_ms: float = 0.0


@dataclass
class RecoveryEvent:
    fault_type: str
    detected_at: float
    recovered_at: float
    recovery_path: str

    @property
    def recovery_time_ms(self) -> float:
        return (self.recovered_at - self.detected_at) * 1000


@dataclass
class ExecutionRecord:
    """Complete execution history for one workflow run."""
    execution_id: str
    workflow_id: str
    workflow_type: str            # e.g. "rag-pipeline", "agent-loop", "batch-etl"
    started_at: float
    ended_at: float
    outcome: str                  # "SUCCESS" | "FAILURE" | "TIMEOUT" | "CANCELLED"

    # Scheduling
    worker_id: str = ""
    worker_selection_reason: str = ""
    task_count: int = 0
    parallel_degree: int = 1      # Max parallel tasks observed

    # Resources
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)

    # Latency
    latency: LatencyBreakdown = field(default_factory=LatencyBreakdown)

    # Cost
    cost_units: float = 0.0       # resource × time proxy

    # Recovery
    recovery_events: list[RecoveryEvent] = field(default_factory=list)

    # Topology fingerprint (hash of task DAG shape)
    topology_fingerprint: str = ""

    # Free-form tags for pattern mining
    tags: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000

    @property
    def succeeded(self) -> bool:
        return self.outcome == "SUCCESS"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_ms"] = self.duration_ms
        d["succeeded"] = self.succeeded
        return d


class ExecutionMemoryStore:
    """
    SQLite-backed store for execution history.

    Chosen over a pure in-memory store because:
      1. Survives scheduler restarts
      2. Supports efficient SQL queries for pattern mining
      3. No external dependency (works in CI without Redis/Postgres)

    Usage::

        store = ExecutionMemoryStore("data/execution_memory.db")
        await store.initialize()
        await store.record(execution_record)
        recent = await store.query_recent(workflow_type="rag-pipeline", limit=100)
    """

    def __init__(self, db_path: str = "data/execution_memory.db") -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        async with self._write() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS executions (
                    execution_id    TEXT PRIMARY KEY,
                    workflow_id     TEXT NOT NULL,
                    workflow_type   TEXT NOT NULL,
                    started_at      REAL NOT NULL,
                    ended_at        REAL NOT NULL,
                    duration_ms     REAL NOT NULL,
                    outcome         TEXT NOT NULL,
                    worker_id       TEXT,
                    task_count      INTEGER DEFAULT 0,
                    parallel_degree INTEGER DEFAULT 1,
                    cost_units      REAL DEFAULT 0.0,
                    topology_fp     TEXT DEFAULT '',
                    cpu_percent     REAL DEFAULT 0.0,
                    memory_mb       REAL DEFAULT 0.0,
                    queue_wait_ms   REAL DEFAULT 0.0,
                    execution_ms    REAL DEFAULT 0.0,
                    total_ms        REAL DEFAULT 0.0,
                    recovery_count  INTEGER DEFAULT 0,
                    tags            TEXT DEFAULT '[]',
                    payload         TEXT DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_workflow_type
                    ON executions(workflow_type, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_outcome
                    ON executions(outcome, started_at DESC);

                CREATE INDEX IF NOT EXISTS idx_worker
                    ON executions(worker_id, started_at DESC);

                CREATE TABLE IF NOT EXISTS recovery_events (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    execution_id    TEXT NOT NULL REFERENCES executions(execution_id),
                    fault_type      TEXT NOT NULL,
                    detected_at     REAL NOT NULL,
                    recovered_at    REAL NOT NULL,
                    recovery_time_ms REAL NOT NULL,
                    recovery_path   TEXT NOT NULL
                );
            """)
        logger.info("ExecutionMemoryStore initialized: %s", self._db_path)

    async def record(self, rec: ExecutionRecord) -> None:
        """Persist an execution record."""
        async with self._write() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO executions VALUES (
                    :execution_id, :workflow_id, :workflow_type,
                    :started_at, :ended_at, :duration_ms, :outcome,
                    :worker_id, :task_count, :parallel_degree,
                    :cost_units, :topology_fp,
                    :cpu_percent, :memory_mb,
                    :queue_wait_ms, :execution_ms, :total_ms,
                    :recovery_count, :tags, :payload
                )
            """, {
                "execution_id": rec.execution_id,
                "workflow_id": rec.workflow_id,
                "workflow_type": rec.workflow_type,
                "started_at": rec.started_at,
                "ended_at": rec.ended_at,
                "duration_ms": rec.duration_ms,
                "outcome": rec.outcome,
                "worker_id": rec.worker_id,
                "task_count": rec.task_count,
                "parallel_degree": rec.parallel_degree,
                "cost_units": rec.cost_units,
                "topology_fp": rec.topology_fingerprint,
                "cpu_percent": rec.resource_usage.cpu_percent,
                "memory_mb": rec.resource_usage.memory_mb,
                "queue_wait_ms": rec.latency.queue_wait_ms,
                "execution_ms": rec.latency.execution_ms,
                "total_ms": rec.latency.total_ms,
                "recovery_count": len(rec.recovery_events),
                "tags": json.dumps(rec.tags),
                "payload": json.dumps(rec.to_dict(), default=str),
            })
            for re in rec.recovery_events:
                conn.execute("""
                    INSERT INTO recovery_events
                    (execution_id, fault_type, detected_at, recovered_at,
                     recovery_time_ms, recovery_path)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    rec.execution_id, re.fault_type, re.detected_at,
                    re.recovered_at, re.recovery_time_ms, re.recovery_path,
                ))

    async def query_recent(
        self,
        workflow_type: str | None = None,
        worker_id: str | None = None,
        outcome: str | None = None,
        limit: int = 100,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        """Query recent executions with optional filters."""
        clauses = []
        params: list[Any] = []

        if workflow_type:
            clauses.append("workflow_type = ?")
            params.append(workflow_type)
        if worker_id:
            clauses.append("worker_id = ?")
            params.append(worker_id)
        if outcome:
            clauses.append("outcome = ?")
            params.append(outcome)
        if since:
            clauses.append("started_at >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM executions {where} "
                f"ORDER BY started_at DESC LIMIT ?",
                params,
            ).fetchall()
            cols = [d[0] for d in conn.execute(
                "SELECT * FROM executions LIMIT 0"
            ).description or []]

        return [dict(zip(cols, row)) for row in rows]

    async def aggregate_stats(
        self,
        workflow_type: str | None = None,
        since_hours: float = 24.0,
    ) -> dict[str, Any]:
        """Aggregate execution statistics for pattern mining."""
        since = time.time() - since_hours * 3600
        where = "WHERE started_at >= ?"
        params: list[Any] = [since]
        if workflow_type:
            where += " AND workflow_type = ?"
            params.append(workflow_type)

        async with self._read() as conn:
            row = conn.execute(f"""
                SELECT
                    COUNT(*) as total,
                    AVG(duration_ms) as avg_duration_ms,
                    MIN(duration_ms) as min_duration_ms,
                    MAX(duration_ms) as max_duration_ms,
                    AVG(cpu_percent) as avg_cpu,
                    AVG(memory_mb) as avg_memory_mb,
                    SUM(CASE WHEN outcome='SUCCESS' THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN outcome='FAILURE' THEN 1 ELSE 0 END) as failures,
                    AVG(recovery_count) as avg_recoveries,
                    AVG(cost_units) as avg_cost
                FROM executions {where}
            """, params).fetchone()

        if not row or row[0] == 0:
            return {"total": 0}

        return {
            "total": row[0],
            "avg_duration_ms": row[1],
            "min_duration_ms": row[2],
            "max_duration_ms": row[3],
            "avg_cpu_percent": row[4],
            "avg_memory_mb": row[5],
            "success_rate": (row[6] or 0) / row[0],
            "failure_rate": (row[7] or 0) / row[0],
            "avg_recoveries_per_run": row[8],
            "avg_cost_units": row[9],
            "window_hours": since_hours,
        }

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[sqlite3.Connection]:
        async with self._lock:
            conn = sqlite3.connect(str(self._db_path))
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path))
        try:
            yield conn
        finally:
            conn.close()
