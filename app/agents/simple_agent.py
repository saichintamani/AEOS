"""
AEOS Simple Agent — v2 CognitiveAgent
Default agent registered in the Orchestrator.
Simulates a lightweight analysis pipeline: classify → decompose → summarize.
Used as the baseline for all single-agent /run requests.
No LLM calls yet — deterministic logic demonstrates the full agent lifecycle.

Migrated from v1 think()/act() to v2 11-step CognitiveAgent runtime.
"""

import asyncio
from typing import Any

from app.agents.cognitive import CognitiveAgent, CognitiveContext, ExecutionResult
from app.core.logger import get_logger

log = get_logger(__name__)


class SimpleAgent(CognitiveAgent):
    """
    Baseline agent that performs rule-based task analysis.
    Implements _step_execute with keyword extraction, domain classification,
    decomposition, and complexity scoring.
    """

    def __init__(self) -> None:
        super().__init__()
        self.id = "simple_agent"
        self.name = "Simple Analysis Agent"
        self.capabilities = [
            "task_classification",
            "keyword_extraction",
            "complexity_scoring",
            "task_decomposition",
        ]

    async def initialize(self) -> None:
        await super().initialize()
        log.debug("SimpleAgent: no external resources required")

    # ── Step 8: Execute ────────────────────────────────────────────────────────

    async def _step_execute(self, ctx: CognitiveContext) -> None:
        task = ctx.task

        # Simulate multi-step async pipeline
        await asyncio.sleep(0.05)
        keywords = self._extract_keywords(task)

        await asyncio.sleep(0.05)
        subtasks = self._decompose(task)

        await asyncio.sleep(0.02)
        complexity = self._score_complexity(task)
        confidence = round(min(0.5 + (len(keywords) * 0.05), 0.98), 2)

        result = {
            "summary": f"Analyzed task: '{task[:80]}{'...' if len(task) > 80 else ''}'",
            "domain": self._classify_domain(task),
            "keywords": keywords,
            "subtasks": subtasks,
            "complexity_score": complexity,
            "confidence": confidence,
            "recommendation": self._recommend(complexity),
        }

        ctx.execution = ExecutionResult(
            success=True,
            output=result,
            token_cost=0,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _classify_domain(self, task: str) -> str:
        task_lower = task.lower()
        domains = {
            "machine_learning": ["train", "model", "dataset", "accuracy", "loss", "predict", "ml", "neural"],
            "data_engineering": ["pipeline", "etl", "ingest", "transform", "schema", "database", "sql"],
            "code_analysis":    ["code", "function", "class", "repo", "refactor", "debug", "bug", "test"],
            "research":         ["research", "find", "search", "analyze", "explain", "summarize", "compare"],
            "orchestration":    ["orchestrate", "agent", "workflow", "task", "schedule", "route"],
        }
        for domain, keywords in domains.items():
            if any(kw in task_lower for kw in keywords):
                return domain
        return "general"

    def _extract_keywords(self, task: str) -> list[str]:
        stopwords = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "being", "have", "has", "had", "do", "does", "did", "will",
            "would", "could", "should", "may", "might", "shall", "to",
            "of", "in", "for", "on", "with", "at", "by", "from", "and",
            "or", "but", "if", "then", "that", "this", "it", "i", "me",
        }
        tokens = task.lower().split()
        keywords = [
            t.strip(".,!?;:\"'") for t in tokens
            if t.strip(".,!?;:\"'") and t.strip(".,!?;:\"'") not in stopwords and len(t) > 2
        ]
        seen: set[str] = set()
        unique: list[str] = []
        for kw in keywords:
            if kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique[:10]

    def _score_complexity(self, task: str) -> int:
        score = 1
        score += min(len(task) // 50, 3)
        score += task.count("and") + task.count("then") + task.count("also")
        score += task.lower().count("multiple") + task.lower().count("all")
        return min(score, 10)

    def _decompose(self, task: str) -> list[str]:
        separators = [" and then ", " then ", ", and ", " also ", " after "]
        parts = [task]
        for sep in separators:
            new_parts = []
            for part in parts:
                new_parts.extend(part.split(sep))
            parts = new_parts
        subtasks = [p.strip().capitalize() for p in parts if len(p.strip()) > 5]
        return subtasks if len(subtasks) > 1 else [f"Execute: {task.strip().capitalize()}"]

    def _recommend(self, complexity: int) -> str:
        if complexity <= 3:
            return "Single-agent execution sufficient."
        elif complexity <= 6:
            return "Consider Research Agent for context enrichment."
        else:
            return "Route to multi-agent pipeline: Planner → Research → ML → Reviewer."
