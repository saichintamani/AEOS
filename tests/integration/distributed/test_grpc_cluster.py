"""
Cross-PROCESS gRPC cluster testbed — the Phase 13 Sprint 2 proof.

Launches 3 AEOS worker nodes as separate OS processes
(app.distributed.testbed.grpc_worker_node), each running a real grpc.aio
EventBusService server. The test process itself is the 4th node ("scheduler"):
it publishes governance-signed tasks over the wire and observes results.

What this proves that in-process tests cannot:
  - Task dispatch crosses a real socket + process boundary (scheduler proc →
    worker proc), decoded and executed by an independently-bootstrapped runtime.
  - Fail-closed governance holds across the boundary: an UNSIGNED task sent to a
    production worker process is rejected and never executes.
  - Results (TASK_COMPLETED) flow back from each worker process to the scheduler.
  - Fan-out across 3 processes: three addressed tasks land on three distinct
    workers concurrently.

Ports are ephemeral: each worker prints "READY <host:port>" on stdout; the test
reads it and peers the scheduler to that address. No fixed ports, no Redis.

Marked slow — spawns real interpreters. Skips cleanly if grpcio/cryptography
are unavailable.

Phase: 13 Sprint 2
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.transport.grpc_bus import GrpcEventBusTransport
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[3]


async def _wait(pred, timeout=15.0, interval=0.05):
    for _ in range(int(timeout / interval)):
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


async def _read_ready(proc: subprocess.Popen, timeout=15.0) -> str:
    """Read lines from a worker process' stdout until 'READY <addr>'."""
    loop = asyncio.get_running_loop()

    def _blocking_readline() -> str:
        assert proc.stdout is not None
        return proc.stdout.readline()

    deadline = loop.time() + timeout
    while loop.time() < deadline:
        line = await loop.run_in_executor(None, _blocking_readline)
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"worker exited early rc={proc.returncode}")
            continue
        line = line.strip()
        if line.startswith("READY "):
            return line.split(" ", 1)[1]
    raise TimeoutError("worker did not become READY in time")


