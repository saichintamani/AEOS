"""
Software Intelligence Platform — Repository Processing Pipeline
================================================================
End-to-end orchestration pipeline for repository analysis.

Stages:
  1. Ingestion      — fetch repository (GitHub/GitLab/Local)
  2. Parsing        — parse all source files → ParseResult[]
  3. Dependency     — build dependency graph
  4. Architecture   — reconstruct architecture layers & patterns
  5. Metrics        — collect file & repo metrics
  6. Security       — run security scan
  7. Code Review    — run review checkers
  8. Technical Debt — detect and prioritize debt
  9. Knowledge Graph — build knowledge graph
 10. Embedding      — generate code embeddings
 11. Search Index   — populate search indices
 12. Report         — generate RepositoryHealthReport

All stages are optional and can be toggled via PipelineConfig.
Results are cached at each stage for incremental execution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from software_intelligence.schemas import (
    ArchitectureReport, CodeReviewReport, DependencyGraph,
    ParseResult, ProcessingJob, ProcessingStage, RepositoryMetrics,
    SecurityReport, SourceFile, TechnicalDebtReport,
)
from software_intelligence.repository.base import BaseRepositoryProvider, IngestionResult
from software_intelligence.repository.providers import get_provider
from software_intelligence.parsers.base import ParserEngine
from software_intelligence.dependency_graph.builder import DependencyGraphBuilder
from software_intelligence.architecture.reconstructor import ArchitectureReconstructor
from software_intelligence.metrics.collector import MetricsCollector
from software_intelligence.security.scanner import SecurityScanner
from software_intelligence.code_review.engine import ReviewEngine
from software_intelligence.technical_debt.engine import TechnicalDebtEngine
from software_intelligence.knowledge_graph.builder import KnowledgeGraphBuilder
from software_intelligence.embeddings.engine import CodeEmbeddingEngine
from software_intelligence.search.engine import SearchEngine
from software_intelligence.reports.builder import ReportBuilder
from software_intelligence.cache.store import CacheStore


# ── Pipeline configuration ─────────────────────────────────────────────────────

@dataclass
class PipelineConfig:
    """Toggles for pipeline stages."""
    run_parsing:        bool = True
    run_dependency:     bool = True
    run_architecture:   bool = True
    run_metrics:        bool = True
    run_security:       bool = True
    run_review:         bool = True
    run_debt:           bool = True
    run_knowledge_graph: bool = True
    run_embeddings:     bool = True
    run_search_index:   bool = True
    run_report:         bool = True

    use_cache:          bool = True
    incremental:        bool = False


@dataclass
class PipelineResult:
    """Aggregated output from the pipeline."""
    repo_id:       str
    started_at:    str
    completed_at:  str
    duration_sec:  float
    files_parsed:  int
    parse_errors:  int

    parse_results:  list[ParseResult]          = field(default_factory=list)
    dependency_graph: DependencyGraph | None   = None
    architecture:   ArchitectureReport | None  = None
    metrics:        RepositoryMetrics | None   = None
    security:       SecurityReport | None      = None
    review:         CodeReviewReport | None    = None
    debt:           TechnicalDebtReport | None = None
    knowledge_graph: Any = None
    health_report:  Any = None

    errors: list[str] = field(default_factory=list)


# ── Pipeline orchestrator ──────────────────────────────────────────────────────

class RepositoryProcessingPipeline:
    """
    Executes the full Software Intelligence pipeline on a repository.

    Usage:
        pipeline = RepositoryProcessingPipeline(
            provider=get_provider("github", ProviderConfig(repo_url="https://github.com/owner/repo")),
            config=PipelineConfig(),
            cache=CacheStore.filesystem(),
        )
        result = pipeline.run(repo_id="owner/repo")
    """

    def __init__(
        self,
        provider: BaseRepositoryProvider,
        config: PipelineConfig | None = None,
        cache: CacheStore | None = None,
    ) -> None:
        self._provider = provider
        self._config   = config or PipelineConfig()
        self._cache    = cache  or CacheStore.in_memory()

        # Engine instances (reusable across stages)
        self._parser_engine = ParserEngine.default()
        self._dep_builder   = DependencyGraphBuilder()
        self._arch_recon    = ArchitectureReconstructor()
        self._metrics_col   = MetricsCollector()
        self._security_scan = SecurityScanner.default()
        self._review_engine = ReviewEngine.default()
        self._debt_engine   = TechnicalDebtEngine.default()
        self._kg_builder    = KnowledgeGraphBuilder()
        self._embed_engine  = CodeEmbeddingEngine()
        self._search_engine = SearchEngine()
        self._report_builder = ReportBuilder()

    def run(self, repo_id: str) -> PipelineResult:
        started_at = datetime.now(timezone.utc)
        result = PipelineResult(
            repo_id=repo_id,
            started_at=started_at.isoformat(),
            completed_at="",
            duration_sec=0.0,
            files_parsed=0,
            parse_errors=0,
        )

        try:
            # Stage 1: Ingestion
            files = self._stage_ingestion(repo_id, result)

            # Stage 2: Parsing
            if self._config.run_parsing:
                result.parse_results = self._stage_parsing(repo_id, files, result)

            # Stage 3: Dependency graph
            if self._config.run_dependency and result.parse_results:
                result.dependency_graph = self._stage_dependency(result.parse_results)

            # Stage 4: Architecture
            if self._config.run_architecture and result.parse_results:
                result.architecture = self._stage_architecture(
                    result.parse_results, result.dependency_graph
                )

            # Stage 5: Metrics
            if self._config.run_metrics and result.parse_results:
                result.metrics = self._stage_metrics(
                    repo_id, result.parse_results, result.dependency_graph
                )

            # Stage 6: Security
            if self._config.run_security and files:
                result.security = self._stage_security(repo_id, files)

            # Stage 7: Code review
            if self._config.run_review and result.parse_results:
                result.review = self._stage_review(repo_id, result.parse_results)

            # Stage 8: Technical debt
            if self._config.run_debt and result.parse_results:
                result.debt = self._stage_debt(
                    repo_id, result.parse_results, result.metrics, result.dependency_graph
                )

            # Stage 9: Knowledge graph
            if self._config.run_knowledge_graph and result.parse_results:
                result.knowledge_graph = self._stage_knowledge_graph(
                    repo_id, result.parse_results, result.dependency_graph
                )

            # Stage 10: Embeddings
            if self._config.run_embeddings and result.parse_results:
                self._stage_embeddings(result.parse_results)

            # Stage 11: Search index
            if self._config.run_search_index and result.parse_results:
                self._stage_search_index(result.parse_results, result.dependency_graph)

            # Stage 12: Report
            if self._config.run_report:
                result.health_report = self._stage_report(
                    repo_id, result.metrics, result.security, result.debt, result.review
                )

        except Exception as e:
            result.errors.append(f"Pipeline error: {str(e)}")

        completed_at = datetime.now(timezone.utc)
        result.completed_at = completed_at.isoformat()
        result.duration_sec = (completed_at - started_at).total_seconds()

        return result

    # ── Stage implementations ──────────────────────────────────────────────────

    def _stage_ingestion(self, repo_id: str, result: PipelineResult) -> list[SourceFile]:
        try:
            files = self._provider.stream_files()
            return list(files)
        except Exception as e:
            result.errors.append(f"Ingestion error: {str(e)}")
            return []

    def _stage_parsing(
        self,
        repo_id: str,
        files: list[SourceFile],
        result: PipelineResult,
    ) -> list[ParseResult]:
        parse_results = []
        for source_file in files:
            try:
                # Check cache first
                content_hash = self._cache._hash_content(source_file.content)
                if self._config.use_cache:
                    cached = self._cache.get_parse_result(repo_id, source_file.path, content_hash)
                    if cached:
                        parse_results.append(cached)
                        continue

                # Parse
                parsed = self._parser_engine.parse(source_file.path, source_file.content)
                parse_results.append(parsed)
                result.files_parsed += 1

                # Cache result
                if self._config.use_cache:
                    self._cache.set_parse_result(repo_id, source_file.path, content_hash, parsed)

            except Exception as e:
                result.parse_errors += 1
                result.errors.append(f"Parse error ({source_file.path}): {str(e)}")

        return parse_results

    def _stage_dependency(self, parse_results: list[ParseResult]) -> DependencyGraph:
        return self._dep_builder.build(parse_results)

    def _stage_architecture(
        self,
        parse_results: list[ParseResult],
        graph: DependencyGraph | None,
    ) -> ArchitectureReport:
        return self._arch_recon.reconstruct(parse_results, graph)

    def _stage_metrics(
        self,
        repo_id: str,
        parse_results: list[ParseResult],
        graph: DependencyGraph | None,
    ) -> RepositoryMetrics:
        if self._config.use_cache:
            cached = self._cache.get_repo_metrics(repo_id)
            if cached:
                return cached
        metrics = self._metrics_col.collect(parse_results, graph, repo_id=repo_id)
        if self._config.use_cache:
            self._cache.set_repo_metrics(repo_id, metrics)
        return metrics

    def _stage_security(self, repo_id: str, files: list[SourceFile]) -> SecurityReport:
        if self._config.use_cache:
            cached = self._cache.get_security_report(repo_id)
            if cached:
                return cached
        report = self._security_scan.scan(files, repo_id=repo_id)
        if self._config.use_cache:
            self._cache.set_security_report(repo_id, report)
        return report

    def _stage_review(self, repo_id: str, parse_results: list[ParseResult]) -> CodeReviewReport:
        return self._review_engine.review_pr(parse_results, pr_id="", repo_id=repo_id)

    def _stage_debt(
        self,
        repo_id: str,
        parse_results: list[ParseResult],
        metrics: RepositoryMetrics | None,
        graph: DependencyGraph | None,
    ) -> TechnicalDebtReport:
        if self._config.use_cache:
            cached = self._cache.get_debt_report(repo_id)
            if cached:
                return cached
        file_metrics = metrics.file_metrics if metrics else []
        report = self._debt_engine.analyze(parse_results, file_metrics, graph, repo_id=repo_id)
        if self._config.use_cache:
            self._cache.set_debt_report(repo_id, report)
        return report

    def _stage_knowledge_graph(
        self,
        repo_id: str,
        parse_results: list[ParseResult],
        graph: DependencyGraph | None,
    ) -> Any:
        if self._config.use_cache:
            cached = self._cache.get_knowledge_graph(repo_id)
            if cached:
                return cached
        kg = (
            self._kg_builder
            .add_parse_results(parse_results)
            .add_dependency_graph(graph)
            .build(repo_id=repo_id)
        )
        if self._config.use_cache:
            self._cache.set_knowledge_graph(repo_id, kg)
        return kg

    def _stage_embeddings(self, parse_results: list[ParseResult]) -> None:
        self._embed_engine.index_results(parse_results)

    def _stage_search_index(
        self,
        parse_results: list[ParseResult],
        graph: DependencyGraph | None,
    ) -> None:
        self._search_engine.build(results=parse_results, graph=graph)
        self._search_engine.attach_semantic(self._embed_engine)

    def _stage_report(
        self,
        repo_id: str,
        metrics: RepositoryMetrics | None,
        security: SecurityReport | None,
        debt: TechnicalDebtReport | None,
        review: CodeReviewReport | None,
    ) -> Any:
        builder = self._report_builder
        if metrics:
            builder.set_metrics(metrics)
        if security:
            builder.set_security(security)
        if debt:
            builder.set_debt(debt)
        if review:
            builder.set_review(review)
        return builder.build_health_report(repo_id)
