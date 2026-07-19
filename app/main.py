"""
AEOS FastAPI Application
Entry point for the entire platform.
Owns: lifespan management, route registration, middleware, DI wiring.

Phase 8A:    AEOS HyperKernel wired into lifespan.
Phase 9B.6:  InvariantEngine, Prometheus /metrics, validation routes.
"""

import hmac
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.rag.security import (
    RateLimiter,
    SecurityError,
    safe_resolve,
    sanitize_filename,
    validate_namespace,
    validate_upload_content,
    validate_upload_extension,
)
from app.rag.loader import MAX_FILE_SIZE_BYTES

from app.core.config import settings
from app.core.logger import get_logger, set_trace_context
from app.core.orchestrator import Orchestrator
from app.agents.simple_agent import SimpleAgent
from app.agents.planner_agent import PlannerAgent
from app.agents.research_agent import ResearchAgent
from app.agents.reviewer_agent import ReviewerAgent
from app.agents.analyst_agent import AnalystAgent
from app.agents.executor_agent import ExecutorAgent

log = get_logger(__name__)

# ── Global instances ───────────────────────────────────────────────────────────
_orchestrator = Orchestrator()

# ── Global metrics registry (Phase 9B.6) ──────────────────────────────────────
from app.distributed.metrics.registry import MetricsRegistry
from app.distributed.observability.prometheus import PrometheusExporter, APIMetrics

_metrics_registry = MetricsRegistry(node_id="aeos-api")
_prometheus_exporter = PrometheusExporter(_metrics_registry, node_id="aeos-api")
_api_metrics = APIMetrics(_metrics_registry)

# ── Tiered, configurable rate limiting ─────────────────────────────────────────
# One token-bucket limiter per endpoint tier; thresholds come from config, never
# hardcoded. The "expensive" tier adds exponential backoff for repeat offenders.
_rl_expensive = RateLimiter(
    capacity=settings.rate_limit_expensive_per_minute,
    penalty_base=settings.rate_limit_backoff_base_seconds,
    penalty_max=settings.rate_limit_backoff_max_seconds,
)
_rl_rag = RateLimiter(capacity=settings.rag_rate_limit_per_minute)
_rl_default = RateLimiter(capacity=settings.rate_limit_default_per_minute)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _enforce_rate(limiter: RateLimiter, request: Request, tier: str) -> None:
    """Raise 429 (with Retry-After) if the client has exhausted its tier budget."""
    if not settings.rate_limit_enabled:
        return
    decision = limiter.check(f"{tier}:{_client_ip(request)}")
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Retry later.",
            headers={"Retry-After": str(int(decision.retry_after) + 1)},
        )


def _make_guard(limiter: RateLimiter, tier: str, require_key: bool = False):
    """Build a FastAPI dependency: per-IP rate limit + optional X-API-Key auth."""
    async def guard(request: Request) -> None:
        _enforce_rate(limiter, request, tier)
        if require_key and settings.api_key:
            provided = request.headers.get("X-API-Key", "")
            if not hmac.compare_digest(provided, settings.api_key):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing API key.",
                )
    return guard


# Tier guards used as route dependencies.
rag_guard = _make_guard(_rl_rag, "rag", require_key=True)
expensive_guard = _make_guard(_rl_expensive, "expensive")
default_guard = _make_guard(_rl_default, "default")


def _api_error(request: Request, exc: Exception, log_msg: str = "API route failure") -> JSONResponse:
    """
    Generic 500 for any route — logs full detail server-side, returns only a
    generic message + trace_id to the client (no stack traces / internals).
    """
    log.exception(log_msg, extra={"ctx_path": request.url.path})
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "message": "Internal error while processing the request.",
            "trace_id": getattr(request.state, "trace_id", None),
        },
    )


