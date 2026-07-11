"""
AEOS Orchestrator Engine
Central coordination layer. Owns the full task execution lifecycle:
  receive → route → dispatch → collect → validate → respond

Upgrade: intelligent intent-based routing, message bus events, shared memory
integration, fallback chain on agent failure.

Design rules:
  - Orchestrator never executes domain logic — agents do
  - All agent communication goes through this class
  - Every execution emits a fully structured OrchestratorResponse
  - Failures are typed and never silently swallowed
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.agents.base import BaseAgent, AgentResponse
from app.agents.context import build_context
from app.core.config import settings
from app.core.logger import get_logger, set_trace_context, new_trace_id
from app.core.memory import get_memory
from app.core.message_bus import AgentMessage, get_message_bus

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


# ── Intelligent routing keyword sets ──────────────────────────────────────────

_RESEARCH_KW = {"research", "find", "search", "look", "gather", "discover", "retrieve"}
_ANALYST_KW  = {"analyze", "analyse", "evaluate", "assess", "compare", "examine", "inspect"}
_EXECUTOR_KW = {"execute", "run", "deploy", "perform", "implement", "apply", "process"}
_PLANNER_KW  = {"plan", "decompose", "structure", "organize", "schedule", "breakdown"}

# If primary agent errors, try this agent once as fallback
_FALLBACK_CHAIN: dict[str, str] = {
    "research_agent": "simple_agent",
    "analyst_agent":  "simple_agent",
    "executor_agent": "simple_agent",
    "planner_agent":  "simple_agent",
}


# ── Response schema ────────────────────────────────────────────────────────────

class OrchestratorResponse:
    """
    Canonical response object returned to the API layer.
    Serialized to the documented JSON contract.
    """

    def __init__(
        self,
        task: str,
        agent: str,
        status: str,
        result,
        latency_ms: float,
        trace_id: str,
        error: str = "",
        thought: str = "",
    ) -> None:
        self.task = task
        self.agent = agent
        self.status = status
        self.result = result
        self.latency_ms = latency_ms
        self.trace_id = trace_id
        self.error = error
        self.thought = thought
        self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        payload = {
            "task": self.task,
            "agent": self.agent,
            "status": self.status,
            "result": self.result,
            "metadata": {
                "latency_ms": self.latency_ms,
                "timestamp": self.timestamp,
                "trace_id": self.trace_id,
            },
        }
        if self.error:
            payload["error"] = self.error
        if self.thought:
            payload["metadata"]["agent_thought"] = self.thought
        return payload


# ── Orchestrator ───────────────────────────────────────────────────────────────

class Orchestrator:
    """
    Core runtime engine for AEOS.

    Lifecycle:
        orchestrator = Orchestrator()
        await orchestrator.startup()     # called by FastAPI lifespan
        response = await orchestrator.run_task(task, mode)
        await orchestrator.shutdown()    # called by FastAPI lifespan
    """

    def __init__(self) -> None:
        self._registry: dict[str, BaseAgent] = {}
        self._ready: bool = False
        self._task_count: int = 0
        self._failure_count: int = 0
        log.info("Orchestrator instance created")

    # ── Startup / Shutdown ─────────────────────────────────────────────────────

    async def startup(self) -> None:
        log.info("Orchestrator starting up", extra={"ctx_agent_count": len(self._registry)})

        if not self._registry:
            log.warning("No agents registered — auto-loading SimpleAgent")
            from app.agents.simple_agent import SimpleAgent
            self.register(SimpleAgent())

        init_tasks = [agent.initialize() for agent in self._registry.values()]
        await asyncio.gather(*init_tasks)

        self._ready = True
        log.info("Orchestrator ready", extra={"ctx_agents": list(self._registry.keys())})

    async def shutdown(self) -> None:
        log.info(
            "Orchestrator shutting down",
            extra={"ctx_total_tasks": self._task_count, "ctx_total_failures": self._failure_count},
        )
        self._ready = False

    # ── Agent Registry ─────────────────────────────────────────────────────────

    def register(self, agent: BaseAgent) -> None:
        if agent.id in self._registry:
            raise ValueError(
                f"Agent ID '{agent.id}' is already registered. "
                "Agent IDs must be unique."
            )
        self._registry[agent.id] = agent
        log.info("Agent registered", extra={"ctx_agent_id": agent.id, "ctx_agent_name": agent.name})

    def get_agent(self, agent_id: str) -> BaseAgent | None:
        return self._registry.get(agent_id)

    def list_agents(self) -> list[dict]:
        return [agent.describe() for agent in self._registry.values()]

    # ── Task Execution Pipeline ────────────────────────────────────────────────

    async def run_task(self, task: str, mode: str = "single-agent") -> OrchestratorResponse:
        """
        Full execution pipeline:
          1. Validate readiness + assign trace context
          2. Publish task.started to message bus
          3. Inject short-term memory snapshot into context
          4. Route intelligently (single-agent) or delegate (multi-agent)
          5. Execute with timeout + fallback
          6. Write result to long-term memory
          7. Clear short-term memory for this task
          8. Publish task.completed / task.failed
        """
        if not self._ready:
            raise RuntimeError("Orchestrator not ready. Call startup() first.")

        trace_id = new_trace_id()
        task_id = str(uuid.uuid4())
        set_trace_context(trace_id=trace_id, task_id=task_id)

        self._task_count += 1
        t_start = time.perf_counter()
        bus = get_message_bus()
        memory = get_memory()

        log.info(
            "Task received",
            extra={"ctx_task_id": task_id, "ctx_mode": mode, "ctx_task_preview": task[:120]},
        )

        # Publish task.started
        await bus.publish(
            "task.started",
            AgentMessage(
                topic="task.started",
                sender_id="orchestrator",
                payload={"task": task[:200], "mode": mode, "task_id": task_id},
                trace_id=trace_id,
            ),
        )

        # Validate input
        if not task or not task.strip():
            resp = self._error_response(task=task, agent="none", trace_id=trace_id, error="Task string is empty.", latency_ms=0.0)
            await self._emit_failed(bus, trace_id, task, resp.error)
            return resp

        # Multi-agent explicit request
        if mode == "multi-agent":
            return await self._finalize_multi(task, trace_id, task_id, t_start, bus, memory)

        # Single-agent: intelligent routing
        mem_snapshot = memory.get_task_context(task_id)
        agent, routed_mode = self._route_intelligent(task)

        # Routing decided multi-agent
        if routed_mode == "multi-agent":
            return await self._finalize_multi(task, trace_id, task_id, t_start, bus, memory)

        if agent is None:
            resp = self._error_response(
                task=task, agent=settings.default_agent, trace_id=trace_id,
                error=f"No agent available. Registered: {list(self._registry.keys())}",
                latency_ms=0.0,
            )
            await self._emit_failed(bus, trace_id, task, resp.error)
            return resp

        log.info("Agent selected", extra={"ctx_agent_id": agent.id})

        agent_ctx = build_context(
            task=task, task_id=task_id, trace_id=trace_id,
            memory_snapshot=mem_snapshot, history=[],
            extra={"mode": mode},
        )
        context = {
            "original_task": task,
            "task_id": task_id,
            "trace_id": trace_id,
            "mode": mode,
            "agent_context": agent_ctx,
        }

        agent_response = await self._run_with_fallback(agent, task, context)
        total_latency = round((time.perf_counter() - t_start) * 1000, 2)

        if agent_response.status == "success" and agent_response.result:
            memory.write_long(
                key=task[:60],
                value=self._summarize_result(agent_response.result),
                agent_id=agent_response.agent_id,
                task_id=task_id,
            )

        memory.clear_task(task_id)

        if agent_response.status == "failed":
            self._failure_count += 1
            await self._emit_failed(bus, trace_id, task, agent_response.error)
        else:
            await bus.publish(
                "task.completed",
                AgentMessage(
                    topic="task.completed",
                    sender_id="orchestrator",
                    payload={
                        "task": task[:200],
                        "agent": agent_response.agent_id,
                        "status": "success",
                        "latency_ms": total_latency,
                    },
                    trace_id=trace_id,
                ),
            )

        log.info(
            "Task complete",
            extra={"ctx_status": agent_response.status, "ctx_agent_id": agent_response.agent_id, "ctx_latency_ms": total_latency},
        )

        return OrchestratorResponse(
            task=task,
            agent=agent_response.agent_id,
            status=agent_response.status,
            result=agent_response.result,
            latency_ms=total_latency,
            trace_id=trace_id,
            error=agent_response.error,
            thought=agent_response.thought,
        )

    # ── Intelligent routing ────────────────────────────────────────────────────

    def _route_intelligent(self, task: str) -> tuple[BaseAgent | None, str]:
        """
        Analyze task intent via keyword matching and return (agent, mode).
        Returns mode="multi-agent" when multiple intents are detected or task is complex.
        """
        words = set(task.lower().split())

        intents: dict[str, str] = {}  # intent_label → agent_id
        if words & _RESEARCH_KW:
            intents["research"] = "research_agent"
        if words & _ANALYST_KW:
            intents["analyze"] = "analyst_agent"
        if words & _EXECUTOR_KW:
            intents["execute"] = "executor_agent"
        if words & _PLANNER_KW:
            intents["plan"] = "planner_agent"

        # Two or more intents, or single intent with long/complex task → multi-agent
        if len(intents) >= 2 or (len(intents) == 1 and len(task) > 150):
            return None, "multi-agent"

        if intents:
            agent_id = next(iter(intents.values()))
            agent = self._registry.get(agent_id)
            if agent:
                log.debug("Intelligent route", extra={"ctx_intent": list(intents.keys()), "ctx_agent": agent_id})
                return agent, "single-agent"

        # Default fallback
        agent = self._registry.get(settings.default_agent) or (
            next(iter(self._registry.values())) if self._registry else None
        )
        return agent, "single-agent"

    # ── Fallback execution ─────────────────────────────────────────────────────

    async def _run_with_fallback(self, agent: BaseAgent, task: str, context: dict) -> AgentResponse:
        """Execute agent; on failure try the fallback agent once."""
        response = await self._execute_agent(agent, task, context)

        if response.status == "failed":
            fallback_id = _FALLBACK_CHAIN.get(agent.id)
            fallback = self._registry.get(fallback_id) if fallback_id else None
            if fallback and fallback.id != agent.id:
                log.warning(
                    "Primary agent failed — trying fallback",
                    extra={"ctx_primary": agent.id, "ctx_fallback": fallback.id, "ctx_error": response.error},
                )
                fb_response = await self._execute_agent(fallback, task, context)
                if fb_response.status == "success":
                    return fb_response

        return response

    async def _execute_agent(self, agent: BaseAgent, task: str, context: dict) -> AgentResponse:
        """Run a single agent with timeout protection."""
        try:
            return await asyncio.wait_for(
                agent.run(task, context),
                timeout=settings.agent_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.error("Agent timed out", extra={"ctx_agent_id": agent.id, "ctx_timeout_s": settings.agent_timeout_seconds})
            return AgentResponse(
                agent_id=agent.id,
                agent_name=agent.name,
                status="failed",
                result=None,
                error=f"Agent '{agent.id}' exceeded timeout of {settings.agent_timeout_seconds}s.",
                latency_ms=settings.agent_timeout_seconds * 1000.0,
            )

    # ── Multi-agent DAG execution ──────────────────────────────────────────────

    async def _finalize_multi(
        self,
        task: str,
        trace_id: str,
        task_id: str,
        t_start: float,
        bus,
        memory,
    ) -> OrchestratorResponse:
        """Wrapper: run multi-agent, handle memory + bus events."""
        result = await self._run_multi_agent(task, trace_id, task_id, t_start, bus, memory)
        if result.status == "success" and result.result:
            memory.write_long(
                key=task[:60],
                value=self._summarize_result(result.result),
                agent_id="multi-agent-pipeline",
                task_id=task_id,
            )
            await bus.publish(
                "task.completed",
                AgentMessage(
                    topic="task.completed",
                    sender_id="orchestrator",
                    payload={"task": task[:200], "agent": result.agent, "status": "success", "latency_ms": result.latency_ms},
                    trace_id=trace_id,
                ),
            )
        else:
            await self._emit_failed(bus, trace_id, task, result.error)
        memory.clear_task(task_id)
        return result

    async def _run_multi_agent(
        self,
        task: str,
        trace_id: str,
        task_id: str,
        t_start: float,
        bus,
        memory,
    ) -> OrchestratorResponse:
        """
        Multi-agent pipeline:
          1. PlannerAgent → DAG of subtasks
          2. Execute subtasks in topological order
          3. ReviewerAgent validates output
          4. On REVISE: retry up to max_retries times
        """
        planner = self._registry.get("planner_agent")
        reviewer = self._registry.get("reviewer_agent")

        if not planner:
            return self._error_response(
                task=task, agent="multi-agent-pipeline", trace_id=trace_id,
                error="planner_agent not registered. Cannot run multi-agent mode.",
                latency_ms=0.0,
            )

        history: list[dict] = []
        context: dict = {
            "original_task": task,
            "task_id": task_id,
            "trace_id": trace_id,
            "mode": "multi-agent",
            "step_results": {},
            "revision_round": 0,
        }

        # Plan
        log.info("Multi-agent: planning phase")
        plan_ctx = build_context(
            task=task, task_id=task_id, trace_id=trace_id,
            memory_snapshot=memory.get_task_context(task_id),
            history=history, extra={"mode": "multi-agent"},
        )
        context["agent_context"] = plan_ctx
        plan_response = await planner.run(task, context)

        if plan_response.status == "failed":
            return self._error_response(
                task=task, agent="planner_agent", trace_id=trace_id,
                error=f"Planning failed: {plan_response.error}",
                latency_ms=round((time.perf_counter() - t_start) * 1000, 2),
            )

        plan = plan_response.result
        context["plan"] = plan
        history.append({"planner": plan})

        # Retry loop
        review_response = None
        for revision_round in range(settings.max_retries + 1):
            context["revision_round"] = revision_round
            subtasks = plan.get("subtasks", [])
            ordered = self._topological_sort(subtasks)

            for subtask in ordered:
                agent_id = subtask["agent"]
                agent = self._registry.get(agent_id)
                if agent is None:
                    log.warning("Subtask agent not found", extra={"ctx_agent_id": agent_id})
                    continue

                agent_ctx = build_context(
                    task=task, task_id=task_id, trace_id=trace_id,
                    memory_snapshot=memory.get_task_context(task_id),
                    history=history,
                    extra={"mode": "multi-agent", "subtask": subtask},
                )
                step_context = {
                    **context,
                    "subtask": subtask,
                    "agent_context": agent_ctx,
                    "previous_result": history[-1] if history else None,
                }

                log.info("Executing subtask", extra={"ctx_subtask_id": subtask["id"], "ctx_agent": agent_id})
                step_response = await agent.run(task, step_context)
                context["step_results"][subtask["id"]] = step_response.result
                history.append({subtask["id"]: step_response.result})

                memory.write_short(task_id, f"step_{subtask['id']}", step_response.result, agent_id=agent_id)

                await bus.publish(
                    "agent.result",
                    AgentMessage(
                        topic="agent.result",
                        sender_id=agent_id,
                        payload={"subtask_id": subtask["id"], "status": step_response.status},
                        trace_id=trace_id,
                    ),
                )

            # Review
            aggregated = {"plan": plan, "step_results": context["step_results"], "task": task}
            if reviewer:
                review_ctx = build_context(
                    task=task, task_id=task_id, trace_id=trace_id,
                    memory_snapshot=memory.get_task_context(task_id),
                    history=history, extra={"mode": "review"},
                )
                review_context = {**context, "previous_result": aggregated, "agent_context": review_ctx}
                review_response = await reviewer.run(task, review_context)
                verdict = review_response.result.get("verdict", "PASS") if review_response.result else "PASS"
                context["review"] = review_response.result
            else:
                verdict = "PASS"

            log.info("Multi-agent round complete", extra={"ctx_verdict": verdict, "ctx_round": revision_round})

            if verdict == "PASS":
                break
            if verdict == "REJECT":
                self._failure_count += 1
                latency = round((time.perf_counter() - t_start) * 1000, 2)
                return OrchestratorResponse(
                    task=task, agent="multi-agent-pipeline", status="failed",
                    result=aggregated, latency_ms=latency, trace_id=trace_id,
                    error="Reviewer rejected output.",
                )
            if review_response and review_response.result:
                context["revision_hints"] = review_response.result.get("revision_hints", [])

        total_latency = round((time.perf_counter() - t_start) * 1000, 2)
        final_result = {
            "plan": plan,
            "step_results": context["step_results"],
            "review": context.get("review"),
            "revision_rounds": context["revision_round"],
        }
        log.info("Multi-agent task complete", extra={"ctx_latency_ms": total_latency, "ctx_steps": len(context["step_results"])})
        return OrchestratorResponse(
            task=task, agent="multi-agent-pipeline", status="success",
            result=final_result, latency_ms=total_latency, trace_id=trace_id,
        )

    def _topological_sort(self, subtasks: list[dict]) -> list[dict]:
        """Kahn's algorithm — returns subtasks ordered by dependency."""
        id_map = {s["id"]: s for s in subtasks}
        in_degree = {s["id"]: 0 for s in subtasks}
        dependents: dict[str, list[str]] = {s["id"]: [] for s in subtasks}

        for s in subtasks:
            for dep in s.get("depends_on", []):
                if dep in in_degree:
                    in_degree[s["id"]] += 1
                    dependents[dep].append(s["id"])

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        ordered = []
        while queue:
            queue.sort(key=lambda sid: id_map[sid].get("priority", 99))
            current = queue.pop(0)
            ordered.append(id_map[current])
            for dep_id in dependents[current]:
                in_degree[dep_id] -= 1
                if in_degree[dep_id] == 0:
                    queue.append(dep_id)

        return ordered

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _emit_failed(self, bus, trace_id: str, task: str, error: str) -> None:
        await bus.publish(
            "task.failed",
            AgentMessage(
                topic="task.failed",
                sender_id="orchestrator",
                payload={"task": task[:200], "error": error},
                trace_id=trace_id,
            ),
        )

    @staticmethod
    def _summarize_result(result) -> dict:
        """Compact, memory-storable summary of an agent result."""
        if result is None:
            return {}
        if isinstance(result, dict):
            summary = {}
            for k in ("summary", "conclusions", "synthesis", "recommendation", "verdict", "execution_status"):
                if k in result:
                    summary[k] = result[k]
            return summary or {k: result[k] for k in list(result)[:3]}
        return {"result": str(result)[:200]}

    def _error_response(self, task: str, agent: str, trace_id: str, error: str, latency_ms: float) -> OrchestratorResponse:
        self._failure_count += 1
        log.error("Orchestrator error", extra={"ctx_error": error})
        return OrchestratorResponse(
            task=task, agent=agent, status="failed", result=None,
            latency_ms=latency_ms, trace_id=trace_id, error=error,
        )

    # ── Introspection ──────────────────────────────────────────────────────────

    def state(self) -> dict:
        memory = get_memory()
        bus = get_message_bus()
        return {
            "ready": self._ready,
            "task_count": self._task_count,
            "failure_count": self._failure_count,
            "default_agent": settings.default_agent,
            "agents": self.list_agents(),
            "config": {
                "environment": settings.environment,
                "agent_timeout_seconds": settings.agent_timeout_seconds,
                "max_retries": settings.max_retries,
                "debug": settings.debug,
            },
            "memory": memory.summarize(),
            "message_bus": bus.summarize(),
        }
