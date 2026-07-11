"""
AEOS Base Agent
All agents inherit from BaseAgent. The contract is strict:
- Every method is async
- run() is the single public entry point called by the Orchestrator
- think() and act() are internal steps that subclasses override
- Every agent logs every step with its agent_id in context
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
import time

from app.core.logger import get_logger, set_trace_context

log = get_logger(__name__)


@dataclass
class AgentResponse:
    """
    Standardized output every agent must return from run().
    The Orchestrator reads this directly — no plain-text returns allowed.
    """
    agent_id: str
    agent_name: str
    status: str                    # "success" | "failed"
    result: Any                    # structured payload, never raw strings
    thought: str = ""              # agent's internal reasoning summary
    error: str = ""                # populated on failure
    latency_ms: float = 0.0
    metadata: dict = field(default_factory=dict)


class BaseAgent(ABC):
    """
    Abstract base for all AEOS agents.

    Subclass contract:
        1. Set self.id and self.name in __init__
        2. Declare self.capabilities list
        3. Implement think() — pure reasoning, no side effects
        4. Implement act() — execution, tool calls, LLM calls
        5. Do NOT override run() — it owns the lifecycle
    """

    def __init__(self) -> None:
        self.id: str = ""
        self.name: str = ""
        self.capabilities: list[str] = []
        self._initialized: bool = False
        self._log = get_logger(f"agent.{self.__class__.__name__}")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """
        Called once by the Orchestrator after registration.
        Load models, warm caches, validate config here.
        Subclasses should call super().initialize() first.
        """
        self._log.info(
            "Agent initializing",
            extra={"ctx_agent": self.id, "ctx_capabilities": self.capabilities},
        )
        self._initialized = True
        self._log.info("Agent ready", extra={"ctx_agent": self.id})

    # ── Primary entry point ───────────────────────────────────────────────────

    async def run(self, task: str, context: dict) -> AgentResponse:
        """
        Orchestrator calls this. Do NOT override.
        Enforces the think → act lifecycle and wraps timing + error handling.
        """
        if not self._initialized:
            raise RuntimeError(
                f"Agent '{self.id}' was not initialized. "
                "Call await agent.initialize() before use."
            )

        set_trace_context(agent_id=self.id)
        self._log.info("Agent run started", extra={"ctx_task_preview": task[:120]})
        t_start = time.perf_counter()

        try:
            thought = await self.think(task)
            self._log.debug("Think phase complete", extra={"ctx_thought": thought})

            result = await self.act(thought, context)
            self._log.debug("Act phase complete")

            latency_ms = (time.perf_counter() - t_start) * 1000
            self._log.info(
                "Agent run succeeded",
                extra={"ctx_latency_ms": round(latency_ms, 2)},
            )

            return AgentResponse(
                agent_id=self.id,
                agent_name=self.name,
                status="success",
                result=result,
                thought=thought,
                latency_ms=round(latency_ms, 2),
            )

        except Exception as exc:
            latency_ms = (time.perf_counter() - t_start) * 1000
            self._log.exception(
                "Agent run failed",
                extra={"ctx_error": str(exc), "ctx_latency_ms": round(latency_ms, 2)},
            )
            return AgentResponse(
                agent_id=self.id,
                agent_name=self.name,
                status="failed",
                result=None,
                error=str(exc),
                latency_ms=round(latency_ms, 2),
            )

    # ── Abstract steps ────────────────────────────────────────────────────────

    @abstractmethod
    async def think(self, task: str) -> str:
        """
        Reasoning phase. Analyze the task, decide what to do.
        Returns a string summarizing the agent's plan / interpretation.
        No side effects — pure cognitive step.
        """
        ...

    @abstractmethod
    async def act(self, processed_input: str, context: dict) -> Any:
        """
        Execution phase. Carry out the plan from think().
        May call tools, LLMs, APIs, or run ML code.
        Returns structured output (dict preferred).
        """
        ...

    # ── Introspection ─────────────────────────────────────────────────────────

    def describe(self) -> dict:
        """Returns agent metadata for /debug/state and registry introspection."""
        return {
            "id": self.id,
            "name": self.name,
            "capabilities": self.capabilities,
            "initialized": self._initialized,
        }

    def __repr__(self) -> str:
        return f"<Agent id={self.id!r} name={self.name!r}>"
