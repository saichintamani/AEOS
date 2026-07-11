"""
AEOS — Agent Context Protocol
Strict schema for all agent communication. Every agent receives an AgentContext
instance injected into the context dict under the key "agent_context".

Schema:
{
    "task_id":  str,
    "input":    str,      # raw task string
    "context":  dict,     # orchestrator metadata (trace_id, mode, plan, ...)
    "memory":   dict,     # short-term memory snapshot for this task
    "history":  list[dict]  # previous agent results in this pipeline run
}
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentContext:
    """
    Canonical input schema passed to every agent.

    Populated by the orchestrator via build_context() before each agent.run() call.
    Agents read this from context["agent_context"] — they must not assume any other
    structure in the context dict (though extra keys may exist for backward compat).
    """
    task_id: str
    input: str
    context: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "input": self.input,
            "context": self.context,
            "memory": self.memory,
            "history": self.history,
        }

    def last_result(self) -> Any:
        """Returns the most recent entry in history, or None."""
        return self.history[-1] if self.history else None


def build_context(
    task: str,
    task_id: str,
    trace_id: str,
    memory_snapshot: dict,
    history: list[dict],
    extra: dict | None = None,
) -> AgentContext:
    """
    Factory called by the Orchestrator before every agent.run() call.

    Args:
        task:             The raw user task string.
        task_id:          UUID for this task execution.
        trace_id:         Distributed trace ID for this request.
        memory_snapshot:  Result of memory.get_task_context(task_id).
        history:          List of previous step result dicts for this pipeline run.
        extra:            Any additional orchestrator metadata to include.

    Returns:
        AgentContext ready to be injected as context["agent_context"].
    """
    ctx_meta = {"trace_id": trace_id, "task_id": task_id}
    if extra:
        ctx_meta.update(extra)
    return AgentContext(
        task_id=task_id,
        input=task,
        context=ctx_meta,
        memory=memory_snapshot,
        history=history,
    )
