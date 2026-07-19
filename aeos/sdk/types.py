"""AEOS SDK — Public types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentConfig:
    """Configuration for a single agent."""
    agent_id: str
    timeout_s: int = 60
    retry: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RunResult:
    """Result of a single task submission."""
    status: str                     # "success" | "partial" | "failed"
    agent_id: str = ""
    result: str = ""
    trace_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "RunResult":
        return cls(
            status=data.get("status", "failed"),
            agent_id=data.get("agent_id", data.get("agent", "")),
            result=str(data.get("result") or data.get("response") or ""),
            trace_id=data.get("trace_id", ""),
            raw=data,
        )


@dataclass
class StepResult:
    step_name: str
    run_result: RunResult


@dataclass
class WorkflowResult:
    """Result of a compiled workflow execution."""
    workflow_name: str
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.run_result.ok for s in self.steps)

    @property
    def final_result(self) -> str:
        if self.steps:
            return self.steps[-1].run_result.result
        return ""
