"""
Software Intelligence Platform — FastAPI Interfaces
===================================================
REST API routes for the Software Intelligence Platform.

Endpoints:
  POST /repository/ingest       — ingest a repository (GitHub/GitLab/Local)
  GET  /repository/{repo_id}    — fetch repository metadata
  POST /repository/{repo_id}/sync — trigger incremental sync

  GET  /analysis/{repo_id}/metrics       — metrics snapshot
  GET  /analysis/{repo_id}/security      — security report
  GET  /analysis/{repo_id}/debt          — technical debt report
  GET  /analysis/{repo_id}/architecture  — architecture reconstruction
  GET  /analysis/{repo_id}/health        — full health report

  POST /search                   — unified search (semantic + symbol + docs)
  GET  /knowledge_graph/{repo_id} — knowledge graph export

  POST /issues/analyze           — batch issue analysis
  POST /prs/analyze              — batch PR analysis
  POST /code_review/file         — review a single file
  POST /code_review/pr           — review a PR diff

All endpoints return JSON. For large reports, use Accept: application/x-ndjson
for streaming line-delimited JSON.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# Placeholder imports — adjust when integrating into AEOS
# from software_intelligence.repository.providers import get_provider
# from software_intelligence.parsers.base import ParserEngine
# ...etc


# ── Request / Response schemas ─────────────────────────────────────────────────

class IngestRequest(BaseModel):
    repo_url: str = Field(..., description="Repository URL (GitHub/GitLab) or local path")
    provider: str = Field("auto", description="github | gitlab | local_git | local_fs | auto")
    branch: str = Field("main", description="Branch to analyze")
    incremental: bool = Field(False, description="Use incremental sync if state exists")


class IngestResponse(BaseModel):
    repo_id: str
    status: str           # "success" | "in_progress" | "failed"
    files_processed: int
    parse_errors: int
    message: str


class AnalysisStatusResponse(BaseModel):
    repo_id: str
    last_synced: str
    files_indexed: int
    metrics_available: bool
    security_available: bool
    debt_available: bool


class SearchRequest(BaseModel):
    query: str
    mode: str = Field("all", description="all | semantic | symbol | documentation | dependency | pattern")
    top_k: int = Field(10, ge=1, le=100)
    entity_types: list[str] | None = Field(None, description="Filter by: function, class, module, issue, pr")


class SearchResponse(BaseModel):
    query: str
    results: list[dict]
    count: int
    mode: str


class IssueAnalysisRequest(BaseModel):
    issues: list[dict]    # List of IssueRecord-compatible dicts


class PRAnalysisRequest(BaseModel):
    prs: list[dict]       # List of PullRequestRecord-compatible dicts


# ── Router setup ───────────────────────────────────────────────────────────────

router = APIRouter(prefix="/software_intelligence", tags=["Software Intelligence"])


# ── Repository ingestion ───────────────────────────────────────────────────────

@router.post("/repository/ingest", response_model=IngestResponse)
async def ingest_repository(req: IngestRequest) -> IngestResponse:
    """
    Ingest a repository from a given URL or local path.
    Spawns background processing pipeline.

    Example:
        POST /software_intelligence/repository/ingest
        {
          "repo_url": "https://github.com/owner/repo",
          "provider": "github",
          "branch": "main",
          "incremental": false
        }
    """
    # TODO: wire to RepositoryProcessingPipeline
    # provider = get_provider(req.provider, ProviderConfig(repo_url=req.repo_url))
    # result = provider.get_repository(branch=req.branch)
    # ... spawn pipeline ...
    return IngestResponse(
        repo_id="stub_repo_id",
        status="in_progress",
        files_processed=0,
        parse_errors=0,
        message="Repository ingestion started. Check /repository/{repo_id} for status.",
    )


@router.get("/repository/{repo_id}", response_model=AnalysisStatusResponse)
async def get_repository_status(repo_id: str) -> AnalysisStatusResponse:
    """Fetch current analysis status for a repository."""
    # TODO: query status store
    return AnalysisStatusResponse(
        repo_id=repo_id,
        last_synced="2026-06-28T00:00:00Z",
        files_indexed=0,
        metrics_available=False,
        security_available=False,
        debt_available=False,
    )


@router.post("/repository/{repo_id}/sync")
async def sync_repository(repo_id: str) -> dict:
    """Trigger an incremental sync for the given repository."""
    # TODO: wire to IncrementalSyncManager
    return {"repo_id": repo_id, "status": "sync_started"}


# ── Analysis endpoints ─────────────────────────────────────────────────────────

@router.get("/analysis/{repo_id}/metrics")
async def get_metrics(repo_id: str) -> dict:
    """Return RepositoryMetrics snapshot."""
    # TODO: load from MetricsCollector cache
    return {"repo_id": repo_id, "metrics": {}}


@router.get("/analysis/{repo_id}/security")
async def get_security(repo_id: str) -> dict:
    """Return SecurityReport."""
    # TODO: load from SecurityScanner cache
    return {"repo_id": repo_id, "findings": []}


@router.get("/analysis/{repo_id}/debt")
async def get_debt(repo_id: str) -> dict:
    """Return TechnicalDebtReport."""
    # TODO: load from TechnicalDebtEngine cache
    return {"repo_id": repo_id, "items": []}


@router.get("/analysis/{repo_id}/architecture")
async def get_architecture(repo_id: str) -> dict:
    """Return ArchitectureReport."""
    # TODO: load from ArchitectureReconstructor cache
    return {"repo_id": repo_id, "components": []}


@router.get("/analysis/{repo_id}/health")
async def get_health(repo_id: str, format: str = Query("json", regex="^(json|markdown)$")) -> Any:
    """
    Return full RepositoryHealthReport.
    Format: json (default) or markdown.
    """
    # TODO: wire to ReportBuilder
    # builder = ReportBuilder()
    # report = builder.build_health_report(repo_id)
    # if format == "markdown":
    #     return {"markdown": report.to_markdown()}
    return {"repo_id": repo_id, "health": {}}


# ── Search ─────────────────────────────────────────────────────────────────────

@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest) -> SearchResponse:
    """
    Unified search across code, issues, PRs, and documentation.

    Example:
        POST /software_intelligence/search
        {
          "query": "authentication middleware",
          "mode": "semantic",
          "top_k": 10
        }
    """
    # TODO: wire to SearchEngine
    # engine = SearchEngine()
    # results = engine.search(req.query, top_k=req.top_k, mode=req.mode)
    return SearchResponse(
        query=req.query,
        results=[],
        count=0,
        mode=req.mode,
    )


# ── Knowledge graph ────────────────────────────────────────────────────────────

@router.get("/knowledge_graph/{repo_id}")
async def get_knowledge_graph(
    repo_id: str,
    format: str = Query("json", regex="^(json|dot)$"),
) -> Any:
    """
    Export the knowledge graph.
    Format: json (D3-compatible) or dot (Graphviz).
    """
    # TODO: wire to KnowledgeGraphBuilder
    # graph = builder.build(repo_id)
    # if format == "dot":
    #     return {"dot": graph.to_dot()}
    # return graph.to_dict()
    return {"repo_id": repo_id, "nodes": [], "edges": []}


# ── Issue & PR analysis ────────────────────────────────────────────────────────

@router.post("/issues/analyze")
async def analyze_issues(req: IssueAnalysisRequest) -> dict:
    """
    Batch analyze GitHub/GitLab issues.
    Returns IssueAnalysis records (category, priority, duplicates, etc.).
    """
    # TODO: wire to IssueAnalyzer
    # analyzer = IssueAnalyzer()
    # analyses = analyzer.analyze_batch(issues)
    return {"analyses": [], "count": 0}


@router.post("/prs/analyze")
async def analyze_prs(req: PRAnalysisRequest) -> dict:
    """
    Batch analyze pull requests.
    Returns PRAnalysis records (risk, summary, checklist, breaking changes, etc.).
    """
    # TODO: wire to PRAnalyzer
    # analyzer = PRAnalyzer()
    # analyses = analyzer.analyze_batch(prs)
    return {"analyses": [], "count": 0}


# ── Code review ────────────────────────────────────────────────────────────────

@router.post("/code_review/file")
async def review_file(file_path: str, content: str) -> dict:
    """
    Review a single file.
    Returns CodeReviewReport with comments.
    """
    # TODO: wire to ReviewEngine
    # parser = ParserEngine.default().parse(file_path, content)
    # engine = ReviewEngine.default()
    # report = engine.review_file(parser, repo_id="inline")
    return {"review": {}, "score": 0.0}


@router.post("/code_review/pr")
async def review_pr(pr_id: str, files: list[dict]) -> dict:
    """
    Review all files in a PR.
    Returns aggregated CodeReviewReport.
    """
    # TODO: wire to ReviewEngine
    # engine = ReviewEngine.default()
    # report = engine.review_pr(parsed_files, pr_id=pr_id, repo_id="...")
    return {"review": {}, "score": 0.0}


# ── Utility endpoints ──────────────────────────────────────────────────────────

@router.get("/health")
async def health_check() -> dict:
    """Platform health check."""
    return {"status": "ok", "service": "software_intelligence"}


@router.get("/capabilities")
async def capabilities() -> dict:
    """Return supported languages, providers, and features."""
    return {
        "languages": ["python", "javascript", "typescript", "java", "go", "rust", "ruby", "kotlin", "cpp"],
        "providers": ["github", "gitlab", "local_git", "local_fs"],
        "features": [
            "code_parsing", "dependency_graph", "architecture_reconstruction",
            "security_scanning", "technical_debt", "code_review",
            "knowledge_graph", "semantic_search", "issue_intelligence", "pr_intelligence",
        ],
    }
