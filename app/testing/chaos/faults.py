"""
app/testing/chaos/faults.py

Chaos Engineering Fault Library — 12 fault types from FIP §1.

All faults implement the BaseFault protocol:
  inject()  → apply the fault
  observe() → poll system state; returns dict with "recovered" key
  recover() → remove the fault; returns description of recovery path

Faults are designed to be safe in CI/staging — they use
subprocess signals, iptables rules, and file operations that
are fully reversible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class BaseFault(ABC):
    """Abstract base for all chaos faults."""

    @abstractmethod
    async def inject(self) -> None:
        """Apply the fault to the system."""

    @abstractmethod
    async def observe(self) -> dict[str, Any]:
        """
        Poll system state during fault.
        Must include 'recovered' key (bool).
        """

    @abstractmethod
    async def recover(self) -> str:
        """
        Remove the fault.
        Returns a human-readable description of the recovery path taken.
        """


# ── 1. Node Crash Fault ────────────────────────────────────────────────────


class NodeCrashFault(BaseFault):
    """
    Simulates a node crash by sending SIGKILL to a named process group.

    In CI, targets a mock node process launched by the test harness.
    In staging, targets the actual worker pod via kubectl exec.
    """

    def __init__(
        self,
        node_id: str,
        process_name: str = "aeos-worker",
        kubectl_pod: str | None = None,
    ) -> None:
        self.node_id = node_id
        self.process_name = process_name
        self.kubectl_pod = kubectl_pod
        self._crashed_pid: int | None = None
        self._crash_time: float | None = None

    async def inject(self) -> None:
        logger.info("[NodeCrashFault] Crashing node %s", self.node_id)
        if self.kubectl_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "exec", self.kubectl_pod, "--", "kill", "-9", "1",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode not in (0, 137):  # 137 = container killed
                raise RuntimeError(f"kubectl kill failed: {err.decode()}")
        else:
            # Find PID by process name (CI mode)
            result = subprocess.run(
                ["pgrep", "-f", self.process_name],
                capture_output=True, text=True,
            )
            pids = [int(p) for p in result.stdout.strip().split() if p]
            if not pids:
                raise RuntimeError(f"No process found matching '{self.process_name}'")
            self._crashed_pid = pids[0]
            os.kill(self._crashed_pid, 9)  # SIGKILL
        self._crash_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._crash_time or time.time())
        if self.kubectl_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "get", "pod", self.kubectl_pod,
                "--output=jsonpath={.status.phase}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            phase = out.decode().strip()
            recovered = phase == "Running"
        else:
            # Check if a new process replaced the dead one
            result = subprocess.run(
                ["pgrep", "-f", self.process_name],
                capture_output=True, text=True,
            )
            new_pids = [int(p) for p in result.stdout.strip().split() if p]
            recovered = bool(new_pids) and (not self._crashed_pid or new_pids[0] != self._crashed_pid)

        return {
            "node_id": self.node_id,
            "elapsed_seconds": elapsed,
            "recovered": recovered,
        }

    async def recover(self) -> str:
        # Node crash is self-recovering via supervisor/Kubernetes restart policy
        return f"Node {self.node_id} restart handled by orchestrator restart policy"


# ── 2. Process Kill Fault ──────────────────────────────────────────────────


class ProcessKillFault(BaseFault):
    """
    Kills a specific named process (SIGTERM then SIGKILL after grace period).
    More targeted than NodeCrashFault — kills a subprocess within a node.
    """

    def __init__(
        self,
        process_name: str,
        grace_period_seconds: float = 5.0,
    ) -> None:
        self.process_name = process_name
        self.grace_period = grace_period_seconds
        self._killed_pids: list[int] = []
        self._kill_time: float | None = None

    async def inject(self) -> None:
        logger.info("[ProcessKillFault] Killing process: %s", self.process_name)
        result = subprocess.run(
            ["pgrep", "-f", self.process_name],
            capture_output=True, text=True,
        )
        pids = [int(p) for p in result.stdout.strip().split() if p]
        if not pids:
            raise RuntimeError(f"No process found matching '{self.process_name}'")
        for pid in pids:
            try:
                os.kill(pid, 15)  # SIGTERM
            except ProcessLookupError:
                pass
        await asyncio.sleep(self.grace_period)
        for pid in pids:
            try:
                os.kill(pid, 9)  # SIGKILL if still alive
            except ProcessLookupError:
                pass  # Already dead — good
        self._killed_pids = pids
        self._kill_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._kill_time or time.time())
        result = subprocess.run(
            ["pgrep", "-f", self.process_name],
            capture_output=True, text=True,
        )
        new_pids = [int(p) for p in result.stdout.strip().split() if p]
        alive_of_killed = [p for p in self._killed_pids if p in new_pids]
        recovered = bool(new_pids) and not alive_of_killed

        return {
            "process_name": self.process_name,
            "killed_pids": self._killed_pids,
            "elapsed_seconds": elapsed,
            "recovered": recovered,
        }

    async def recover(self) -> str:
        return f"Process '{self.process_name}' restart handled by process supervisor"


# ── 3. Network Partition Fault ─────────────────────────────────────────────


class NetworkPartitionFault(BaseFault):
    """
    Partitions network traffic using iptables DROP rules.

    Drops all TCP traffic between source and target CIDRs.
    Requires NET_ADMIN capability (available in privileged CI containers).
    """

    def __init__(
        self,
        source_cidr: str,
        target_cidr: str,
        direction: str = "both",  # "both" | "ingress" | "egress"
    ) -> None:
        self.source_cidr = source_cidr
        self.target_cidr = target_cidr
        self.direction = direction
        self._rules_added: list[list[str]] = []
        self._inject_time: float | None = None

    async def _run_iptables(self, *args: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "iptables", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"iptables failed: {err.decode()}")

    async def inject(self) -> None:
        logger.info(
            "[NetworkPartitionFault] Partitioning %s ↔ %s",
            self.source_cidr, self.target_cidr,
        )
        rules = []
        if self.direction in ("both", "egress"):
            rule = ["-I", "OUTPUT", "-s", self.source_cidr, "-d", self.target_cidr, "-j", "DROP"]
            await self._run_iptables(*rule)
            rules.append(rule)
        if self.direction in ("both", "ingress"):
            rule = ["-I", "INPUT", "-s", self.target_cidr, "-d", self.source_cidr, "-j", "DROP"]
            await self._run_iptables(*rule)
            rules.append(rule)
        self._rules_added = rules
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        # Partition is active as long as rules exist; no auto-recovery
        return {
            "source_cidr": self.source_cidr,
            "target_cidr": self.target_cidr,
            "partition_active": bool(self._rules_added),
            "elapsed_seconds": elapsed,
            "recovered": False,  # Recovered only after recover() is called
        }

    async def recover(self) -> str:
        removed = 0
        for rule in self._rules_added:
            delete_rule = [r.replace("-I", "-D", 1) if r == rule[0] else r for r in rule]
            delete_rule[0] = "-D"
            try:
                await self._run_iptables(*delete_rule)
                removed += 1
            except RuntimeError as exc:
                logger.warning("[NetworkPartitionFault] Rule removal failed: %s", exc)
        self._rules_added = []
        return f"Removed {removed} iptables DROP rules between {self.source_cidr} and {self.target_cidr}"


# ── 4. Split Brain Fault ───────────────────────────────────────────────────


class SplitBrainFault(BaseFault):
    """
    Simulates split-brain by partitioning the cluster into two halves.

    Uses NetworkPartitionFault internally to drop inter-partition traffic.
    Verifies the system detects and heals the split brain condition.
    """

    def __init__(
        self,
        partition_a_cidrs: list[str],
        partition_b_cidrs: list[str],
    ) -> None:
        self.partition_a = partition_a_cidrs
        self.partition_b = partition_b_cidrs
        self._partitions: list[NetworkPartitionFault] = []
        self._inject_time: float | None = None
        self._healed = False

    async def inject(self) -> None:
        logger.info("[SplitBrainFault] Creating split brain: A=%s B=%s", self.partition_a, self.partition_b)
        for cidr_a in self.partition_a:
            for cidr_b in self.partition_b:
                fault = NetworkPartitionFault(cidr_a, cidr_b, direction="both")
                await fault.inject()
                self._partitions.append(fault)
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        return {
            "partition_a": self.partition_a,
            "partition_b": self.partition_b,
            "partition_count": len(self._partitions),
            "elapsed_seconds": elapsed,
            "healed": self._healed,
            "recovered": self._healed,
        }

    async def recover(self) -> str:
        recovery_paths = []
        for fault in self._partitions:
            path = await fault.recover()
            recovery_paths.append(path)
        self._partitions = []
        self._healed = True
        return f"Split brain healed: {len(recovery_paths)} partition rules removed"


# ── 5. Kafka Broker Loss Fault ─────────────────────────────────────────────


class KafkaBrokerLossFault(BaseFault):
    """
    Simulates loss of a Kafka broker by stopping the broker process
    or suspending its pod.

    Verifies partition leadership reassignment and consumer continuity.
    """

    def __init__(
        self,
        broker_id: int,
        broker_pod: str | None = None,
        kafka_admin_client: Any = None,
    ) -> None:
        self.broker_id = broker_id
        self.broker_pod = broker_pod
        self._admin = kafka_admin_client
        self._inject_time: float | None = None
        self._initial_leader_counts: dict[str, int] = {}

    async def inject(self) -> None:
        logger.info("[KafkaBrokerLossFault] Taking down broker %d", self.broker_id)
        if self.broker_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "delete", "pod", self.broker_pod, "--grace-period=0",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Failed to delete broker pod: {err.decode()}")
        else:
            # Simulate via process kill
            result = subprocess.run(
                ["pgrep", "-f", f"kafka.*broker.id={self.broker_id}"],
                capture_output=True, text=True,
            )
            pids = [int(p) for p in result.stdout.strip().split() if p]
            for pid in pids:
                os.kill(pid, 9)
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        recovered = False

        if self._admin is not None:
            try:
                metadata = await asyncio.wait_for(
                    asyncio.to_thread(self._admin.list_topics, timeout=5),
                    timeout=6.0,
                )
                # Check if any partition still has this broker as leader
                broker_still_leading = any(
                    tp.leader == self.broker_id
                    for tp in metadata.topics.values()
                    for tp in tp.partitions.values()
                )
                recovered = not broker_still_leading
            except Exception as exc:
                logger.debug("[KafkaBrokerLossFault] Admin check error: %s", exc)

        return {
            "broker_id": self.broker_id,
            "elapsed_seconds": elapsed,
            "recovered": recovered,
        }

    async def recover(self) -> str:
        if self.broker_pod:
            # Pod will be rescheduled by Kubernetes StatefulSet controller
            return f"Broker {self.broker_id} pod {self.broker_pod} rescheduled by StatefulSet controller"
        return f"Broker {self.broker_id} restarted by process supervisor"


# ── 6. Redis Shard Loss Fault ──────────────────────────────────────────────


class RedisShardLossFault(BaseFault):
    """
    Simulates loss of a Redis shard (primary or replica).

    In Cluster mode: kills the pod, verifies failover to replica.
    In Sentinel mode: kills primary, verifies Sentinel promotes replica.
    """

    def __init__(
        self,
        shard_pod: str | None = None,
        redis_client: Any = None,
        sentinel_client: Any = None,
    ) -> None:
        self.shard_pod = shard_pod
        self._redis = redis_client
        self._sentinel = sentinel_client
        self._inject_time: float | None = None
        self._primary_before: str | None = None

    async def inject(self) -> None:
        logger.info("[RedisShardLossFault] Taking down Redis shard %s", self.shard_pod)
        # Record current primary before injection
        if self._redis is not None:
            try:
                info = await self._redis.info("replication")
                self._primary_before = info.get("master_host", "unknown")
            except Exception:
                pass

        if self.shard_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "delete", "pod", self.shard_pod, "--grace-period=0",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Failed to delete Redis pod: {err.decode()}")
        else:
            result = subprocess.run(
                ["pgrep", "-x", "redis-server"],
                capture_output=True, text=True,
            )
            pids = [int(p) for p in result.stdout.strip().split() if p]
            if not pids:
                raise RuntimeError("No redis-server process found")
            os.kill(pids[0], 9)
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        recovered = False
        current_primary = None

        if self._redis is not None:
            try:
                info = await asyncio.wait_for(self._redis.info("replication"), timeout=2.0)
                current_primary = info.get("master_host", info.get("role"))
                # Recovered if we have a primary and it can accept writes
                await asyncio.wait_for(self._redis.ping(), timeout=1.0)
                recovered = True
            except Exception:
                pass

        return {
            "shard_pod": self.shard_pod,
            "primary_before": self._primary_before,
            "current_primary": current_primary,
            "elapsed_seconds": elapsed,
            "recovered": recovered,
        }

    async def recover(self) -> str:
        return f"Redis shard {self.shard_pod} failover handled by Sentinel/Cluster controller"


# ── 7. Checkpoint Corruption Fault ────────────────────────────────────────


class CheckpointCorruptionFault(BaseFault):
    """
    Corrupts checkpoint data by overwriting bytes in a checkpoint file.

    Verifies the system detects corruption (via checksum), rejects the
    checkpoint, and recovers by replaying from the previous valid checkpoint.
    """

    def __init__(
        self,
        checkpoint_dir: str = "/tmp/aeos-checkpoints",
        corruption_bytes: int = 16,
    ) -> None:
        self.checkpoint_dir = checkpoint_dir
        self.corruption_bytes = corruption_bytes
        self._corrupted_files: list[str] = []
        self._backups: dict[str, bytes] = {}
        self._inject_time: float | None = None

    async def inject(self) -> None:
        import glob as glob_module
        logger.info("[CheckpointCorruptionFault] Corrupting checkpoints in %s", self.checkpoint_dir)
        pattern = os.path.join(self.checkpoint_dir, "*.chkpt")
        files = glob_module.glob(pattern)
        if not files:
            # Create a synthetic checkpoint file for CI
            os.makedirs(self.checkpoint_dir, exist_ok=True)
            synthetic = os.path.join(self.checkpoint_dir, "test-task-001.chkpt")
            with open(synthetic, "wb") as f:
                f.write(b'{"task_id":"test-001","state":"RUNNING","offset":42}')
            files = [synthetic]

        for path in files[:2]:  # Corrupt at most 2 files
            with open(path, "rb") as f:
                original = f.read()
            self._backups[path] = original

            # Inject random bytes in the middle
            mid = len(original) // 2
            corrupted = (
                original[:mid]
                + bytes(random.getrandbits(8) for _ in range(self.corruption_bytes))
                + original[mid + self.corruption_bytes:]
            )
            with open(path, "wb") as f:
                f.write(corrupted)
            self._corrupted_files.append(path)
            logger.info("[CheckpointCorruptionFault] Corrupted: %s", path)

        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        # The system is "recovered" once corruption is detected and
        # valid checkpoints are restored. We detect via backup state.
        all_restored = all(
            os.path.exists(p) and open(p, "rb").read() == self._backups[p]
            for p in self._corrupted_files
            if p in self._backups
        )
        return {
            "corrupted_files": self._corrupted_files,
            "elapsed_seconds": elapsed,
            "recovered": all_restored and elapsed > 1.0,
        }

    async def recover(self) -> str:
        restored = 0
        for path, original in self._backups.items():
            with open(path, "wb") as f:
                f.write(original)
            restored += 1
        self._corrupted_files = []
        return f"Restored {restored} corrupted checkpoint file(s) from backup"


# ── 8. Clock Skew Fault ───────────────────────────────────────────────────


class ClockSkewFault(BaseFault):
    """
    Introduces artificial clock skew by writing a skew file that AEOS
    time utilities read before calling time.time().

    Verifies the system's clock synchronization detection and Raft
    election timeout adjustments.
    """

    SKEW_FILE = "/tmp/aeos-clock-skew-seconds"

    def __init__(self, skew_seconds: float = 30.0) -> None:
        self.skew_seconds = skew_seconds
        self._inject_time: float | None = None

    async def inject(self) -> None:
        logger.info("[ClockSkewFault] Injecting clock skew: %.1fs", self.skew_seconds)
        with open(self.SKEW_FILE, "w") as f:
            f.write(str(self.skew_seconds))
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        skew_active = os.path.exists(self.SKEW_FILE)
        return {
            "skew_seconds": self.skew_seconds,
            "skew_active": skew_active,
            "elapsed_seconds": elapsed,
            "recovered": not skew_active,
        }

    async def recover(self) -> str:
        if os.path.exists(self.SKEW_FILE):
            os.unlink(self.SKEW_FILE)
        return f"Clock skew file removed; system clocks resynchronized"


# ── 9. Scheduler Crash Fault ──────────────────────────────────────────────


class SchedulerCrashFault(BaseFault):
    """
    Crashes the AEOS task scheduler process.

    Verifies tasks in flight are not lost (lease-based recovery),
    the scheduler restarts, and task throughput recovers within RTO.
    """

    def __init__(
        self,
        scheduler_process_name: str = "aeos.scheduler",
        scheduler_pod: str | None = None,
    ) -> None:
        self.scheduler_process_name = scheduler_process_name
        self.scheduler_pod = scheduler_pod
        self._inject_time: float | None = None
        self._killed_pid: int | None = None

    async def inject(self) -> None:
        logger.info("[SchedulerCrashFault] Crashing scheduler")
        if self.scheduler_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "delete", "pod", self.scheduler_pod, "--grace-period=0",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        else:
            result = subprocess.run(
                ["pgrep", "-f", self.scheduler_process_name],
                capture_output=True, text=True,
            )
            pids = [int(p) for p in result.stdout.strip().split() if p]
            if not pids:
                raise RuntimeError(f"Scheduler process not found: {self.scheduler_process_name}")
            self._killed_pid = pids[0]
            os.kill(self._killed_pid, 9)
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        if self.scheduler_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "get", "pod", self.scheduler_pod,
                "--output=jsonpath={.status.phase}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            recovered = out.decode().strip() == "Running"
        else:
            result = subprocess.run(
                ["pgrep", "-f", self.scheduler_process_name],
                capture_output=True, text=True,
            )
            new_pids = [int(p) for p in result.stdout.strip().split() if p]
            recovered = bool(new_pids) and (self._killed_pid not in new_pids)

        return {
            "scheduler": self.scheduler_process_name,
            "elapsed_seconds": elapsed,
            "recovered": recovered,
        }

    async def recover(self) -> str:
        return "Scheduler restart handled by Kubernetes Deployment (Recreate strategy)"


# ── 10. Governance Outage Fault ───────────────────────────────────────────


class GovernanceOutageFault(BaseFault):
    """
    Simulates governance service unavailability.

    Blocks governance token requests by writing a DENY-ALL policy file
    that the GovernanceClient reads before making gRPC calls.

    Verifies: execution halts within 1 token TTL, no governance bypass,
    tasks queue (not drop) during outage.
    """

    GOVERNANCE_OUTAGE_FLAG = "/tmp/aeos-governance-outage"

    def __init__(
        self,
        governance_pod: str | None = None,
        outage_duration_hint: float = 30.0,
    ) -> None:
        self.governance_pod = governance_pod
        self.outage_duration_hint = outage_duration_hint
        self._inject_time: float | None = None

    async def inject(self) -> None:
        logger.info("[GovernanceOutageFault] Injecting governance outage")
        # Write outage flag file (GovernanceClient checks this in CI mode)
        with open(self.GOVERNANCE_OUTAGE_FLAG, "w") as f:
            f.write(f"outage_injected_at={time.time()}")

        if self.governance_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "delete", "pod", self.governance_pod, "--grace-period=0",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        self._inject_time = time.time()

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        outage_active = os.path.exists(self.GOVERNANCE_OUTAGE_FLAG)

        if self.governance_pod:
            proc = await asyncio.create_subprocess_exec(
                "kubectl", "get", "pod", self.governance_pod,
                "--output=jsonpath={.status.phase}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            pod_running = out.decode().strip() == "Running"
        else:
            pod_running = not outage_active  # Flag-based simulation

        return {
            "outage_active": outage_active,
            "governance_available": pod_running,
            "elapsed_seconds": elapsed,
            "recovered": pod_running and not outage_active,
        }

    async def recover(self) -> str:
        if os.path.exists(self.GOVERNANCE_OUTAGE_FLAG):
            os.unlink(self.GOVERNANCE_OUTAGE_FLAG)
        return "Governance outage flag removed; governance pod rescheduled by Deployment controller"


# ── 11. Storage Exhaustion Fault ──────────────────────────────────────────


class StorageExhaustionFault(BaseFault):
    """
    Exhausts disk space by writing a large sparse file to the
    checkpoint/artifact directory.

    Verifies the system detects low-disk, pauses checkpointing,
    emits a critical alert, and cleans up on recovery.
    """

    def __init__(
        self,
        target_dir: str = "/tmp/aeos-storage-fault",
        fill_mb: int = 512,
    ) -> None:
        self.target_dir = target_dir
        self.fill_mb = fill_mb
        self._fill_file: str | None = None
        self._inject_time: float | None = None

    async def inject(self) -> None:
        logger.info("[StorageExhaustionFault] Filling %dMB in %s", self.fill_mb, self.target_dir)
        os.makedirs(self.target_dir, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=self.target_dir, prefix="chaos-fill-")
        self._fill_file = path

        # Write a sparse file using seek (doesn't actually consume blocks until read)
        # For actual disk pressure, we write real data
        chunk = b"\x00" * (1024 * 1024)  # 1MB
        with os.fdopen(fd, "wb") as f:
            written = 0
            while written < self.fill_mb:
                f.write(chunk)
                written += 1
                if written % 64 == 0:
                    await asyncio.sleep(0)  # Yield to event loop

        self._inject_time = time.time()
        logger.info("[StorageExhaustionFault] Wrote %dMB fill file: %s", self.fill_mb, path)

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        fill_exists = self._fill_file and os.path.exists(self._fill_file)

        # Check available disk space
        if os.path.exists(self.target_dir):
            stat = os.statvfs(self.target_dir)
            free_mb = (stat.f_bavail * stat.f_frsize) // (1024 * 1024)
        else:
            free_mb = -1

        return {
            "target_dir": self.target_dir,
            "fill_file": self._fill_file,
            "fill_active": fill_exists,
            "free_mb": free_mb,
            "elapsed_seconds": elapsed,
            "recovered": not fill_exists,
        }

    async def recover(self) -> str:
        if self._fill_file and os.path.exists(self._fill_file):
            os.unlink(self._fill_file)
            freed_mb = self.fill_mb
        else:
            freed_mb = 0
        return f"Removed {freed_mb}MB fill file from {self.target_dir}; disk space restored"


# ── 12. Memory Pressure Fault ─────────────────────────────────────────────


class MemoryPressureFault(BaseFault):
    """
    Creates artificial memory pressure by allocating a large byte array.

    Verifies: OOM killer does not target critical processes, system
    degrades gracefully (backpressure, not crash), and memory is
    released cleanly on recovery.
    """

    def __init__(self, pressure_mb: int = 512) -> None:
        self.pressure_mb = pressure_mb
        self._allocation: bytearray | None = None
        self._inject_time: float | None = None

    async def inject(self) -> None:
        logger.info("[MemoryPressureFault] Allocating %dMB of memory pressure", self.pressure_mb)
        # Allocate in async-friendly chunks to avoid blocking the event loop
        total_bytes = self.pressure_mb * 1024 * 1024
        chunk_size = 32 * 1024 * 1024  # 32MB chunks
        self._allocation = bytearray()

        allocated = 0
        while allocated < total_bytes:
            to_alloc = min(chunk_size, total_bytes - allocated)
            try:
                self._allocation.extend(bytearray(to_alloc))
                allocated += to_alloc
            except MemoryError:
                logger.warning(
                    "[MemoryPressureFault] MemoryError at %dMB — stopping allocation",
                    allocated // (1024 * 1024),
                )
                break
            await asyncio.sleep(0)  # Yield between allocations

        self._inject_time = time.time()
        logger.info(
            "[MemoryPressureFault] Allocated %.1fMB",
            len(self._allocation) / (1024 * 1024),
        )

    async def observe(self) -> dict[str, Any]:
        elapsed = time.time() - (self._inject_time or time.time())
        allocated_mb = len(self._allocation) / (1024 * 1024) if self._allocation else 0

        # Try reading /proc/meminfo for available memory
        available_mb = -1
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        available_mb = int(line.split()[1]) // 1024
                        break
        except (OSError, FileNotFoundError):
            pass

        return {
            "pressure_mb": self.pressure_mb,
            "allocated_mb": allocated_mb,
            "available_mb": available_mb,
            "elapsed_seconds": elapsed,
            "recovered": self._allocation is None,
        }

    async def recover(self) -> str:
        allocated_mb = len(self._allocation) / (1024 * 1024) if self._allocation else 0
        self._allocation = None
        return f"Released {allocated_mb:.0f}MB memory allocation; GC will reclaim"