# Backward-compatible alias for the RAG routes.
_rag_error = _api_error


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("AEOS starting up", extra={"ctx_version": settings.app_version, "ctx_env": settings.environment})

    # ── 1. Boot the HyperKernel ────────────────────────────────────────────────
    from app.kernel.kernel import AEOSKernel
    kernel = AEOSKernel.get_instance()

    try:
        await kernel.startup()
        log.info("HyperKernel booted", extra={"ctx_state": kernel.state().value})
    except Exception as exc:
        log.error("HyperKernel boot failed — falling back to direct orchestrator", extra={"ctx_error": str(exc)})
        kernel = None

    # ── 2. Register agents with orchestrator ───────────────────────────────────
    _orchestrator.register(SimpleAgent())
    _orchestrator.register(PlannerAgent())
    _orchestrator.register(ResearchAgent())
    _orchestrator.register(ReviewerAgent())
    _orchestrator.register(AnalystAgent())
    _orchestrator.register(ExecutorAgent())

    await _orchestrator.startup()

    # ── 3. Register orchestrator as kernel service ─────────────────────────────
    if kernel:
        try:
            kernel.register_service(
                service_id="orchestrator",
                service=_orchestrator,
                capabilities=[
                    "agent.execute",
                    "agent.plan",
                    "agent.research",
                    "agent.analyze",
                    "agent.review",
                ],
            )
        except Exception as exc:
            log.warning("Could not register orchestrator with kernel", extra={"ctx_error": str(exc)})

        # ── 4. Wire Execution Engine as kernel service ─────────────────────────
        try:
            from app.execution.engine import ExecutionEngine
            engine = ExecutionEngine(kernel=kernel)
            kernel.register_service(
                service_id="execution_engine",
                service=engine,
                capabilities=["execution.run", "execution.plan"],
            )
            app.state.execution_engine = engine
            log.info("ExecutionEngine registered as kernel service")
        except Exception as exc:
            log.warning("ExecutionEngine registration failed", extra={"ctx_error": str(exc)})

    # ── 5. Pre-warm RAG engine ─────────────────────────────────────────────────
    try:
        from app.rag.rag_engine import get_rag_engine
        await get_rag_engine().initialize()
        log.info("RAG engine ready")
        if kernel:
            kernel.register_service(
                service_id="rag_engine",
                service=get_rag_engine(),
                capabilities=["rag.query", "rag.index", "rag.ingest"],
            )
    except Exception as exc:
        log.warning("RAG engine init failed (non-fatal)", extra={"ctx_error": str(exc)})

    app.state.orchestrator = _orchestrator
    app.state.kernel = kernel

    # Warn loudly when running without an API key in non-debug mode.
    # In production, every endpoint should be behind an authenticated gateway.
    if not settings.api_key and not settings.debug:
        log.critical(
            "SECURITY: API_KEY is not set. Non-RAG endpoints are unauthenticated. "
            "Set the API_KEY environment variable before exposing this service externally."
        )

    # ── 6. Expose metrics + validation state on app ────────────────────────────
    app.state.metrics_registry = _metrics_registry
    app.state.prometheus_exporter = _prometheus_exporter
    app.state.api_metrics = _api_metrics

    # ── 7. Start InvariantEngine background monitor ────────────────────────────
    try:
        from app.distributed.validation.invariants import (
            InvariantEngine,
            check_raft_single_leader,
            check_raft_log_monotonicity,
            check_raft_inv_001,
        )
        inv_engine = InvariantEngine(check_interval_s=30.0)

        # Raft checks are registered only when Raft nodes are available.
        # For the API node we register lightweight structural checks only;
        # worker nodes wire full Raft checks when they boot.
        inv_engine.on_violation(
            lambda v: log.error(
                "INVARIANT_VIOLATION",
                extra={
                    "ctx_invariant": v.invariant_id,
                    "ctx_severity": str(v.severity),
                    "ctx_msg": v.message,
                },
            )
        )
        await inv_engine.start()
        app.state.invariant_engine = inv_engine
        log.info("InvariantEngine: background monitor started")
    except Exception as exc:
        log.warning("InvariantEngine init failed (non-fatal)", extra={"ctx_error": str(exc)})
        app.state.invariant_engine = None

    log.info("AEOS ready to serve", extra={"ctx_kernel": kernel.state().value if kernel else "unavailable"})

    yield  # ← application runs here

    log.info("AEOS shutting down")
    inv_engine = getattr(app.state, "invariant_engine", None)
    if inv_engine:
        await inv_engine.stop()
    await _orchestrator.shutdown()
    if kernel:
        await kernel.shutdown(graceful=True)
    log.info("AEOS shutdown complete")



