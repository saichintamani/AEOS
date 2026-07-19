"""
AEOS SDK — WorkflowBuilder

Fluent API for constructing workflows programmatically
instead of writing YAML.

Usage::

    from aeos.sdk import WorkflowBuilder

    workflow = (
        WorkflowBuilder("research-pipeline")
        .add_step("plan", "Create a research plan for: {query}", agent="planner")
        .add_step("research", "Research: {query}", mode="multi-agent")
        .add_step("review", "Review the research output.", agent="reviewer", depends_on=["research"])
        .build()
    )

    # Compile with variable substitution
    compiled = WorkflowBuilder.compile(workflow, query="What is Raft consensus?")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class _StepDef:
    name: str
    task: str
    mode: str = "single-agent"
    agent: str = ""
    depends_on: list[str] = field(default_factory=list)
    timeout_s: int = 60
    retry: int = 0


class WorkflowBuilder:
    """Fluent builder for AEOS workflows."""

    def __init__(self, name: str, description: str = "") -> None:
        self._name = name
        self._description = description
        self._steps: list[_StepDef] = []
        self._agents: list[str] = []

    def agents(self, *agent_ids: str) -> "WorkflowBuilder":
        """Declare which agents this workflow uses."""
        self._agents.extend(agent_ids)
        return self

    def add_step(
        self,
        name: str,
        task: str,
        *,
        mode: str = "single-agent",
        agent: str = "",
        depends_on: list[str] | None = None,
        timeout_s: int = 60,
        retry: int = 0,
    ) -> "WorkflowBuilder":
        """Add a step to the workflow."""
        self._steps.append(_StepDef(
            name=name,
            task=task,
            mode=mode,
            agent=agent,
            depends_on=depends_on or [],
            timeout_s=timeout_s,
            retry=retry,
        ))
        return self

    def build(self) -> dict[str, Any]:
        """Build the raw workflow dict (YAML-equivalent structure)."""
        return {
            "workflow": {
                "name": self._name,
                "description": self._description,
                "agents": self._agents,
                "steps": [
                    {
                        "name": s.name,
                        "task": s.task,
                        "mode": s.mode,
                        "agent": s.agent,
                        "depends_on": s.depends_on,
                        "timeout_s": s.timeout_s,
                        "retry": s.retry,
                    }
                    for s in self._steps
                ],
            }
        }

    def compile(self, variables: dict[str, str] | None = None) -> dict:
        """Build and compile the workflow, optionally interpolating variables."""
        from aeos.workflow.compiler import WorkflowCompiler
        raw = self.build()
        return WorkflowCompiler().compile(raw, variables=variables or {})
