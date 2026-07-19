"""
AEOS Integration Tests — production security hardening (Phases 1,2,5,6)

Covers: tiered rate limiting (429 + Retry-After), strict input validation on
every endpoint, no information leakage on errors, and content-based upload
validation. Reuses the session-scoped `async_client` fixture.
"""
import pytest


# ── Phase 2: strict input validation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_rejects_unknown_field(async_client):
    # extra="forbid" → unexpected fields are rejected, not silently ignored.
    resp = await async_client.post("/api/v1/run", json={"task": "hi", "surprise": 1})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_run_rejects_malformed_mode(async_client):
    resp = await async_client.post("/api/v1/run", json={"task": "hi", "mode": "DROP TABLE"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_github_rejects_bad_repo_format(async_client):
    resp = await async_client.post("/api/v1/github/analyze", json={"repo": "not-a-repo"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_github_rejects_bad_extension(async_client):
    resp = await async_client.post(
        "/api/v1/github/analyze",
        json={"repo": "octocat/Hello-World", "file_extensions": [".py; rm -rf"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ml_train_rejects_bad_algorithm(async_client):
    resp = await async_client.post("/api/v1/ml/train", json={
        "target_column": "y", "model_name": "m", "algorithm": "Bad Algo!",
        "inline_data": [{"a": 1, "y": 0}],
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_execution_graph_rejects_bad_fmt(async_client):
    resp = await async_client.get("/api/v1/execution/graph?fmt=evil")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_validation_limit_out_of_range(async_client):
    resp = await async_client.get("/api/v1/validation/violations?limit=99999")
    assert resp.status_code == 422


# ── Phase 2 + 5 + 6: path confinement + no leakage on /ml/train ──────────────

@pytest.mark.asyncio
async def test_ml_train_dataset_path_traversal_blocked(async_client):
    resp = await async_client.post("/api/v1/ml/train", json={
        "target_column": "y", "model_name": "m",
        "dataset_path": "../../../../etc/passwd",
    })
    assert resp.status_code == 422
    body = resp.json()
    # Controlled message — never echoes the attempted path or a stack trace.
    assert "datasets directory" in body["message"]
    assert "etc/passwd" not in body["message"]


# ── Phase 6: content-based upload validation ─────────────────────────────────

@pytest.mark.asyncio
async def test_upload_executable_disguised_as_txt_rejected(async_client):
    # A Windows PE ("MZ...") renamed to .txt must be rejected by content sniffing.
    resp = await async_client.post(
        "/api/v1/rag/upload",
        files={"file": ("innocent.txt", b"MZ\x90\x00\x03 evil payload", "text/plain")},
        data={"namespace": "sectest"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_fake_pdf_rejected(async_client):
    resp = await async_client.post(
        "/api/v1/rag/upload",
        files={"file": ("doc.pdf", b"this is not a pdf at all", "application/pdf")},
        data={"namespace": "sectest"},
    )
    assert resp.status_code == 422


# ── Phase 1: rate limiting (429 + Retry-After) ───────────────────────────────

@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after(async_client):
    import app.main as m
    limiter = m._rl_default
    saved_cap = limiter._capacity
    try:
        # Shrink the shared 'default' limiter to a single token and hit a light
        # read endpoint twice: first passes, second is throttled.
        limiter._capacity = 1.0
        limiter._buckets.clear()
        r1 = await async_client.get("/api/v1/validation/invariants")
        r2 = await async_client.get("/api/v1/validation/invariants")
        assert r1.status_code == 200
        assert r2.status_code == 429
        assert "retry-after" in {k.lower() for k in r2.headers.keys()}
    finally:
        limiter._capacity = saved_cap
        limiter._buckets.clear()
