"""
Phase 10 — AEOS Workflow DSL Compiler

Converts a YAML workflow definition into a validated intermediate
representation that can be submitted to the AEOS execution engine.

YAML Schema::

    workflow:
      name: my-workflow                 # required
      description: optional text
      version: "1.0"                    # optional, default "1.0"
      agents:                           # optional: declare which agents are used
        - planner
        - researcher
      steps:                            # required: ordered list of steps
        - name: plan                    # optional step name (auto-generated if absent)
          task: "Do X with {var}"       # required: task text (supports {var} interpolation)
          mode: single-agent            # optional: single-agent | multi-agent (default single-agent)
          agent: planner                # optional: route to a specific agent
          depends_on: []                # optional: step names this step waits for
          timeout_s: 60                 # optional: per-step timeout in seconds
          retry: 0                      # optional: retry count on failure

Compiled output is a dict::

    {
      "name": "my-workflow",
      "description": "...",
      "version": "1.0",
      "steps": [
        {
          "id": "step-0",
          "name": "plan",
          "task": "Do X with value",
          "mode": "single-agent",
          "agent": "planner",
          "depends_on": [],
          "timeout_s": 60,
          "retry": 0,
        },
        ...
      ]
    }
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


class WorkflowValidationError(Exception):
    """Raised when the YAML workflow definition is invalid."""


@dataclass
class WorkflowStep:
    id: str
    name: str
    task: str
    mode: str = "single-agent"
    agent: str = ""
    depends_on: list[str] = field(default_factory=list)
    timeout_s: int = 60
    retry: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "task": self.task,
            "mode": self.mode,
            "agent": self.agent,
            "depends_on": self.depends_on,
            "timeout_s": self.timeout_s,
            "retry": self.retry,
        }


@dataclass
class CompiledWorkflow:
    name: str
    description: str
    version: str
    steps: list[WorkflowStep]
    agents: list[str]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "agents": self.agents,
            "steps": [s.to_dict() for s in self.steps],
        }


class WorkflowCompiler:
    """
    Compiles a YAML workflow dict into a validated CompiledWorkflow.

    Usage::

        import yaml
        from aeos.workflow.compiler import WorkflowCompiler

        raw = yaml.safe_load(open("workflow.yaml"))
        compiled = WorkflowCompiler().compile(raw)
        # compiled is a dict ready to POST to /api/v1/run or iterate steps
    """

    _VALID_MODES = {"single-agent", "multi-agent"}

    def compile(
        self,
        raw: dict[str, Any],
        variables: dict[str, str] | None = None,
    ) -> dict:
        """
        Validate and compile *raw* YAML dict.

        Args:
            raw:       Parsed YAML dict (top-level key must be 'workflow')
            variables: Optional key→value substitutions for {var} placeholders

        Returns:
            CompiledWorkflow.to_dict() — serialisable representation
        """
        wf = self._extract_workflow_block(raw)
        name = self._require_str(wf, "name", "workflow.name")
        description = wf.get("description", "")
        version = str(wf.get("version", "1.0"))
        agents = [str(a) for a in wf.get("agents", [])]

        raw_steps = wf.get("steps")
        if not raw_steps:
            raise WorkflowValidationError("workflow.steps is required and must not be empty")
        if not isinstance(raw_steps, list):
            raise WorkflowValidationError("workflow.steps must be a list")

        step_names: set[str] = set()
        steps: list[WorkflowStep] = []

        for i, raw_step in enumerate(raw_steps):
            step = self._compile_step(raw_step, index=i, variables=variables or {})
            if step.name in step_names:
                raise WorkflowValidationError(
                    f"Duplicate step name '{step.name}' at index {i}"
                )
            step_names.add(step.name)
            steps.append(step)

        # Validate depends_on references
        for step in steps:
            for dep in step.depends_on:
                if dep not in step_names:
                    raise WorkflowValidationError(
                        f"Step '{step.name}' depends_on unknown step '{dep}'"
                    )

        compiled = CompiledWorkflow(
            name=name,
            description=description,
            version=version,
            steps=steps,
            agents=agents,
        )
        return compiled.to_dict()

    def _extract_workflow_block(self, raw: dict) -> dict:
        if "workflow" not in raw:
            raise WorkflowValidationError(
                "YAML must have a top-level 'workflow:' key"
            )
        wf = raw["workflow"]
        if not isinstance(wf, dict):
            raise WorkflowValidationError("workflow: must be a mapping")
        return wf

    def _compile_step(
        self,
        raw: Any,
        index: int,
        variables: dict[str, str],
    ) -> WorkflowStep:
        if not isinstance(raw, dict):
            raise WorkflowValidationError(
                f"steps[{index}] must be a mapping, got {type(raw).__name__}"
            )

        name = str(raw.get("name", f"step-{index}"))
        task_raw = self._require_str(raw, "task", f"steps[{index}].task")
        task = self._interpolate(task_raw, variables)

        mode = str(raw.get("mode", "single-agent"))
        if mode not in self._VALID_MODES:
            raise WorkflowValidationError(
                f"steps[{index}].mode must be one of {self._VALID_MODES}, got '{mode}'"
            )

        agent = str(raw.get("agent", ""))
        depends_on = [str(d) for d in raw.get("depends_on", [])]
        timeout_s = int(raw.get("timeout_s", 60))
        retry = int(raw.get("retry", 0))

        return WorkflowStep(
            id=f"step-{index}",
            name=name,
            task=task,
            mode=mode,
            agent=agent,
            depends_on=depends_on,
            timeout_s=timeout_s,
            retry=retry,
        )

    @staticmethod
    def _require_str(d: dict, key: str, path: str) -> str:
        val = d.get(key)
        if val is None:
            raise WorkflowValidationError(f"'{path}' is required")
        if not isinstance(val, str) or not val.strip():
            raise WorkflowValidationError(f"'{path}' must be a non-empty string")
        return val.strip()

    @staticmethod
    def _interpolate(text: str, variables: dict[str, str]) -> str:
        """Replace {var} placeholders with values from *variables*."""
        def _replace(m: re.Match) -> str:
            key = m.group(1)
            if key in variables:
                return variables[key]
            return m.group(0)   # leave unreplaced if not in variables
        return re.sub(r"\{(\w+)\}", _replace, text)
