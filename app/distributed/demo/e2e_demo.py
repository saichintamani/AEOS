"""
Wave 9B.5.10 — End-to-End Distributed Demo

Simulates a full multi-worker AEOS cluster in-process:
  • Coordinator   — LeaderScheduler + CapabilityFederator + Raft leader
  • Research      — LLM worker (claude-3-opus)
  • Planner       — LLM + Planning worker
  • Reasoner      — LLM + Planning worker
  • Reviewer      — LLM + ToolCall worker
  • MemoryStore   — Memory + RAG + Search worker

Workflow: "Multi-step research pipeline"
  Step 1: Research  (assigned to Research worker)
  Step 2: Plan      (assigned to Planner)
  Step 3: Reason    (assigned to Reasoner, depends on Plan)
  Step 4: Review    (assigned to Reviewer, depends on Reason)

All transport is in-memory (no Kafka/Redis/gRPC required).
All workers run as asyncio tasks with simulated handlers.

Usage::
    python -m app.distributed.demo.e2e_demo
    # or from tests: from app.distributed.demo.e2e_demo import run_demo; asyncio.run(run_demo())
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.distributed.capability.federation import (
    CapabilityAdvertisement,
    CapabilityCategory,
    CapabilityFederator,
    LLMCapability,
    MemoryCapability,
)
from app.distributed.consensus.raft import RaftNode, RaftRole
from app.distributed.fault.injector import FaultInjector, FaultType
from app.distributed.fault.scenarios import WorkerCrashScenario
from app.distributed.scheduler.distributed_scheduler import LeaderScheduler
from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements

logger = logging.getLogger(__name__)


# ── Worker definitions ────────────────────────────────────────────────────────

@dataclass
class SimWorker:
    """In-process simulated worker with a task handler."""
    worker_id: str
    advertisement: CapabilityAdvertisement
    tasks_completed: int = 0
    tasks_failed: int = 0
    _alive: bool = field(default=True, repr=False)

    async def handle(self, task: TaskRequirements, context: dict) -> dict:
        """Simulate task execution with latency."""
        if not self._alive:
            raise RuntimeError(f"Worker {self.worker_id} is dead")
        # Simulate 10–50ms execution
        await asyncio.sleep(0.01 + (hash(task.task_id) % 4) * 0.01)
        self.tasks_completed += 1
        return {
            "worker_id": self.worker_id,
            "task_id": task.task_id,
            "task_type": task.task_type,
            "result": f"{task.task_type} completed by {self.worker_id}",
        }

    def crash(self) -> None:
        self._alive = False

    def revive(self) -> None:
        self._alive = True


def _make_workers() -> dict[str, SimWorker]:
    return {
        "research": SimWorker(
            worker_id="research",
            advertisement=CapabilityAdvertisement(
                worker_id="research",
                cpu_cores=8, memory_gb=32.0, current_load=0.1,
                llm=LLMCapability(
                    models=["claude-3-opus"],
                    max_context_tokens=200_000,
                    supports_function_calling=False,
                ),
                has_search=True,
                skills=frozenset(["research", "web_search"]),
            ),
        ),
        "planner": SimWorker(
            worker_id="planner",
            advertisement=CapabilityAdvertisement(
                worker_id="planner",
                cpu_cores=8, memory_gb=16.0, current_load=0.1,
                llm=LLMCapability(
                    models=["claude-3-sonnet"],
                    max_context_tokens=100_000,
                    supports_function_calling=True,
                ),
                has_planning=True,
                skills=frozenset(["planning", "task_decomposition"]),
            ),
        ),
        "reasoner": SimWorker(
            worker_id="reasoner",
            advertisement=CapabilityAdvertisement(
                worker_id="reasoner",
                cpu_cores=8, memory_gb=16.0, current_load=0.1,
                llm=LLMCapability(
                    models=["claude-3-sonnet"],
                    max_context_tokens=100_000,
                    supports_function_calling=True,
                ),
                has_planning=True,
                skills=frozenset(["reasoning", "chain_of_thought"]),
            ),
        ),
        "reviewer": SimWorker(
            worker_id="reviewer",
            advertisement=CapabilityAdvertisement(
                worker_id="reviewer",
                cpu_cores=4, memory_gb=8.0, current_load=0.2,
                llm=LLMCapability(
                    models=["claude-3-haiku"],
                    max_context_tokens=50_000,
                    supports_function_calling=True,
                ),
                has_tool_call=True,
                skills=frozenset(["review", "quality_check"]),
            ),
        ),
        "memory": SimWorker(
            worker_id="memory",
            advertisement=CapabilityAdvertisement(
                worker_id="memory",
                cpu_cores=4, memory_gb=64.0, current_load=0.3,
                memory_store=MemoryCapability(
                    backend="pgvector",
                    max_vectors=1_000_000,
                    supports_hybrid_search=True,
                ),
                has_rag=True,
                has_search=True,
                skills=frozenset(["memory_retrieval", "vector_search"]),
            ),
        ),
    }


# ── Raft cluster ──────────────────────────────────────────────────────────────

def _make_raft_cluster() -> dict[str, RaftNode]:
    node_ids = ["coord-1", "coord-2", "coord-3"]
    nodes: dict[str, RaftNode] = {}

    def make_rpc(nid: str):
        async def rpc(target_id: str, method: str, payload: Any) -> Any:
            t = nodes.get(target_id)
            if t is None:
                raise ConnectionError(f"Raft node {target_id} unreachable")
            if method == "request_vote":
                return await t.handle_vote_request(payload)
            if method == "append_entries":
                return await t.handle_append_entries(payload)
            raise ValueError(f"Unknown Raft method: {method}")
        return rpc

    for nid in node_ids:
        peers = [p for p in node_ids if p != nid]
        nodes[nid] = RaftNode(node_id=nid, peers=peers, rpc_send=make_rpc(nid))

    return nodes


# ── Demo result ───────────────────────────────────────────────────────────────

@dataclass
class DemoResult:
    workflow_id: str
    tasks_submitted: int = 0
    tasks_succeeded: int = 0
    tasks_failed: int = 0
    total_latency_ms: float = 0.0
    worker_utilization: dict[str, int] = field(default_factory=dict)
    raft_leader: str = ""
    workers_registered: int = 0
    federation_profiles: int = 0
    chaos_events: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        if self.tasks_submitted == 0:
            return 0.0
        return self.tasks_succeeded / self.tasks_submitted


# ── Main demo orchestration ───────────────────────────────────────────────────

class DistributedDemo:
    """
    Orchestrates the full E2E distributed demo.

    Phase 1: Bootstrap (Raft election + worker registration + capability federation)
    Phase 2: Execute research pipeline (5-task workflow)
    Phase 3: Chaos test (crash one worker, verify recovery)
    Phase 4: Collect and return metrics
    """

    def __init__(self) -> None:
        self.workers = _make_workers()
        self.raft_nodes = _make_raft_cluster()
        self.federator = CapabilityFederator()
        self.scheduler = LeaderScheduler()
        self.fault_injector = FaultInjector()
        self._results: list[dict] = []

    async def bootstrap(self) -> str:
        """Phase 1: elect a Raft leader, register workers, advertise capabilities."""
        logger.info("=== Phase 1: Bootstrap ===")

        # Raft election
        leader_node = self.raft_nodes["coord-1"]
        await leader_node._start_election()
        assert leader_node.role == RaftRole.LEADER
        leader_id = leader_node.leader_id
        logger.info("Raft leader elected: %s (term=%d)", leader_id, leader_node.term)

        # Propose initial cluster config to Raft log
        await leader_node.propose({
            "op": "cluster_init",
            "workers": list(self.workers.keys()),
        })

        # Register workers with scheduler + advertise capabilities
        for wid, worker in self.workers.items():
            profile = worker.advertisement.to_capability_profile()
            self.scheduler.register_worker(profile)
            await self.federator.advertise(worker.advertisement)
            logger.info("Worker registered: %s (categories=%s)",
                        wid, {c.value for c in worker.advertisement.categories})

        return leader_id

    async def run_workflow(self, workflow_id: str) -> list[dict]:
        """Phase 2: submit a 5-task research pipeline."""
        logger.info("=== Phase 2: Execute Workflow '%s' ===", workflow_id)

        tasks = [
            TaskRequirements(
                task_id="research-001",
                task_type="research",
                required_skills=frozenset(["research"]),
                priority="high",
                workflow_id=workflow_id,
            ),
            TaskRequirements(
                task_id="plan-001",
                task_type="planning",
                required_skills=frozenset(["planning"]),
                priority="normal",
                workflow_id=workflow_id,
            ),
            TaskRequirements(
                task_id="reason-001",
                task_type="reasoning",
                required_skills=frozenset(["reasoning"]),
                priority="normal",
                workflow_id=workflow_id,
            ),
            TaskRequirements(
                task_id="review-001",
                task_type="review",
                required_skills=frozenset(["review"]),
                priority="normal",
                workflow_id=workflow_id,
            ),
            TaskRequirements(
                task_id="memory-001",
                task_type="memory_retrieval",
                required_skills=frozenset(["memory_retrieval"]),
                priority="low",
                workflow_id=workflow_id,
            ),
        ]

        results = []
        for req in tasks:
            ok, reason = await self.scheduler.submit(req)
            if ok:
                # Find which worker got the task
                ws = self.scheduler._worker_schedulers
                for wid, w_sched in ws.items():
                    if w_sched.queue_depth > 0:
                        task = await w_sched.next()
                        if task and task.requirements.task_id == req.task_id:
                            worker = self.workers.get(wid)
                            if worker:
                                result = await worker.handle(req, {})
                                results.append(result)
                                logger.info(
                                    "Task %s → %s: %s",
                                    req.task_id, wid, result["result"],
                                )
                            break
            else:
                logger.warning("Task %s rejected: %s", req.task_id, reason)
                results.append({"task_id": req.task_id, "error": reason})

        return results

    async def run_chaos(self) -> list[str]:
        """Phase 3: crash the research worker, verify cluster adapts."""
        logger.info("=== Phase 3: Chaos Testing ===")
        events = []

        async with WorkerCrashScenario(self.fault_injector) as scenario:
            fired = await self.fault_injector.should_inject(FaultType.WORKER_CRASH)
            if fired:
                # Crash the research worker
                self.workers["research"].crash()
                self.scheduler._worker_schedulers.pop("research", None)
                self.scheduler._profiles = [
                    p for p in self.scheduler._profiles if p.worker_id != "research"
                ]
                await self.federator.withdraw("research")
                events.append("WORKER_CRASH:research")
                logger.info("CHAOS: research worker crashed")

                # Verify remaining workers still handle tasks
                stats = self.scheduler.cluster_stats()
                assert stats["workers"] == len(self.workers) - 1
                events.append(f"CLUSTER_SIZE:{stats['workers']}")

                # Re-submit task that would have gone to research
                req = TaskRequirements(
                    task_id="fallback-001",
                    task_type="planning",
                    required_skills=frozenset(["planning"]),
                    priority="critical",
                )
                ok, reason = await self.scheduler.submit(req)
                if ok:
                    events.append("FALLBACK_TASK:ok")
                    logger.info("CHAOS: fallback task dispatched to surviving workers")
                else:
                    events.append(f"FALLBACK_TASK:failed:{reason}")

        return events

    async def collect_metrics(self, leader_id: str, task_results: list[dict]) -> DemoResult:
        """Phase 4: collect final cluster metrics."""
        logger.info("=== Phase 4: Metrics ===")

        profiles = await self.federator.profiles()
        stats = self.scheduler.cluster_stats()

        succeeded = sum(1 for r in task_results if "error" not in r)
        failed = sum(1 for r in task_results if "error" in r)

        utilization = {}
        for r in task_results:
            if "worker_id" in r:
                wid = r["worker_id"]
                utilization[wid] = utilization.get(wid, 0) + 1

        return DemoResult(
            workflow_id="research-pipeline-v1",
            tasks_submitted=len(task_results),
            tasks_succeeded=succeeded,
            tasks_failed=failed,
            worker_utilization=utilization,
            raft_leader=leader_id,
            workers_registered=stats["workers"],
            federation_profiles=len(profiles),
        )


async def run_demo(verbose: bool = True) -> DemoResult:
    """
    Run the complete E2E distributed demo.

    Returns DemoResult with cluster metrics and outcomes.
    """
    if verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )

    demo = DistributedDemo()

    # Phase 1: Bootstrap
    leader_id = await demo.bootstrap()

    # Phase 2: Run workflow
    t0 = time.monotonic()
    task_results = await demo.run_workflow("research-pipeline-v1")
    latency_ms = (time.monotonic() - t0) * 1000

    # Phase 3: Chaos
    chaos_events = await demo.run_chaos()

    # Phase 4: Metrics
    result = await demo.collect_metrics(leader_id, task_results)
    result.total_latency_ms = latency_ms
    result.chaos_events = chaos_events

    if verbose:
        print("\n" + "=" * 60)
        print("AEOS Distributed Runtime — E2E Demo Results")
        print("=" * 60)
        print(f"Workflow:          {result.workflow_id}")
        print(f"Raft Leader:       {result.raft_leader}")
        print(f"Workers Active:    {result.workers_registered}")
        print(f"Federation Profiles: {result.federation_profiles}")
        print(f"Tasks Submitted:   {result.tasks_submitted}")
        print(f"Tasks Succeeded:   {result.tasks_succeeded}")
        print(f"Tasks Failed:      {result.tasks_failed}")
        print(f"Success Rate:      {result.success_rate:.1%}")
        print(f"Total Latency:     {result.total_latency_ms:.1f}ms")
        print(f"Worker Utilization: {result.worker_utilization}")
        print(f"Chaos Events:      {result.chaos_events}")
        print("=" * 60)

    return result


if __name__ == "__main__":
    asyncio.run(run_demo())
