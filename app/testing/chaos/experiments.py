"""
app/testing/chaos/experiments.py

Pre-built Chaos Experiment Definitions.

Each experiment follows the scientific method:
  - hypothesis: what SHOULD be true after recovery
  - fault: which fault to inject
  - steady_state_probes: what must be true before/after
  - expected_rto: maximum allowed recovery time

All 12 fault types from FIP §1 are covered.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .engine import ChaosExperiment, SteadyStateProbe
from .faults import (
    CheckpointCorruptionFault,
    ClockSkewFault,
    GovernanceOutageFault,
    KafkaBrokerLossFault,
    MemoryPressureFault,
    NetworkPartitionFault,
    NodeCrashFault,
    ProcessKillFault,
    RedisShardLossFault,
    SchedulerCrashFault,
    SplitBrainFault,
    StorageExhaustionFault,
)


# ── Probe factories ────────────────────────────────────────────────────────


def http_probe(url: str, timeout: float = 5.0) -> SteadyStateProbe:
    """Probe that checks an HTTP endpoint returns 200."""
    async def _probe() -> bool:
        try:
            import aiohttp  # type: ignore[import]
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    return resp.status == 200
        except Exception:
            return False

    return SteadyStateProbe(name=f"http:{url}", probe_fn=_probe, required=True)


def redis_probe(redis_client: Any) -> SteadyStateProbe:
    """Probe that checks Redis responds to PING."""
    async def _probe() -> bool:
        try:
            result = await asyncio.wait_for(redis_client.ping(), timeout=3.0)
            return bool(result)
        except Exception:
            return False

    return SteadyStateProbe(name="redis:ping", probe_fn=_probe, required=True)


def kafka_probe(kafka_consumer: Any, topic: str = "aeos.tasks") -> SteadyStateProbe:
    """Probe that checks Kafka topic metadata is accessible."""
    async def _probe() -> bool:
        try:
            partitions = await asyncio.wait_for(
                asyncio.to_thread(kafka_consumer.partitions_for_topic, topic),
                timeout=5.0,
            )
            return partitions is not None
        except Exception:
            return False

    return SteadyStateProbe(name=f"kafka:{topic}", probe_fn=_probe, required=True)


def always_true_probe(name: str = "noop") -> SteadyStateProbe:
    """Placeholder probe for experiments that don't need steady-state checks."""
    async def _probe() -> bool:
        return True

    return SteadyStateProbe(name=name, probe_fn=_probe, required=False)


# ── Experiment builders ────────────────────────────────────────────────────


