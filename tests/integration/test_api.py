"""
AEOS Integration Tests — FastAPI Endpoints
Tests all public routes with the full app lifecycle.
"""

import pytest


# ── /health ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_returns_200(async_client):
    resp = await async_client.get("/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_health_body_schema(async_client):
    resp = await async_client.get("/health")
    body = resp.json()
    assert "status" in body
    assert "version" in body
    assert "environment" in body
    assert "timestamp" in body
    assert body["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_trace_id_in_response_header(async_client):
    resp = await async_client.get("/health")
    assert "x-trace-id" in resp.headers


# ── /api/v1/run — single-agent ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_single_agent_success(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "analyze this codebase", "mode": "single-agent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["agent"] == "simple_agent"
    assert "result" in body
    assert "metadata" in body


@pytest.mark.asyncio
async def test_run_response_has_trace_id(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "trace test", "mode": "single-agent"},
    )
    body = resp.json()
    assert "trace_id" in body["metadata"]
    assert body["metadata"]["trace_id"] != ""


@pytest.mark.asyncio
async def test_run_response_has_latency(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "latency test", "mode": "single-agent"},
    )
    body = resp.json()
    assert "latency_ms" in body["metadata"]
    assert body["metadata"]["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_run_empty_task_returns_422(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "", "mode": "single-agent"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_missing_task_field_returns_422(async_client):
    resp = await async_client.post("/api/v1/run", json={"mode": "single-agent"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_task_too_long_returns_422(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "x" * 4001, "mode": "single-agent"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_default_mode_is_single_agent(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "default mode check"},
    )
    assert resp.status_code == 200
    assert resp.json()["agent"] == "simple_agent"


# ── /api/v1/run — multi-agent ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_multi_agent_success(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "research and summarize AEOS architecture", "mode": "multi-agent"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["agent"] == "multi-agent-pipeline"
    assert "step_results" in body["result"]
    assert "plan" in body["result"]


@pytest.mark.asyncio
async def test_run_multi_agent_has_review(async_client):
    resp = await async_client.post(
        "/api/v1/run",
        json={"task": "find and analyze code patterns", "mode": "multi-agent"},
    )
    body = resp.json()
    assert body["result"].get("review") is not None
    review = body["result"]["review"]
    assert "verdict" in review
    assert review["verdict"] in ("PASS", "REVISE", "REJECT")


# ── /api/v1/debug/state ────────────────────────────────────────────────────────

@pytest.fixture
def debug_mode():
    """Enable debug mode for /debug routes (they return 403 when debug=False,
    per HIGH-001 production hardening). Restores the original value afterward."""
    from app.core.config import settings
    original = settings.debug
    settings.debug = True
    yield
    settings.debug = original


@pytest.mark.asyncio
async def test_debug_state_returns_200(async_client, debug_mode):
    resp = await async_client.get("/api/v1/debug/state")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_debug_state_has_agents(async_client, debug_mode):
    resp = await async_client.get("/api/v1/debug/state")
    body = resp.json()
    assert "agents" in body
    agent_ids = [a["id"] for a in body["agents"]]
    assert "simple_agent" in agent_ids
    assert "planner_agent" in agent_ids
    assert "research_agent" in agent_ids
    assert "reviewer_agent" in agent_ids


@pytest.mark.asyncio
async def test_debug_state_shows_ready(async_client, debug_mode):
    resp = await async_client.get("/api/v1/debug/state")
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_debug_state_forbidden_in_production(async_client):
    """HIGH-001: /debug/state must be denied when debug is disabled."""
    from app.core.config import settings
    original = settings.debug
    settings.debug = False
    try:
        resp = await async_client.get("/api/v1/debug/state")
        assert resp.status_code == 403
    finally:
        settings.debug = original


# ── /api/v1/rag ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rag_ingest_text(async_client):
    resp = await async_client.post(
        "/api/v1/rag/ingest",
        json={"text": "AEOS uses ChromaDB for vector storage.", "source": "integration_test"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["chunks_added"] >= 1


@pytest.mark.asyncio
async def test_rag_ingest_empty_text_returns_422(async_client):
    # Empty text now fails Pydantic min_length validation → 422 (was a manual 400).
    resp = await async_client.post("/api/v1/rag/ingest", json={"text": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_rag_query_after_ingest(async_client):
    # Ingest first
    await async_client.post(
        "/api/v1/rag/ingest",
        json={"text": "FastAPI is used as the HTTP framework in AEOS.", "source": "api_test"},
    )
    # Then query
    resp = await async_client.post(
        "/api/v1/rag/query",
        json={"query": "HTTP framework"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body


@pytest.mark.asyncio
async def test_rag_query_empty_query_returns_422(async_client):
    # Empty query now fails Pydantic min_length validation → 422 (was a manual 400).
    resp = await async_client.post("/api/v1/rag/query", json={"query": ""})
    assert resp.status_code == 422


# ── /api/v1/ml ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ml_list_models_returns_list(async_client):
    resp = await async_client.get("/api/v1/ml/models")
    assert resp.status_code == 200
    body = resp.json()
    assert "models" in body
    assert isinstance(body["models"], list)


@pytest.mark.asyncio
async def test_ml_train_with_inline_data(async_client, ml_inline_data):
    resp = await async_client.post(
        "/api/v1/ml/train",
        json={
            "inline_data": ml_inline_data,
            "target_column": "label",
            "algorithm": "random_forest",
            "model_name": "integration-test-model",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "model_id" in body
    assert "metrics" in body
    assert "accuracy" in body["metrics"]


@pytest.mark.asyncio
async def test_ml_train_missing_data_returns_400(async_client):
    resp = await async_client.post(
        "/api/v1/ml/train",
        json={"target_column": "label", "algorithm": "random_forest", "model_name": "x"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ml_train_invalid_algorithm_returns_400(async_client, ml_inline_data):
    resp = await async_client.post(
        "/api/v1/ml/train",
        json={
            "inline_data": ml_inline_data,
            "target_column": "label",
            "algorithm": "nonexistent_algo",
            "model_name": "bad-algo-test",
        },
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_ml_trained_model_appears_in_list(async_client, ml_inline_data):
    # Train a model
    train_resp = await async_client.post(
        "/api/v1/ml/train",
        json={
            "inline_data": ml_inline_data,
            "target_column": "label",
            "algorithm": "logistic_regression",
            "model_name": "list-test-model",
        },
    )
    model_id = train_resp.json()["model_id"]

    # Verify it appears in list
    list_resp = await async_client.get("/api/v1/ml/models")
    model_ids = [m["model_id"] for m in list_resp.json()["models"]]
    assert model_id in model_ids
