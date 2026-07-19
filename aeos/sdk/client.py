"""
AEOS SDK — AEOSClient

Async HTTP client for the AEOS Runtime API.

Usage::

    async with AEOSClient("http://localhost:8000") as client:
        result = await client.run("Summarise this document")
        print(result.result)

        # RAG
        await client.ingest("My document text", source="my-doc.txt")
        results = await client.query("What is the main topic?")

        # Metrics / validation
        health = await client.health()
        violations = await client.validation_status()
"""

from __future__ import annotations

from typing import Any

from aeos.sdk.types import RunResult, WorkflowResult, StepResult


class AEOSClient:
    """
    Async HTTP client wrapping the AEOS REST API.

    Can be used as an async context manager or manually opened/closed.
    """

    def __init__(self, base_url: str = "http://localhost:8000", timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client: Any = None  # httpx.AsyncClient

    async def __aenter__(self) -> "AEOSClient":
        await self._open()
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def _open(self) -> None:
        try:
            import httpx
        except ImportError:
            raise ImportError("AEOSClient requires httpx: pip install httpx")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_open(self) -> Any:
        if self._client is None:
            raise RuntimeError("AEOSClient is not open. Use 'async with AEOSClient(...) as client:'")
        return self._client

    # ── Core API ──────────────────────────────────────────────────────────────

    async def health(self) -> dict:
        """Check the AEOS runtime health."""
        resp = await self._ensure_open().get("/health")
        return resp.json()

    async def run(self, task: str, mode: str = "single-agent") -> RunResult:
        """Submit a task to the AEOS orchestrator."""
        resp = await self._ensure_open().post(
            "/api/v1/run",
            json={"task": task, "mode": mode},
        )
        return RunResult.from_api(resp.json())

    async def execute(self, task: str, mode: str = "single-agent") -> RunResult:
        """Submit a task through the full 15-stage Execution Engine."""
        resp = await self._ensure_open().post(
            "/api/v1/execute",
            json={"task": task, "mode": mode},
        )
        return RunResult.from_api(resp.json())

    async def run_workflow(self, compiled_workflow: dict) -> WorkflowResult:
        """Run a pre-compiled workflow dict step-by-step."""
        workflow_result = WorkflowResult(workflow_name=compiled_workflow.get("name", "workflow"))
        for step in compiled_workflow.get("steps", []):
            run = await self.run(step["task"], mode=step.get("mode", "single-agent"))
            workflow_result.steps.append(StepResult(step_name=step["name"], run_result=run))
        return workflow_result

    # ── RAG API ──────────────────────────────────────────────────────────────

    async def ingest(
        self,
        text: str,
        source: str = "sdk",
        namespace: str = "default",
    ) -> dict:
        """Ingest text into the RAG knowledge base."""
        resp = await self._ensure_open().post(
            "/api/v1/rag/ingest",
            json={"text": text, "source": source, "namespace": namespace},
        )
        return resp.json()

    async def query(
        self,
        query: str,
        top_k: int = 5,
        namespace: str = "default",
    ) -> list[dict]:
        """Query the RAG knowledge base."""
        resp = await self._ensure_open().post(
            "/api/v1/rag/query",
            json={"query": query, "top_k": top_k, "namespace": namespace},
        )
        return resp.json().get("results", [])

    # ── Observability ─────────────────────────────────────────────────────────

    async def metrics(self) -> str:
        """Get Prometheus text format metrics."""
        resp = await self._ensure_open().get("/metrics")
        return resp.text

    async def validation_status(self) -> dict:
        """Get the invariant engine status and recent violations."""
        resp = await self._ensure_open().get("/api/v1/validation/status")
        return resp.json()

    async def evaluate_invariants(self) -> dict:
        """Trigger an on-demand invariant evaluation."""
        resp = await self._ensure_open().post("/api/v1/validation/evaluate", json={})
        return resp.json()

    async def execution_metrics(self) -> dict:
        """Get per-node execution metrics."""
        resp = await self._ensure_open().get("/api/v1/execution/metrics")
        return resp.json()