def node_crash_experiment(
    node_id: str = "worker-0",
    process_name: str = "aeos-worker",
    kubectl_pod: str | None = None,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-001: Worker node crash and recovery."""
    return ChaosExperiment(
        name="fip-001-node-crash",
        fault=NodeCrashFault(node_id=node_id, process_name=process_name, kubectl_pod=kubectl_pod),
        hypothesis=(
            "When a worker node crashes, in-flight tasks are reclaimed within 30s "
            "via lease expiry, the node restarts within 60s, and API availability "
            "is unaffected throughout."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=90.0,
        expected_rto=60.0,
    )


def process_kill_experiment(
    process_name: str = "aeos.worker",
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-002: Critical subprocess kill."""
    return ChaosExperiment(
        name="fip-002-process-kill",
        fault=ProcessKillFault(process_name=process_name, grace_period_seconds=5.0),
        hypothesis=(
            "When the worker subprocess is killed, the supervisor restarts it "
            "within 15s and task processing resumes without message loss."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=3.0,
        max_recovery_wait=60.0,
        expected_rto=15.0,
    )


def network_partition_experiment(
    source_cidr: str = "10.0.0.0/24",
    target_cidr: str = "10.0.1.0/24",
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-003: Network partition between zones."""
    return ChaosExperiment(
        name="fip-003-network-partition",
        fault=NetworkPartitionFault(source_cidr=source_cidr, target_cidr=target_cidr),
        hypothesis=(
            "When the network is partitioned between zones, Raft elects a leader "
            "in the majority partition within 10s, writes are rejected in the "
            "minority partition (fail-closed), and healing completes within 30s."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=60.0,
        expected_rto=30.0,
    )


def split_brain_experiment(
    partition_a: list[str] | None = None,
    partition_b: list[str] | None = None,
) -> ChaosExperiment:
    """FIP-004: Cluster split-brain scenario."""
    return ChaosExperiment(
        name="fip-004-split-brain",
        fault=SplitBrainFault(
            partition_a_cidrs=partition_a or ["10.0.0.0/24"],
            partition_b_cidrs=partition_b or ["10.0.1.0/24"],
        ),
        hypothesis=(
            "During split-brain, only the majority partition accepts writes. "
            "The minority partition enters read-only mode. After healing, "
            "both partitions converge to the same state within 30s."
        ),
        steady_state_probes=[always_true_probe("split-brain-pre-check")],
        observation_interval=5.0,
        max_recovery_wait=120.0,
        expected_rto=30.0,
    )


def kafka_broker_loss_experiment(
    broker_id: int = 0,
    broker_pod: str | None = None,
    kafka_admin: Any = None,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-005: Kafka broker failure and partition leadership reassignment."""
    return ChaosExperiment(
        name="fip-005-kafka-broker-loss",
        fault=KafkaBrokerLossFault(
            broker_id=broker_id,
            broker_pod=broker_pod,
            kafka_admin_client=kafka_admin,
        ),
        hypothesis=(
            "When a Kafka broker fails, partition leadership reassigns within 30s, "
            "consumers reconnect automatically, and message throughput resumes "
            "with at-most-once delivery guarantee maintained."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=120.0,
        expected_rto=30.0,
    )


def redis_shard_loss_experiment(
    shard_pod: str | None = None,
    redis_client: Any = None,
) -> ChaosExperiment:
    """FIP-006: Redis primary shard failure and failover."""
    return ChaosExperiment(
        name="fip-006-redis-shard-loss",
        fault=RedisShardLossFault(shard_pod=shard_pod, redis_client=redis_client),
        hypothesis=(
            "When the Redis primary fails, Sentinel/Cluster promotes a replica "
            "within 30s (RTO). Lease operations resume with no data loss (RPO=0 "
            "for committed writes, RPO≤1s for uncommitted)."
        ),
        steady_state_probes=[redis_probe(redis_client)] if redis_client else [always_true_probe("redis-pre")],
        observation_interval=5.0,
        max_recovery_wait=60.0,
        expected_rto=30.0,
    )


def checkpoint_corruption_experiment(
    checkpoint_dir: str = "/tmp/aeos-checkpoints",
) -> ChaosExperiment:
    """FIP-007: Checkpoint file corruption detection and recovery."""
    return ChaosExperiment(
        name="fip-007-checkpoint-corruption",
        fault=CheckpointCorruptionFault(checkpoint_dir=checkpoint_dir),
        hypothesis=(
            "When checkpoint data is corrupted, the system detects the checksum "
            "mismatch within 5s, rejects the corrupted checkpoint, and replays "
            "from the last valid checkpoint without task re-execution."
        ),
        steady_state_probes=[always_true_probe("checkpoint-pre")],
        observation_interval=3.0,
        max_recovery_wait=30.0,
        expected_rto=10.0,
    )


def clock_skew_experiment(
    skew_seconds: float = 30.0,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-008: Clock skew detection and Raft impact."""
    return ChaosExperiment(
        name="fip-008-clock-skew",
        fault=ClockSkewFault(skew_seconds=skew_seconds),
        hypothesis=(
            f"When a {skew_seconds}s clock skew is introduced, the system "
            "detects the drift within 10s, Raft election timeouts self-adjust, "
            "and lease validity checks account for the skew."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=60.0,
        expected_rto=15.0,
    )


def scheduler_crash_experiment(
    scheduler_process: str = "aeos.scheduler",
    scheduler_pod: str | None = None,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-009: Scheduler crash and task lease-based recovery."""
    return ChaosExperiment(
        name="fip-009-scheduler-crash",
        fault=SchedulerCrashFault(
            scheduler_process_name=scheduler_process,
            scheduler_pod=scheduler_pod,
        ),
        hypothesis=(
            "When the scheduler crashes, queued tasks are not lost (stored in "
            "Kafka/Redis), the scheduler restarts within 30s, and scheduling "
            "resumes with no duplicate executions (exactly-once guarantee)."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=90.0,
        expected_rto=30.0,
    )


def governance_outage_experiment(
    governance_pod: str | None = None,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-010: Governance service outage — fail-closed verification."""
    return ChaosExperiment(
        name="fip-010-governance-outage",
        fault=GovernanceOutageFault(governance_pod=governance_pod),
        hypothesis=(
            "When the governance service is unavailable, new task execution "
            "is BLOCKED (fail-closed, not fail-open). Existing approved tasks "
            "complete normally. Governance recovers within 60s and queued "
            "approval requests are processed."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=120.0,
        expected_rto=60.0,
    )


def storage_exhaustion_experiment(
    target_dir: str = "/tmp/aeos-storage-fault",
    fill_mb: int = 256,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-011: Storage exhaustion and checkpoint backpressure."""
    return ChaosExperiment(
        name="fip-011-storage-exhaustion",
        fault=StorageExhaustionFault(target_dir=target_dir, fill_mb=fill_mb),
        hypothesis=(
            "When disk space is exhausted, the system detects low-disk within "
            "10s, pauses checkpoint writes (not crashes), emits a CRITICAL "
            "alert, and resumes checkpointing within 30s of disk recovery."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=60.0,
        expected_rto=30.0,
    )


def memory_pressure_experiment(
    pressure_mb: int = 256,
    api_url: str = "http://localhost:8000",
) -> ChaosExperiment:
    """FIP-012: Memory pressure and graceful degradation."""
    return ChaosExperiment(
        name="fip-012-memory-pressure",
        fault=MemoryPressureFault(pressure_mb=pressure_mb),
        hypothesis=(
            f"Under {pressure_mb}MB memory pressure, the system applies "
            "backpressure to new task intake (reduces concurrency), does NOT "
            "crash critical processes, and recovers throughput within 30s "
            "of pressure removal."
        ),
        steady_state_probes=[http_probe(f"{api_url}/healthz")],
        observation_interval=5.0,
        max_recovery_wait=60.0,
        expected_rto=15.0,
    )


# ── Full Suite ─────────────────────────────────────────────────────────────


def build_full_suite(
    api_url: str = "http://localhost:8000",
    redis_client: Any = None,
    kafka_admin: Any = None,
    enable_network_faults: bool = False,  # Requires NET_ADMIN; disabled by default
) -> list[ChaosExperiment]:
    """
    Build the complete FIP §1 experiment suite.

    Set enable_network_faults=True only in privileged CI containers
    or environments where iptables manipulation is permitted.
    """
    suite = [
        process_kill_experiment(api_url=api_url),
        checkpoint_corruption_experiment(),
        clock_skew_experiment(api_url=api_url),
        scheduler_crash_experiment(api_url=api_url),
        governance_outage_experiment(api_url=api_url),
        storage_exhaustion_experiment(api_url=api_url),
        memory_pressure_experiment(api_url=api_url),
    ]

    if redis_client:
        suite.append(redis_shard_loss_experiment(redis_client=redis_client))

    if kafka_admin:
        suite.append(kafka_broker_loss_experiment(kafka_admin=kafka_admin, api_url=api_url))

    if enable_network_faults:
        suite.extend([
            network_partition_experiment(api_url=api_url),
            split_brain_experiment(),
        ])

    return suite


def build_ci_safe_suite(api_url: str = "http://localhost:8000") -> list[ChaosExperiment]:
    """
    Minimal suite safe to run in standard CI without elevated privileges.
    All faults use process signals, file operations, or memory allocation.
    """
    return [
        process_kill_experiment(api_url=api_url),
        checkpoint_corruption_experiment(),
        clock_skew_experiment(api_url=api_url),
        scheduler_crash_experiment(api_url=api_url),
        governance_outage_experiment(api_url=api_url),
        storage_exhaustion_experiment(fill_mb=64, api_url=api_url),
        memory_pressure_experiment(pressure_mb=128, api_url=api_url),
    ]
