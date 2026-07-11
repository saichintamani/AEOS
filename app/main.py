"""
AEOS FastAPI Application
Entry point for the entire platform.
Owns: lifespan management, route registration, middleware, DI wiring.

Phase 8A:    AEOS HyperKernel wired into lifespan.
Phase 9B.6:  InvariantEngine, Prometheus /metrics, validation routes.
"""

import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

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

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.debug else [],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
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

    # ── Validation routes (Phase 9B.6 Priority 1) ──────────────────────────────
    from app.api.validation import router as validation_router
    app.include_router(validation_router, prefix=settings.api_prefix)

    return app


# ── Schemas ────────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    task: str = Field(..., min_length=1, max_length=4000)
    mode: str = Field(default="single-agent", description="'single-agent' | 'multi-agent'")


class HealthResponse(BaseModel):
    status: str
    version: str
    environment: str
    timestamp: str


class GitHubAnalyzeRequest(BaseModel):
    repo: str = Field(..., description="GitHub repo in 'owner/repo' format", examples=["octocat/Hello-World"])
    index_into_rag: bool = Field(default=True)
    file_extensions: list[str] = Field(default=[".py", ".md", ".txt"])


class MLTrainRequest(BaseModel):
    dataset_path: str | None = Field(default=None, description="Path to CSV or JSON file")
    inline_data: list[dict] | None = Field(default=None, description="Inline rows as list of dicts")
    target_column: str = Field(..., description="Column name to predict")
    algorithm: str = Field(default="random_forest")
    model_name: str = Field(..., description="Human-readable name for the saved model")
    hyperparams: dict[str, Any] = Field(default_factory=dict)
    test_size: float = Field(default=0.2, ge=0.05, le=0.5)


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
    @app.post(f"{settings.api_prefix}/run", tags=["Orchestration"])
    async def run_task(request: Request, body: RunRequest):
        orchestrator: Orchestrator = request.app.state.orchestrator
        response = await orchestrator.run_task(task=body.task, mode=body.mode)
        http_status = status.HTTP_200_OK if response.status == "success" else status.HTTP_422_UNPROCESSABLE_ENTITY
        return JSONResponse(content=response.to_dict(), status_code=http_status)

    # ── /debug/state ───────────────────────────────────────────────────────────
    @app.get(f"{settings.api_prefix}/debug/state", tags=["Debug"], include_in_schema=settings.debug)
    async def debug_state(request: Request):
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
    async def execution_graph(request: Request, fmt: str = "json"):
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
    async def execution_metrics(request: Request, fmt: str = "json"):
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
    @app.post(f"{settings.api_prefix}/execute", tags=["HyperKernel"])
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
    @app.post(f"{settings.api_prefix}/github/analyze", tags=["GitHub Analyzer"])
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
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "error", "message": str(exc)},
            )
        except Exception as exc:
            log.exception("GitHub analyze failed", extra={"ctx_repo": body.repo})
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"status": "error", "message": str(exc)},
            )

    # ── /ml/train ──────────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/ml/train", tags=["ML Pipeline"])
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
                ext = body.dataset_path.rsplit(".", 1)[-1].lower()
                if ext == "json":
                    df, meta = loader.load_json(body.dataset_path, target_col=body.target_column)
                else:
                    df, meta = loader.load_csv(body.dataset_path, target_col=body.target_column)
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

        except (ValueError, KeyError) as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "error", "message": str(exc)},
            )
        except Exception as exc:
            log.exception("ML training failed")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"status": "error", "message": str(exc)},
            )

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
    @app.post(f"{settings.api_prefix}/rag/ingest", tags=["RAG Engine"])
    async def rag_ingest(request: Request, body: dict):
        from app.rag.rag_engine import get_rag_engine
        text = body.get("text", "")
        source = body.get("source", "api")
        namespace = body.get("namespace", "default")
        if not text:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "error", "message": "Field 'text' is required."},
            )
        engine = get_rag_engine(namespace)
        chunks = engine.ingest_text(text, source=source)
        return JSONResponse(content={"status": "success", "chunks_added": chunks, "namespace": namespace})

    # ── /rag/query ─────────────────────────────────────────────────────────────
    @app.post(f"{settings.api_prefix}/rag/query", tags=["RAG Engine"])
    async def rag_query(request: Request, body: dict):
        from app.rag.rag_engine import get_rag_engine
        query = body.get("query", "")
        top_k = body.get("top_k", settings.rag_top_k)
        namespace = body.get("namespace", "default")
        if not query:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"status": "error", "message": "Field 'query' is required."},
            )
        engine = get_rag_engine(namespace)
        results = engine.query(query, top_k=top_k)
        return JSONResponse(content={
            "status": "success",
            "query": query,
            "results": [
                {"text": r.text, "score": r.score, "rank": r.rank, "source": r.metadata.get("source", "")}
                for r in results
            ],
        })


# ── App singleton ──────────────────────────────────────────────────────────────
app = create_app()