# ── Application factory ────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="AI Engineering Orchestration System — Production Runtime",
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )

    # CORS: never pair a wildcard origin with credentials. Use an explicit
    # allow-list from config; empty list = same-origin only (the web UI is served
    # from this same app, so it needs no cross-origin grant).
    _cors_origins = list(settings.cors_allow_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=bool(_cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-API-Key", "X-Trace-Id"],
    )

    @app.middleware("http")
    async def trace_and_metrics_middleware(request: Request, call_next):
        trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4()))
        set_trace_context(trace_id=trace_id)
        request.state.trace_id = trace_id
        start_ns = time.monotonic_ns()
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        # Record HTTP metrics (skip /metrics itself to avoid recursion)
        if request.url.path != "/metrics":
            _api_metrics.record_request(
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                start_ns=start_ns,
            )
        return response

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        log.exception("Unhandled exception", extra={"ctx_path": request.url.path})
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "error",
                "message": "Internal server error",
                "trace_id": getattr(request.state, "trace_id", None),
            },
        )

    _register_routes(app)

    # ── Static demo UI ─────────────────────────────────────────────────────────
    # Serves the single-page "Ask-your-documents" UI at "/" and its assets under
    # /static. Same-origin, so no CORS grant required.
    _static_dir = Path(__file__).parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            index_file = _static_dir / "index.html"
            if index_file.is_file():
                return FileResponse(str(index_file))
            return JSONResponse(content={"status": "ok", "message": "AEOS API is running."})

    # ── Validation routes (Phase 9B.6 Priority 1) ──────────────────────────────
    from app.api.validation import router as validation_router
    app.include_router(
        validation_router,
        prefix=settings.api_prefix,
        dependencies=[Depends(default_guard)],
    )

    return app


# ── Schemas ────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    model_config = {"extra": "forbid"}
    task: str = Field(..., min_length=1, max_length=4000)
    # Bounded + format-restricted (lowercase words/hyphens) to reject junk/injection
    # while staying permissive about the exact mode string the orchestrator accepts.
    mode: str = Field(default="single-agent", max_length=32, pattern=r"^[a-z][a-z-]*$")


# ── RAG request models (bounded + validated) ───────────────────────────────────

class RagIngestRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=1_000_000)
    source: str = Field(default="api", max_length=200)
    namespace: str = Field(default="default", max_length=64)


class RagQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    namespace: str = Field(default="default", max_length=64)


class RagAnswerRequest(RagQueryRequest):
    """Same shape as a query; returns a generated cited answer instead of chunks."""


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    timestamp: str


class GitHubAnalyzeRequest(BaseModel):
    model_config = {"extra": "forbid"}
    repo: str = Field(
        ..., min_length=3, max_length=140,
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
        description="GitHub repo in 'owner/repo' format", examples=["octocat/Hello-World"],
    )
    index_into_rag: bool = Field(default=True)
    file_extensions: list[str] = Field(default=[".py", ".md", ".txt"], max_length=20)

    @field_validator("file_extensions")
    @classmethod
    def _validate_extensions(cls, exts: list[str]) -> list[str]:
        import re
        for e in exts:
            if not re.match(r"^\.[A-Za-z0-9]{1,10}$", e):
                raise ValueError(f"Invalid file extension: {e!r} (expected like '.py')")
        return exts


