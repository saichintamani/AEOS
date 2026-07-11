"""
AEOS Executor Agent — v2 CognitiveAgent
Action-performer agent: validates, sequences, and executes a list of actions
derived from the task or a prior planner result.

Supported action types (rule-based, no external calls):
  summarize   — produce a text summary of the context
  format      — reformat structured data into a canonical shape
  validate    — check whether required fields are present
  transform   — apply simple key/value transformations
  aggregate   — combine multiple results into a single structure

Unsupported actions are logged and marked "unsupported" — no exception raised.

Routing keyword triggers: execute, run, deploy, perform, implement

Migrated from v1 think()/act() to v2 11-step CognitiveAgent runtime.
"""

from __future__ import annotations
from typing import Any

from app.agents.cognitive import CognitiveAgent, CognitiveContext, ExecutionResult
from app.core.logger import get_logger

log = get_logger(__name__)

_SUPPORTED_ACTIONS = {"summarize", "format", "validate", "transform", "aggregate"}

_ACTION_KEYWORDS = {
    "summarize": {"summarize", "summary", "brief", "overview"},
    "format":    {"format", "reformat", "structure", "organize"},
    "validate":  {"validate", "verify", "check", "confirm"},
    "transform": {"transform", "convert", "map", "translate"},
    "aggregate": {"aggregate", "combine", "merge", "collect", "join"},
}


class ExecutorAgent(CognitiveAgent):
    """
    Action execution agent.

    _step_execute parses action list from task, validates, and executes
    each action, then writes the execution log to working memory.
    """

    def __init__(self) -> None:
        super().__init__()
        self.id = "executor_agent"
        self.name = "Executor Agent"
        self.capabilities = [
            "action_execution",
            "plan_execution",
            "validation",
            "status_reporting",
            "action_rollback",
        ]

    async def initialize(self) -> None:
        await super().initialize()
        self._log.debug("ExecutorAgent ready")

    # ── Step 8: Execute ────────────────────────────────────────────────────────

    async def _step_execute(self, ctx: CognitiveContext) -> None:
        task = ctx.task
        previous = ctx.raw_context.get("previous_result") or {}
        upstream = ctx.raw_context.get("upstream_results", {})
        if not previous and upstream:
            previous = next(iter(upstream.values()), {})

        actions_requested = self._parse_actions(task)
        execution_log: list[str] = []
        actions_executed: list[dict] = []

        for action in actions_requested:
            record = self._execute_action(action, task, previous, [], execution_log)
            actions_executed.append(record)

        success_count    = sum(1 for r in actions_executed if r["status"] == "success")
        failure_count    = sum(1 for r in actions_executed if r["status"] == "error")
        unsupported_count = sum(1 for r in actions_executed if r["status"] == "unsupported")

        if failure_count == len(actions_requested) and actions_requested:
            execution_status = "failed"
        elif success_count + unsupported_count == len(actions_requested):
            execution_status = "completed"
        elif success_count > 0:
            execution_status = "partial"
        else:
            execution_status = "completed"

        # Write execution log to working memory
        try:
            from app.core.memory import get_memory
            mem = get_memory()
            mem.write_short(
                task_id=ctx.cycle_id,
                key="execution_log",
                value=execution_log,
                agent_id=self.id,
            )
        except Exception as exc:
            self._log.warning("Could not write execution log to memory", extra={"ctx_error": str(exc)})

        ctx.execution = ExecutionResult(
            success=execution_status in ("completed", "partial"),
            output={
                "actions_requested": actions_requested,
                "actions_executed": actions_executed,
                "success_count": success_count,
                "failure_count": failure_count,
                "execution_status": execution_status,
                "execution_log": execution_log,
            },
        )

    # ── Action implementations ─────────────────────────────────────────────────

    def _execute_action(
        self,
        action: str,
        task: str,
        previous: Any,
        history: list[dict],
        execution_log: list[str],
    ) -> dict:
        if action not in _SUPPORTED_ACTIONS:
            msg = f"[UNSUPPORTED] '{action}' is not a supported action type."
            execution_log.append(msg)
            self._log.warning(msg)
            return {"action": action, "status": "unsupported", "result": None, "error": msg}

        try:
            result = self._dispatch(action, task, previous, history)
            msg = f"[OK] {action}: completed successfully."
            execution_log.append(msg)
            return {"action": action, "status": "success", "result": result, "error": ""}
        except Exception as exc:
            msg = f"[ERROR] {action}: {exc}"
            execution_log.append(msg)
            self._log.exception("Action execution failed", extra={"ctx_action": action})
            return {"action": action, "status": "error", "result": None, "error": str(exc)}

    def _dispatch(self, action: str, task: str, previous: Any, history: list[dict]) -> Any:
        if action == "summarize":
            return self._action_summarize(previous, history)
        if action == "format":
            return self._action_format(previous)
        if action == "validate":
            return self._action_validate(previous)
        if action == "transform":
            return self._action_transform(previous)
        if action == "aggregate":
            return self._action_aggregate(history)
        raise ValueError(f"No dispatch for action '{action}'")

    def _action_summarize(self, previous: Any, history: list[dict]) -> dict:
        parts = []
        if isinstance(previous, dict):
            for k, v in previous.items():
                if isinstance(v, str) and v:
                    parts.append(f"{k}: {v[:120]}")
        elif isinstance(previous, str):
            parts.append(previous[:300])
        for h in history[-2:]:
            if isinstance(h, dict):
                for k, v in h.items():
                    if isinstance(v, str):
                        parts.append(f"{k}: {v[:80]}")
        return {
            "summary": " | ".join(parts[:6]) if parts else "No content to summarize.",
            "source_count": len(history) + (1 if previous else 0),
        }

    def _action_format(self, previous: Any) -> dict:
        if isinstance(previous, dict):
            return {k: str(v) for k, v in previous.items()}
        if isinstance(previous, list):
            return {"items": [str(i) for i in previous], "count": len(previous)}
        return {"formatted": str(previous) if previous else ""}

    def _action_validate(self, previous: Any) -> dict:
        if previous is None:
            return {"valid": False, "issues": ["No data to validate."]}
        issues = []
        if isinstance(previous, dict):
            empty_keys = [k for k, v in previous.items() if v is None or v == ""]
            if empty_keys:
                issues.append(f"Empty fields: {empty_keys}")
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "field_count": len(previous) if isinstance(previous, dict) else 1,
        }

    def _action_transform(self, previous: Any) -> dict:
        if isinstance(previous, dict):
            return {k.upper().replace(" ", "_"): v for k, v in previous.items()}
        if isinstance(previous, list):
            return {"transformed": [str(item).strip() for item in previous]}
        return {"transformed": str(previous).strip() if previous is not None else ""}

    def _action_aggregate(self, history: list[dict]) -> dict:
        combined: dict[str, list] = {}
        for step in history:
            if isinstance(step, dict):
                for k, v in step.items():
                    combined.setdefault(k, []).append(v)
        return {"aggregated": combined, "source_count": len(history)}

    def _parse_actions(self, task: str) -> list[str]:
        task_lower = task.lower()
        found: list[str] = []
        for action, keywords in _ACTION_KEYWORDS.items():
            if any(kw in task_lower for kw in keywords):
                found.append(action)
        if not found:
            found = ["summarize"]
        return found
