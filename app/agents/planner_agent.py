"""
AEOS Planner Agent — v2 CognitiveAgent
Decomposes user tasks into an executable subtask DAG.
Output is a structured JSON plan consumed by the Orchestrator's multi-agent executor.
All logic is rule-based — replace _step_execute internals with an LLM call when ready.

Migrated from v1 think()/act() to v2 11-step CognitiveAgent runtime.
"""

from __future__ import annotations
import copy
import uuid
from typing import Any

from app.agents.cognitive import CognitiveAgent, CognitiveContext, ExecutionResult
from app.core.logger import get_logger

log = get_logger(__name__)

_DAG_TEMPLATES: dict[str, list[dict]] = {
    "research": [
        {"id": "step_1", "agent": "research_agent", "description": "Retrieve relevant context from knowledge base", "depends_on": [], "priority": 1},
        {"id": "step_2", "agent": "reviewer_agent",  "description": "Validate research findings for completeness",  "depends_on": ["step_1"], "priority": 2},
    ],
    "code_analysis": [
        {"id": "step_1", "agent": "research_agent", "description": "Search knowledge base for relevant code patterns", "depends_on": [],                     "priority": 1},
        {"id": "step_2", "agent": "simple_agent",   "description": "Classify and decompose the code task",           "depends_on": [],                     "priority": 1},
        {"id": "step_3", "agent": "reviewer_agent", "description": "Review and validate combined analysis",          "depends_on": ["step_1", "step_2"], "priority": 2},
    ],
    "ml": [
        {"id": "step_1", "agent": "research_agent", "description": "Research ML approach and prior art",   "depends_on": [],                     "priority": 1},
        {"id": "step_2", "agent": "simple_agent",   "description": "Analyze task complexity and data needs", "depends_on": [],                     "priority": 1},
        {"id": "step_3", "agent": "reviewer_agent", "description": "Validate ML plan before execution",     "depends_on": ["step_1", "step_2"], "priority": 2},
    ],
    "general": [
        {"id": "step_1", "agent": "simple_agent",   "description": "Analyze and classify the task", "depends_on": [],         "priority": 1},
        {"id": "step_2", "agent": "reviewer_agent", "description": "Review the analysis output",    "depends_on": ["step_1"], "priority": 2},
    ],
}

_DOMAIN_KEYWORDS: dict[str, list[str]] = {
    "research":      ["research", "find", "search", "explain", "summarize", "what is", "how does", "why"],
    "code_analysis": ["code", "function", "class", "repo", "refactor", "debug", "bug", "test", "analyze"],
    "ml":            ["train", "model", "dataset", "predict", "accuracy", "ml", "neural", "classify", "regression"],
}


class PlannerAgent(CognitiveAgent):

    def __init__(self) -> None:
        super().__init__()
        self.id = "planner_agent"
        self.name = "Task Planner Agent"
        self.capabilities = ["task_decomposition", "dag_planning", "agent_routing", "subtask_ordering"]

    async def initialize(self) -> None:
        await super().initialize()

    # ── Step 8: Execute ────────────────────────────────────────────────────────

    async def _step_execute(self, ctx: CognitiveContext) -> None:
        task = ctx.task
        task_type = self._classify_task_type(task)
        subtasks = self._build_dag(task, task_type)

        plan = {
            "plan_id": str(uuid.uuid4()),
            "task": task,
            "task_type": task_type,
            "subtasks": subtasks,
            "estimated_complexity": self._estimate_complexity(task),
            "agent_count": len({s["agent"] for s in subtasks}),
        }

        self._log.info(
            "Plan created",
            extra={"ctx_task_type": task_type, "ctx_subtask_count": len(subtasks)},
        )

        ctx.execution = ExecutionResult(success=True, output=plan)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _classify_task_type(self, task: str) -> str:
        task_lower = task.lower()
        for domain, keywords in _DOMAIN_KEYWORDS.items():
            if any(kw in task_lower for kw in keywords):
                return domain
        return "general"

    def _build_dag(self, task: str, task_type: str) -> list[dict]:
        template = copy.deepcopy(_DAG_TEMPLATES.get(task_type, _DAG_TEMPLATES["general"]))
        if template:
            template[0]["description"] = f"{template[0]['description']}: {task[:80]}"
        return template

    def _estimate_complexity(self, task: str) -> int:
        score = 1
        score += min(len(task) // 60, 3)
        score += sum(task.lower().count(kw) for kw in ["and", "then", "also", "multiple", "all"])
        return min(score, 10)
