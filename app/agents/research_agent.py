"""
AEOS Research Agent — v2 CognitiveAgent
Queries the RAG knowledge base and synthesizes findings.
Outputs structured research results with source attribution.
Replace synthesis logic with an LLM call when ready.

Migrated from v1 think()/act() to v2 11-step CognitiveAgent runtime.
RAG retrieval moved to _step_retrieve override; synthesis in _step_execute.
"""

from __future__ import annotations
import time
from typing import Any

from app.agents.cognitive import (
    CognitiveAgent, CognitiveContext,
    ExecutionResult, RetrievedContext,
)
from app.core.logger import get_logger

log = get_logger(__name__)


class ResearchAgent(CognitiveAgent):

    def __init__(self) -> None:
        super().__init__()
        self.id = "research_agent"
        self.name = "Research Agent"
        self.capabilities = ["rag_retrieval", "context_synthesis", "knowledge_search", "source_attribution"]
        self._rag_engine = None

    async def initialize(self) -> None:
        await super().initialize()
        try:
            from app.rag.rag_engine import get_rag_engine
            self._rag_engine = get_rag_engine()
            await self._rag_engine.initialize()
        except Exception as exc:
            self._log.warning(
                "RAG engine init failed — running without retrieval",
                extra={"ctx_error": str(exc)},
            )
            self._rag_engine = None

    # ── Step 3: Retrieve — hit the RAG engine ─────────────────────────────────

    async def _step_retrieve(self, ctx: CognitiveContext) -> None:
        t0 = time.perf_counter()
        query = self._extract_query(ctx.task)

        rag_passages: list[str] = []
        short_term: list[dict[str, Any]] = []

        if self._rag_engine and self._rag_engine.store_count() > 0:
            results = self._rag_engine.query(query, top_k=5)
            rag_passages = [r.text[:300] for r in results]
            short_term = [
                {
                    "text": r.text[:300],
                    "score": r.score,
                    "source": r.metadata.get("source", "unknown"),
                    "rank": r.rank,
                }
                for r in results
            ]

        # Also surface upstream results from prior pipeline nodes
        upstream = ctx.raw_context.get("upstream_results", {})
        for k, v in upstream.items():
            short_term.append({"key": k, "value": str(v)[:200]})

        ctx.retrieved = RetrievedContext(
            short_term_items=short_term,
            rag_passages=rag_passages,
            retrieval_latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            total_items=len(short_term),
        )

    # ── Step 8: Execute — synthesize findings ─────────────────────────────────

    async def _step_execute(self, ctx: CognitiveContext) -> None:
        query = self._extract_query(ctx.task)
        findings = ctx.retrieved.short_term_items if ctx.retrieved else []
        rag_findings = [f for f in findings if "score" in f]  # RAG results have score

        confidence = 0.5
        if rag_findings:
            confidence = round(sum(f["score"] for f in rag_findings) / len(rag_findings), 2)

        synthesis = self._synthesize(query, rag_findings)

        self._log.info(
            "Research complete",
            extra={"ctx_findings": len(rag_findings), "ctx_confidence": confidence},
        )

        ctx.execution = ExecutionResult(
            success=True,
            output={
                "query": query,
                "sources_found": len(rag_findings),
                "findings": rag_findings,
                "synthesis": synthesis,
                "confidence": confidence,
            },
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    def _extract_query(self, task: str) -> str:
        prefixes = [
            "research and summarize ", "research ", "find information about ",
            "search for ", "look up ", "explain ", "what is ", "how does ",
            "summarize ", "analyze ",
        ]
        task_lower = task.lower().strip()
        for prefix in prefixes:
            if task_lower.startswith(prefix):
                return task[len(prefix):].strip()
        return task.strip()

    def _synthesize(self, query: str, findings: list[dict]) -> str:
        if not findings:
            return (
                f"No relevant documents found in the knowledge base for query: '{query}'. "
                "Consider ingesting relevant documents first via the /api/v1/github/analyze "
                "endpoint or the RAG ingest API."
            )
        top = findings[0]
        source_list = list({f["source"] for f in findings})
        return (
            f"Found {len(findings)} relevant document(s) for '{query}'. "
            f"Top match (score={top['score']:.2f}): {top['text'][:200]}... "
            f"Sources consulted: {', '.join(source_list[:3])}."
        )