class MLTrainRequest(BaseModel):
    model_config = {"extra": "forbid"}
    # dataset_path is confined to the datasets directory at the route (see ml_train);
    # here it is only length/format-bounded. Prefer inline_data.
    dataset_path: str | None = Field(default=None, max_length=512, description="Filename under the datasets dir (CSV/JSON)")
    inline_data: list[dict] | None = Field(default=None, max_length=50_000, description="Inline rows as list of dicts")
    target_column: str = Field(..., min_length=1, max_length=128)
    algorithm: str = Field(default="random_forest", max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    model_name: str = Field(..., min_length=1, max_length=200)
    hyperparams: dict[str, Any] = Field(default_factory=dict)
    test_size: float = Field(default=0.2, ge=0.05, le=0.5)

    @field_validator("hyperparams")
    @classmethod
    def _bound_hyperparams(cls, hp: dict) -> dict:
        if len(hp) > 50:
            raise ValueError("Too many hyperparameters (max 50)")
        return hp


# ── Route registration ─────────────────────────────────────────────────────────

def _register_routes(app: FastAPI) -> None:

    # ── /metrics (Prometheus scrape endpoint) ─────────────────────────────────
    @app.get("/metrics", tags=["Observability"], include_in_schema=False)
    async def prometheus_metrics(request: Request):
        """Prometheus text format metrics endpoint (scraped by Prometheus)."""
        exporter: PrometheusExporter = getattr(
            request.app.state, "prometheus_exporter", _prometheus_exporter
        )
        return PlainTextResponse(
            content=exporter.export(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    # ── /health ────────────────────────────────────────────────────────────────
    @app.get("/health", response_model=HealthResponse, tags=["System"])
    async def health(request: Request):
        orchestrator: Orchestrator = request.app.state.orchestrator
        return {
            "status": "healthy" if orchestrator._ready else "starting",
            "version": settings.app_version,
            "environment": settings.environment,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── /run ───────────────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/run", tags=["Orchestration"], dependencies=[Depends(expensive_guard)])
    async def run_task(request: Request, body: RunRequest):
        orchestrator: Orchestrator = request.app.state.orchestrator
        response = await orchestrator.run_task(task=body.task, mode=body.mode)
        http_status = status.HTTP_200_OK if response.status == "success" else status.HTTP_422_UNPROCESSABLE_ENTITY
        return JSONResponse(content=response.to_dict(), status_code=http_status)

    # ── /debug/state ───────────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/debug/state", tags=["Debug"], include_in_schema=settings.debug)
    async def debug_state(request: Request):
        if not settings.debug:
            raise HTTPException(status_code=403, detail="Debug endpoints are disabled in production mode.")
        orchestrator: Orchestrator = request.app.state.orchestrator
        kernel = getattr(request.app.state, "kernel", None)
        state = orchestrator.state()
        if kernel:
            state["kernel"] = kernel.introspect()
        return JSONResponse(content=state)

    # ── /kernel/health ─────────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/kernel/health", tags=["HyperKernel"])
    async def kernel_health(request: Request):
        kernel = getattr(request.app.state, "kernel", None)
        if kernel is None:
            return JSONResponse(
                status_code=503,
                content={"status": "kernel_unavailable", "message": "HyperKernel did not start successfully."},
            )
        snapshot = await kernel.check_health_now()
        return JSONResponse(content={
            "status": "healthy" if snapshot.healthy else "degraded",
            "kernel_state": snapshot.state,
            "plugins_loaded": snapshot.plugins_loaded,
            "services_registered": snapshot.services_registered,
            "uptime_seconds": snapshot.uptime_seconds,
            "failed_components": snapshot.failed_components,
        })

    # ── /kernel/introspect ─────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/kernel/introspect", tags=["HyperKernel"], include_in_schema=settings.debug)
    async def kernel_introspect(request: Request):
        if not settings.debug:
            raise HTTPException(status_code=403, detail="Debug endpoints are disabled in production mode.")
        kernel = getattr(request.app.state, "kernel", None)
        if kernel is None:
            return JSONResponse(status_code=503, content={"error": "Kernel unavailable"})
        return JSONResponse(content=kernel.introspect())

    # ── /execution/introspect ─────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/execution/introspect", tags=["Execution Engine"])
    async def execution_introspect(request: Request):
        """DEE runtime state: metrics, events, checkpoints, trace count, last graph."""
        engine = getattr(request.app.state, "execution_engine", None)
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "ExecutionEngine unavailable"})
        return JSONResponse(content=engine.introspect())

    # ── /execution/graph ──────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/execution/graph", tags=["Execution Engine"])
    async def execution_graph(request: Request, fmt: Literal["json", "mermaid", "dot", "ascii"] = "json"):
        """
        Export the last execution graph in the requested format.
        ?fmt=json (default) | mermaid | dot | ascii
        """
        engine = getattr(request.app.state, "execution_engine", None)
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "ExecutionEngine unavailable"})
        graph = engine.last_graph
        if graph is None:
            return JSONResponse(status_code=404, content={"error": "No execution graph available yet"})
        viz = engine.visualizer
        if fmt == "mermaid":
            return JSONResponse(content={"format": "mermaid", "graph": viz.to_mermaid(graph)})
        if fmt == "dot":
            return JSONResponse(content={"format": "dot", "graph": viz.to_dot(graph)})
        if fmt == "ascii":
            return JSONResponse(content={"format": "ascii", "graph": viz.to_ascii(graph)})
        return JSONResponse(content=viz.to_json(graph))

    # ── /execution/metrics ────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/execution/metrics", tags=["Execution Engine"])
    async def execution_metrics(request: Request, fmt: Literal["json", "prometheus"] = "json"):
        """
        Per-node and workflow metrics.
        ?fmt=json (default) | prometheus
        """
        engine = getattr(request.app.state, "execution_engine", None)
        if engine is None:
            return JSONResponse(status_code=503, content={"error": "ExecutionEngine unavailable"})
        if fmt == "prometheus":
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse(
                content=engine.metrics.export_prometheus(),
                media_type="text/plain; version=0.0.4",
            )
        node_metrics = [nm.to_dict() for nm in engine.metrics.all_node_metrics()]
        return JSONResponse(content={
            "summary": engine.metrics.summary(),
            "nodes": node_metrics,
        })

    # ── /execute ───────────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/execute", tags=["HyperKernel"], dependencies=[Depends(expensive_guard)])
    async def execute_task(request: Request, body: RunRequest):
        """
        Execute a task through the 15-stage Execution Engine pipeline.
        Falls back to the orchestrator if the execution engine is unavailable.
        """
        engine = getattr(request.app.state, "execution_engine", None)
        trace_id = getattr(request.state, "trace_id", "")

        if engine is not None:
            result = await engine.run(
                task=body.task,
                mode=body.mode,
                caller_id="api",
                trace_id=trace_id,
            )
            http_status = status.HTTP_200_OK if result.status != "failed" else status.HTTP_422_UNPROCESSABLE_ENTITY
            return JSONResponse(content=result.to_dict(), status_code=http_status)

        # Fallback to orchestrator
        orchestrator: Orchestrator = request.app.state.orchestrator
        response = await orchestrator.run_task(task=body.task, mode=body.mode)
        http_status = status.HTTP_200_OK if response.status == "success" else status.HTTP_422_UNPROCESSABLE_ENTITY
        return JSONResponse(content=response.to_dict(), status_code=http_status)

    # ── /github/analyze ────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/github/analyze", tags=["GitHub Analyzer"], dependencies=[Depends(expensive_guard)])
    async def analyze_github_repo(request: Request, body: GitHubAnalyzeRequest):
        from app.github_analyzer.indexer import RepoIndexer
        try:
            indexer = RepoIndexer()
            stats = await indexer.index_repo(
                repo_full_name=body.repo,
                index_into_rag=body.index_into_rag,
                file_extensions=body.file_extensions,
            )
            return JSONResponse(content={"status": "success", "result": stats})
        except ValueError:
            # Client-side problem (bad/inaccessible repo, missing token). Log the
            # detail server-side; return a controlled message with no internals.
            log.warning("GitHub analyze rejected", extra={"ctx_repo": body.repo})
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "error", "message": "Invalid request or repository not accessible."},
            )
        except Exception as exc:
            return _api_error(request, exc, "GitHub analyze failed")

    # ── /ml/train ──────────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/ml/train", tags=["ML Pipeline"], dependencies=[Depends(expensive_guard)])
    async def ml_train(request: Request, body: MLTrainRequest):
        from app.ml_pipeline.dataset import DatasetLoader
        from app.ml_pipeline.trainer import ModelTrainer
        from app.ml_pipeline.evaluator import ModelEvaluator
        from app.ml_pipeline.registry import ModelRegistry

        try:
            loader = DatasetLoader()
            if body.inline_data:
                df, meta = loader.load_inline(body.inline_data, name=body.model_name, target_col=body.target_column)
            elif body.dataset_path:
                # SECURITY: confine to the datasets directory. Prevents arbitrary
                # file / URL reads via pandas (path traversal, absolute paths, URLs).
                try:
                    safe_path = safe_resolve(settings.ml_dataset_path, body.dataset_path)
                except SecurityError:
                    return JSONResponse(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        content={"status": "error", "message": "dataset_path must be a file within the datasets directory."},
                    )
                if not safe_path.is_file():
                    return JSONResponse(
                        status_code=status.HTTP_404_NOT_FOUND,
                        content={"status": "error", "message": "Dataset file not found."},
                    )
                ext = safe_path.suffix.lower()
                if ext == ".json":
                    df, meta = loader.load_json(str(safe_path), target_col=body.target_column)
                else:
                    df, meta = loader.load_csv(str(safe_path), target_col=body.target_column)
            else:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={"status": "error", "message": "Provide either 'inline_data' or 'dataset_path'."},
                )

            trainer = ModelTrainer()
            train_result = trainer.train(
                df=df,
                target_col=body.target_column,
                algorithm=body.algorithm,
                test_size=body.test_size,
                hyperparams=body.hyperparams or None,
            )

            evaluator = ModelEvaluator()
            metrics = evaluator.evaluate(
                model=train_result["model"],
                X_test=train_result["X_test"],
                y_test=train_result["y_test"],
            )
            # Remove non-serializable 'report' string for JSON response
            serializable_metrics = {k: v for k, v in metrics.items() if k != "report"}

            registry = ModelRegistry()
            record = registry.save(
                model=train_result["model"],
                name=body.model_name,
                algorithm=body.algorithm,
                dataset_id=meta.id,
                metrics=serializable_metrics,
                feature_names=train_result["feature_names"],
            )

            return JSONResponse(content={
                "status": "success",
                "model_id": record.model_id,
                "name": record.name,
                "algorithm": record.algorithm,
                "metrics": serializable_metrics,
                "dataset": {"id": meta.id, "rows": meta.row_count, "features": meta.feature_columns},
                "train_meta": train_result["train_meta"],
            })

        except (ValueError, KeyError):
            # Bad training input (unknown algorithm, missing target column, etc.).
            log.warning("ML training rejected", extra={"ctx_model": body.model_name})
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "error", "message": "Invalid training request or dataset."},
            )
        except Exception as exc:
            return _api_error(request, exc, "ML training failed")

    # ── /ml/models ─────────────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/ml/models", tags=["ML Pipeline"])
    async def ml_list_models(request: Request):
        from app.ml_pipeline.registry import ModelRegistry
        from dataclasses import asdict
        registry = ModelRegistry()
        models = registry.list_models()
        return JSONResponse(content={
            "status": "success",
            "count": len(models),
            "models": [
                {k: v for k, v in asdict(m).items() if k not in ("model_path", "meta_path")}
                for m in models
            ],
        })

    # ── /rag/ingest ────────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/rag/ingest", tags=["RAG Engine"], dependencies=[Depends(rag_guard)])
    async def rag_ingest(request: Request, body: RagIngestRequest):
        from app.rag.rag_engine import get_rag_engine
        try:
            namespace = validate_namespace(body.namespace)
        except SecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            engine = get_rag_engine(namespace)
            chunks = engine.ingest_text(body.text, source=body.source)
        except Exception as exc:
            return _rag_error(request, exc)
        return JSONResponse(content={"status": "success", "chunks_added": chunks, "namespace": namespace})

    # ── /rag/query (raw ranked chunks) ─────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/rag/query", tags=["RAG Engine"], dependencies=[Depends(rag_guard)])
    async def rag_query(request: Request, body: RagQueryRequest):
        from app.rag.rag_engine import get_rag_engine
        try:
            namespace = validate_namespace(body.namespace)
        except SecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            engine = get_rag_engine(namespace)
            results = engine.query(body.query, top_k=body.top_k)
        except Exception as exc:
            return _rag_error(request, exc)
        return JSONResponse(content={
            "status": "success",
            "query": body.query,
            "results": [
                {"text": r.text, "score": r.score, "rank": r.rank, "source": r.metadata.get("source", "")}
                for r in results
            ],
        })

    # ── /rag/answer (grounded, cited answer — the "G" in RAG) ──────────────────
    @app.post(f"{settings.api_prefix}/rag/answer", tags=["RAG Engine"], dependencies=[Depends(rag_guard)])
    async def rag_answer(request: Request, body: RagAnswerRequest):
        from app.rag.rag_engine import get_rag_engine
        try:
            namespace = validate_namespace(body.namespace)
        except SecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        try:
            engine = get_rag_engine(namespace)
            result = engine.answer(body.query, top_k=body.top_k)
        except Exception as exc:
            return _rag_error(request, exc)
        return JSONResponse(content={"status": "success", **result.to_dict()})

    # ── /rag/upload (hardened file ingestion) ──────────────────────────────────
    @app.post(f"{settings.api_prefix}/rag/upload", tags=["RAG Engine"], dependencies=[Depends(rag_guard)])
    async def rag_upload(
        request: Request,
        file: UploadFile = File(...),
        namespace: str = Form("default"),
    ):
        from app.rag.rag_engine import get_rag_engine
        # 1. Validate namespace + file extension before touching disk.
        try:
            ns = validate_namespace(namespace)
            ext = validate_upload_extension(file.filename or "")
        except SecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        # 2. Read with a hard size cap (never trust Content-Length).
        data = await file.read(MAX_FILE_SIZE_BYTES + 1)
        if len(data) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"File too large (> {MAX_FILE_SIZE_BYTES // 1_048_576} MB).")
        if not data:
            raise HTTPException(status_code=422, detail="Empty file.")

        # 2b. Content-based validation: bytes must match the claimed type and must
        #     never be an executable/archive (defeats extension spoofing).
        try:
            validate_upload_content(ext, data)
        except SecurityError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        # 3. Write to a confined temp dir under a sanitised name, ingest, delete.
        upload_root = Path(tempfile.gettempdir()) / "aeos_uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid.uuid4().hex}_{sanitize_filename(file.filename or 'upload')}"
        dest = safe_resolve(upload_root, safe_name)
        clean_name = sanitize_filename(file.filename or "upload")
        try:
            dest.write_bytes(data)
            engine = get_rag_engine(ns)
            # Store the original filename as the source, never the temp path.
            chunks = engine.ingest_file(str(dest), source=clean_name)
        except Exception as exc:
            return _rag_error(request, exc)
        finally:
            try:
                dest.unlink(missing_ok=True)
            except Exception:
                pass
        return JSONResponse(content={
            "status": "success",
            "filename": clean_name,
            "chunks_added": chunks,
            "namespace": ns,
        })


# ── App singleton ──────────────────────────────────────────────────────────────
app = create_app()
