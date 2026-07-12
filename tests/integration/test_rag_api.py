"""
AEOS Integration Tests — hardened RAG API (/rag/answer, /rag/upload, security)

Reuses the session-scoped `async_client` fixture (which pre-warms the RAG
engine). Uses a dedicated namespace and resets it on teardown so tests don't
leave persisted data behind.
"""
import pytest

NS = "apitest"


@pytest.fixture(autouse=True)
def _clean_namespace():
    yield
    # Remove any persisted data for the test namespace.
    from app.rag.rag_engine import get_rag_engine
    get_rag_engine(NS).reset()


# ── /rag/answer — the generation endpoint ────────────────────────────────────

@pytest.mark.asyncio
async def test_answer_returns_grounded_citation(async_client):
    await async_client.post("/api/v1/rag/ingest", json={
        "text": "The AEOS execution engine runs a DAG of nodes with retries and checkpoints.",
        "source": "engine_doc", "namespace": NS,
    })
    resp = await async_client.post("/api/v1/rag/answer", json={
        "query": "What does the AEOS execution engine do?", "namespace": NS,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["answer"]
    assert body["used_generator"] in ("extractive", "openai")
    assert len(body["citations"]) >= 1
    assert body["citations"][0]["marker"] == 1
    assert "[1]" in body["answer"]


@pytest.mark.asyncio
async def test_answer_empty_kb_says_dont_know(async_client):
    resp = await async_client.post("/api/v1/rag/answer", json={
        "query": "anything at all", "namespace": NS,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["confidence"] == 0.0
    assert body["citations"] == []


# ── Security: bounds + validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_namespace_rejected(async_client):
    resp = await async_client.post("/api/v1/rag/query", json={
        "query": "x", "namespace": "../etc",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_top_k_over_limit_rejected(async_client):
    resp = await async_client.post("/api/v1/rag/query", json={
        "query": "x", "top_k": 9999, "namespace": NS,
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_oversized_text_rejected(async_client):
    resp = await async_client.post("/api/v1/rag/ingest", json={
        "text": "a" * 1_000_001, "namespace": NS,
    })
    assert resp.status_code == 422


# ── /rag/upload — hardened file ingestion ────────────────────────────────────

@pytest.mark.asyncio
async def test_upload_markdown(async_client):
    content = b"# Notes\n\nAEOS stores embeddings as npz and json, never pickle."
    resp = await async_client.post(
        "/api/v1/rag/upload",
        files={"file": ("notes.md", content, "text/markdown")},
        data={"namespace": NS},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["chunks_added"] >= 1
    assert body["filename"] == "notes.md"  # not a temp path


@pytest.mark.asyncio
async def test_upload_disallowed_type_rejected(async_client):
    resp = await async_client.post(
        "/api/v1/rag/upload",
        files={"file": ("evil.exe", b"MZ\x00", "application/octet-stream")},
        data={"namespace": NS},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_source_is_filename_not_path(async_client):
    content = b"AEOS confines uploads to a temp directory."
    await async_client.post(
        "/api/v1/rag/upload",
        files={"file": ("confine.txt", content, "text/plain")},
        data={"namespace": NS},
    )
    resp = await async_client.post("/api/v1/rag/query", json={
        "query": "temp directory", "namespace": NS,
    })
    sources = [r["source"] for r in resp.json()["results"]]
    # The stored source is the clean filename, never the server temp path.
    assert any(s == "confine.txt" for s in sources)
    assert not any("aeos_uploads" in s for s in sources)