def _spawn_worker(node_id: str, sched_addr: str, keys_dir: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [
            sys.executable, "-m", "app.distributed.testbed.grpc_worker_node",
            "--node-id", node_id,
            "--host", "127.0.0.1",
            "--port", "0",
            "--peer", sched_addr,
            "--keys-dir", keys_dir,
            "--environment", "production",
            "--work-ms", "10",
        ],
        cwd=str(_REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _accepted(worker_node, *, token_id=None, raw_token=None, task_id="t"):
    payload = {
        "task_id": task_id, "workflow_id": "wf", "step_id": "s1",
        "task_type": "echo", "task_payload": {"n": task_id}, "priority": "normal",
        "lease_key": f"exec:{task_id}", "fencing_token": 1,
        "assigned_worker_id": worker_node, "attempt": 0, "max_attempts": 3,
    }
    if token_id is not None:
        payload["token_id"] = token_id
    if raw_token is not None:
        payload["raw_token"] = raw_token
    return EventEnvelope(
        event_type=DistributedEventType.TASK_ACCEPTED, payload=payload,
        source_node_id="scheduler", workflow_id="wf", task_id=task_id,
    )


class _Scheduler:
    def __init__(self, keys_dir: str):
        self.keys_dir = keys_dir
        self.transport = GrpcEventBusTransport("scheduler", port=0)
        self.serializer = JsonEventSerializer()
        self.completed: list[str] = []
        self.procs: list[subprocess.Popen] = []
        self.by_node: dict[str, subprocess.Popen] = {}

    async def start(self):
        await self.transport.start()
        self.pub = DefaultEventPublisher(
            clock=MonotonicClock(), router=DefaultEventRouter(),
            serializer=self.serializer, transport=self.transport, source_node_id="scheduler",
        )
        self.con = DefaultEventConsumer(self.transport, self.serializer, node_id="scheduler")

        async def on_completed(env: EventEnvelope):
            # Handlers on a topic receive every event on that topic; TASK_ACCEPTED
            # and TASK_COMPLETED share aeos.events.execution, so filter by type.
            if env.event_type != DistributedEventType.TASK_COMPLETED:
                return
            self.completed.append(env.payload.get("task_id", ""))

        await self.con.subscribe([DistributedEventType.TASK_COMPLETED], on_completed, "scheduler")
        await self.con.start()

    async def add_workers(self, node_ids: list[str]):
        sched_addr = self.transport.address
        for nid in node_ids:
            proc = _spawn_worker(nid, sched_addr, self.keys_dir)
            self.procs.append(proc)
            addr = await _read_ready(proc)
            self.transport.add_peer(addr)
            self.by_node[nid] = proc

    async def kill_worker(self, node_id: str):
        """Hard-kill one worker process (simulates a node crash)."""
        proc = self.by_node[node_id]
        proc.kill()
        try:
            await asyncio.get_running_loop().run_in_executor(None, proc.wait, 5)
        except Exception:
            pass

    def sign(self, subject: str) -> str:
        store = KeyStore(keys_dir=self.keys_dir, algorithm=KeyAlgorithm.ES256)
        store.initialize()
        return TokenSigner(store, issuer="aeos").sign(subject, audience=["aeos"], ttl_seconds=120)

    async def stop(self):
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        await self.con.stop()
        await self.transport.stop()


@pytest.mark.asyncio
async def test_three_process_cluster_dispatch_and_governance(tmp_path):
    keys_dir = str(tmp_path / "keys")
    # Generate the signing key ON DISK first so all worker processes load the
    # SAME key (no init race) and the scheduler signs with a key they trust.
    KeyStore(keys_dir=keys_dir, algorithm=KeyAlgorithm.ES256).initialize()

    sched = _Scheduler(keys_dir)
    await sched.start()
    try:
        await sched.add_workers(["worker-1", "worker-2", "worker-3"])

        # 1) Three signed tasks, one addressed to each worker process.
        for i, wid in enumerate(["worker-1", "worker-2", "worker-3"], start=1):
            token = sched.sign(f"task-{i}")
            await sched.pub.publish(
                _accepted(wid, token_id=f"task-{i}", raw_token=token, task_id=f"t{i}")
            )

        assert await _wait(lambda: set(sched.completed) >= {"t1", "t2", "t3"}), \
            f"completed={sched.completed}"

        # 2) Fail-closed governance across the process boundary: an unsigned
        #    task addressed to a production worker must never complete.
        before = len(sched.completed)
        await sched.pub.publish(_accepted("worker-1", task_id="t-unsigned"))
        await asyncio.sleep(1.0)
        assert "t-unsigned" not in sched.completed
        assert len(sched.completed) == before
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_worker_process_crash_leaves_cluster_serving(tmp_path):
    """Cross-process crash isolation: killing one worker process must not stop
    the surviving workers from executing addressed tasks, and a task addressed
    to the dead node must never phantom-complete."""
    keys_dir = str(tmp_path / "keys")
    KeyStore(keys_dir=keys_dir, algorithm=KeyAlgorithm.ES256).initialize()

    sched = _Scheduler(keys_dir)
    await sched.start()
    try:
        await sched.add_workers(["worker-1", "worker-2", "worker-3"])

        # Hard-kill worker-2 (simulated node crash). Its peer address remains in
        # the scheduler's peer set — publish must tolerate the dead RPC target.
        await sched.kill_worker("worker-2")

        # Signed tasks to the two survivors still complete over the wire.
        for i, wid in ((1, "worker-1"), (3, "worker-3")):
            token = sched.sign(f"task-{i}")
            await sched.pub.publish(
                _accepted(wid, token_id=f"task-{i}", raw_token=token, task_id=f"t{i}")
            )
        assert await _wait(lambda: set(sched.completed) >= {"t1", "t3"}), \
            f"completed={sched.completed}"

        # A signed task addressed to the DEAD worker never completes — no other
        # node answers for it (tasks are addressed by assigned_worker_id).
        token = sched.sign("task-2")
        await sched.pub.publish(
            _accepted("worker-2", token_id="task-2", raw_token=token, task_id="t2")
        )
        await asyncio.sleep(1.0)
        assert "t2" not in sched.completed
    finally:
        await sched.stop()
