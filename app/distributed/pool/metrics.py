"""
WorkerSnapshot — rich live record of a worker node's metrics.

to_worker_view() projects to the lightweight WorkerView used by scheduling strategies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.distributed.scheduler.contracts import WorkerView


@dataclass
class WorkerSnapshot:
    """Live metrics record updated by heartbeat events."""
    node_id: str
    host: str = "127.0.0.1"
    port: int = 9000
    region: str = "us-east-1"
    az: str = "a"
    capabilities: frozenset[str] = field(default_factory=frozenset)

    in_flight_tasks: int = 0
    queue_depth: int = 0
    max_in_flight: int = 16
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    gpu_utilization: float = 0.0
    kafka_consumer_lag: int = 0
    network_latency_ms: float = 0.0

    is_healthy: bool = True
    missed_heartbeats: int = 0

    def to_worker_view(self) -> WorkerView:
        return WorkerView(
            node_id=self.node_id,
            host=self.host,
            region=self.region,
            az=self.az,
            in_flight_tasks=self.in_flight_tasks,
            queue_depth=self.queue_depth,
            max_in_flight=self.max_in_flight,
            cpu_utilization=self.cpu_utilization,
            memory_utilization=self.memory_utilization,
            gpu_utilization=self.gpu_utilization,
            kafka_consumer_lag=self.kafka_consumer_lag,
            network_latency_ms=self.network_latency_ms,
            capabilities=self.capabilities,
            is_healthy=self.is_healthy,
            missed_heartbeats=self.missed_heartbeats,
        )
