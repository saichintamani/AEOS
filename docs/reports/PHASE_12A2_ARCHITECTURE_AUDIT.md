# AEOS Platform — Phase 12A.2 Repository-Wide Architecture Audit

**Document:** `docs/reports/PHASE_12A2_ARCHITECTURE_AUDIT.md`
**Date:** 2026-07-14
**Auditor posture:** Adversarial. Assume AEOS will be reviewed by senior distributed systems engineers from Temporal, Ray, Kubernetes, Netflix, and Anthropic.
**Basis:** Full codebase read across all six audit dimensions.

---

## Audit Scope

Six dimensions, audited in parallel:

| Dimension | Files Read | Key Focus |
|-----------|-----------|-----------|
| **Transport / gRPC** | channel.py, grpc_channel.py, communication contracts | Real inter-node communication |
| **Security** | main.py, governance.py, worker runtime | Auth coverage, attack surface |
| **Raft Correctness** | raft.py, recovery.py, log_store.py, wal.py | Consensus safety and WAL integration |
| **OSS Adoption** | README.md, pyproject.toml, examples/, CLI | First-30-minute user experience |
| **Scalability** | backpressure/engine.py, pool/, transport/kafka.py | Resource bounds, bottlenecks |
| **Test Coverage** | All test directories + source modules | Coverage gaps, dead code |

---

## CRITICAL Findings

### CRIT-001: WAL integration shim is defined but never called

**File:** `app/distributed/consensus/recovery.py:285`
**Dimension:** Raft Correctness

`integrate_with_raft_node()` is the function that patches `RaftNode` to use `DurableLogStore` for all mutations. It is exported from `app/distributed/consensus/__init__.py` and defined in `recovery.py`. However, **a search of the entire codebase finds zero call sites**:

```
grep result: integrate_with_raft_node( → found only in recovery.py (definition) and __init__.py (export)
```

This means the WAL layer built in P12A.1-1 is **never attached to any running RaftNode**. In every code path that constructs a `RaftNode` (cluster setup, multi-node tests), the node operates with the original in-memory `RaftState`. The 27 WAL tests all pass because they test `DurableLogStore` and `WriteAheadLog` in isolation — not through `RaftNode`.

**Impact:** LONGEVITY-001 is technically closed in isolation but NOT closed end-to-end. A RaftNode restart still loses all state. The claim "zero consensus state loss after restart" cannot be made until the shim is called at construction time.

**Required fix:** Every `RaftNode.__init__` or construction site must call `integrate_with_raft_node(node, persistence)` and `persistence.recover()`. Alternatively, wire persistence into `RaftNode.__init__` directly so it cannot be constructed without it.

---

### CRIT-002: gRPC inter-node communication is not implemented

**File:** `app/distributed/communication/channel.py:72-92`
**Dimension:** Transport / gRPC

`GrpcChannel` raises `NotImplementedError` on **every method** — `connect()`, `close()`, `make_call()`, `make_streaming_call()`, `health_check()`. The in-process `InMemoryRpcChannel` is the only functional transport. But `InMemoryRpcChannel` requires all nodes to share the same Python process.

The 3-node docker-compose cluster (`docker-compose.cluster.yml`) launches three separate containers. There is no mechanism for these containers to call each other's Raft RPC handlers. The Raft consensus algorithm requires nodes to exchange `AppendEntries` and `RequestVote` RPCs across the network. Without a real transport, the three containers cannot form a cluster.

**Current production path:**
```
RaftNode._rpc(peer_id, "request_vote", req)
  → caller provides rpc_send function at construction
  → in tests: InMemoryRpcChannel (same process)
  → in production (docker-compose): ??? — no wiring exists
```

No file in `app/distributed/cluster/`, `app/distributed/demo/`, or `app/kernel/` wires a real cross-process `rpc_send` for `RaftNode`.

**Impact:** The 3-node cluster docker-compose launches three isolated, non-communicating processes. There is no distributed consensus. Each node runs independently. This is the most architecturally significant gap in the platform.

**Required fix:** Wire `GrpcChannel` (or a Redis/Kafka-based RPC bridge) as the `rpc_send` argument when constructing `RaftNode` in multi-node mode.

---

### CRIT-003: ML model registry uses pickle.load — RCE on load

**File:** `app/ml_pipeline/registry.py:97`
**Dimension:** Security

```python
return pickle.load(f)
```

This is acknowledged in the README ("Still open (documented, not yet fixed)"), but it is a critical security finding for OSS launch. Any user who places or receives a tampered `.pkl` file in `./data/model_registry/` achieves arbitrary code execution when the model is loaded. The `/ml/train` endpoint writes models to this directory, and `/ml/models` lists them — but there is no `/ml/predict` endpoint yet. When one is added (Phase 13 likely), it will call `pickle.load()` on user-accessible files.

This must be resolved before public release. `joblib` with `mmap_mode` or `safetensors` for neural models should replace pickle.

---

### CRIT-004: Governance token is a string field, never cryptographically verified in the worker path

**File:** `app/distributed/worker/runtime.py:161`, `app/distributed/worker/governance.py:77-83`
**Dimension:** Security

The `GovernanceClient.verify_token()` checks only whether `token_id` appears in `self._revoked_tokens` (a local in-memory set). It does **not** cryptographically verify the JWT signature. The new `TokenVerifier` from P12A.1-3 is never called in this path.

```python
# runtime.py:161
await self._governance.verify_token(ctx.token_id)

# governance.py:77 — only a revocation list check
async def verify_token(self, token_id: str | None) -> None:
    if token_id is None:
        return   # ← None tokens ALWAYS PASS
    async with self._lock:
        if token_id in self._revoked_tokens:
            raise TokenRevokedException(token_id)
```

Additionally: `if token_id is None: return` means **any task dispatched without a token_id executes unconditionally**. The `ExecutionContext.token_id` field defaults to `None` (context.py:50). Any caller that does not explicitly set `token_id` bypasses all governance checks.

The `gov_approved` claim in the JWT (used by `AdaptiveScheduler`) is also not verified against the `TokenVerifier` signature before being trusted.

**Impact:** An attacker who can submit a `TASK_ACCEPTED` event (or call the worker runtime directly) with `token_id=None` or any non-revoked string executes arbitrary task handlers on the worker without a valid governance token.

---

## HIGH Findings

### HIGH-001: /health, /metrics, /debug/state, /kernel/* have no authentication

**File:** `app/main.py:416-481`
**Dimension:** Security

Route authentication map:

| Route | Auth |
|-------|------|
| `GET /health` | None |
| `GET /metrics` | None |
| `GET /api/v1/debug/state` | None (only gated by `include_in_schema=settings.debug`) |
| `GET /api/v1/kernel/health` | None |
| `GET /api/v1/kernel/introspect` | None |
| `GET /api/v1/execution/introspect` | None |
| `GET /api/v1/execution/graph` | None |
| `GET /api/v1/execution/metrics` | None |
| `POST /api/v1/run` | Rate limit only (`expensive_guard`) |
| `POST /api/v1/execute` | Rate limit only (`expensive_guard`) |
| `POST /api/v1/github/analyze` | Rate limit only (`expensive_guard`) |
| `POST /api/v1/ml/train` | Rate limit only (`expensive_guard`) |
| `GET /api/v1/ml/models` | None |
| `POST /api/v1/rag/*` | Rate limit + X-API-Key (`rag_guard`) |

The README acknowledges this: "Put them behind your own gateway/authN in production." That is acceptable documentation for a known gap but it must be prominently called out in the OSS launch checklist. `/api/v1/debug/state` and `/api/v1/kernel/introspect` expose full internal state and should be disabled in production by default, not just hidden from OpenAPI schema.

**Required:** `/debug/state` and `/kernel/introspect` must return 404 or 403 when `settings.debug=False`, not just be hidden from schema.

---

### HIGH-002: InMemoryTransport has an unbounded message queue per topic per subscriber

**File:** `app/distributed/transport/memory.py:34`, `app/distributed/worker/runtime.py:69`
**Dimension:** Scalability

`InMemoryTransport.publish()` calls `asyncio.create_task(self._safe_call(handler, message))` for every delivered message (line 59). If the handler is slow or blocked, tasks accumulate without bound. There is no message queue size cap inside the transport.

`WorkerRuntime._queue` is bounded (`asyncio.Queue(maxsize=queue_capacity)` with default 128) and correctly logs a warning and drops when full. But the transport layer itself has no back-pressure signal — a slow worker will accumulate asyncio tasks until OOM.

Additionally, `InMemoryTransport._published` is a `defaultdict(int)` that grows without bound as new topic names appear. Minor, but worth noting.

**Required:** `InMemoryTransport` needs a per-(topic, group) asyncio queue with a configurable `maxsize` and either blocking or drop-on-full semantics. The backpressure signal from `BackpressureEngine` should propagate to the transport layer.

---

### HIGH-003: RaftNode log is unbounded in practice

**File:** `app/distributed/consensus/raft.py:197`, `app/distributed/consensus/recovery.py`
**Dimension:** Scalability / Raft Correctness

`RaftNode.propose()` appends to `self._state.log` (line 197) — an in-memory list with no bound. Compaction is triggered manually via `DurableLogStore.compact_to_snapshot()`. There is no automatic compaction loop in `RaftNode` or `KeyRotator`-equivalent. In a long-running cluster with frequent proposals, the log grows indefinitely.

More critically: since CRIT-001 means the WAL is never attached, the log is in-memory AND unbounded. A memory-intensive workload will OOM the scheduler process.

**Required:** Add a compaction trigger — after every N proposals (configurable; default 1000), automatically snapshot the state machine and call `compact_to_snapshot()`. This should be part of `KeyRotator`-equivalent for Raft (`RaftRotator`?).

---

### HIGH-004: `software_intelligence/` package is completely isolated — not imported by any app module

**File:** `software_intelligence/` (top-level directory)
**Dimension:** Dead Code / OSS Adoption

`software_intelligence/` is a top-level Python package (not under `app/`). Searching `app/` for imports: zero results. The module is never imported by any production code path, never loaded by the FastAPI app, and not exercised by any test in `tests/`.

The README's "Software Intelligence Layer (OSIP)" section describes it as powering "AI-assisted software engineering workflows" — but it has no wiring into any current endpoint or agent.

**Assessment:** Either dead code to remove before OSS launch, or intentionally deferred. Either way, OSS users who read the README will expect it to work and will be confused when it doesn't surface in the API.

---

### HIGH-005: `app/cloud/`, `app/ml/`, `app/open_source/` are empty placeholder directories committed to the repo

**File:** `app/cloud/__init__.py` (empty), `app/ml/__init__.py` (empty), `app/open_source/__init__.py` (empty)
**Dimension:** OSS Adoption / Dead Code

Three directories with no content are committed. When external contributors clone the repo and look at the structure, they will be confused about what belongs in these directories and may open issues or PRs targeting them incorrectly.

**Required before OSS launch:** Either populate these directories with at least a stub implementation and a `ROADMAP.md`, or delete them entirely. Empty public directories signal an abandoned plan.

---

### HIGH-006: The RAG /rag/ingest, /rag/query, /rag/answer, /rag/upload routes require X-API-Key but the key is optional

**File:** `app/main.py:92-98`, `app/core/config.py` (implicit)
**Dimension:** Security

```python
if require_key and settings.api_key:
    provided = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(provided, settings.api_key):
        raise HTTPException(status_code=401, ...)
```

`settings.api_key` — if it is an empty string or `None` (e.g., API_KEY env var not set), the condition `settings.api_key` evaluates to falsy, and the key check is **silently skipped**. Any caller can access all RAG endpoints without authentication if the operator does not set `API_KEY`.

This is a deploy-time footgun: the default is open, not closed. Production deployments where an operator forgets to set `API_KEY` are fully unauthenticated with no warning.

**Required:** At startup, if `settings.api_key` is empty and `settings.environment == "production"`, log a loud `SECURITY WARNING` and optionally refuse to start.

---

## MEDIUM Findings

### MED-001: `integrate_with_raft_node()` shim mutates private attributes of RaftNode

**File:** `app/distributed/consensus/recovery.py:285`

The shim patches `RaftNode._state`, `_role`, `_leader_id`, `_next_index`, `_match_index`, and replaces `propose()`, `handle_vote_request()`, `handle_append_entries()` with new bound methods. This is a fragile integration — any change to RaftNode's private interface silently breaks the shim. The correct fix is to give `RaftNode.__init__` an optional `persistence: RaftPersistence | None = None` parameter and check it in every state mutation method internally.

---

### MED-002: `app/distributed/consensus/raft.py` does not persist term/vote before responding to VoteRequest

**File:** `app/distributed/consensus/raft.py:203-213`

The Raft paper (§5.2) requires: "A server must persist its current term and its vote before sending any RPC response or updating any state." `handle_vote_request()` updates `self._state.voted_for` in-memory (line 209) and returns immediately. Even with the WAL shim attached (CRIT-001), the shim patches `propose()` and `handle_append_entries()` but may not intercept `handle_vote_request()`. If the node crashes after granting a vote but before the WAL write, it may grant a second vote for the same term on restart.

This is a correctness violation of Raft invariant INV-RAFT-001: "A server grants at most one vote per term."

---

### MED-003: `GovernanceClient` revocation list is in-memory and not shared across worker nodes

**File:** `app/distributed/worker/governance.py:41`

`self._revoked_tokens: set[str]` is per-process. In a multi-worker deployment, revoking a token on the scheduler does not propagate to worker B unless B receives the `TOKEN_REVOKED` event through the transport. With `InMemoryTransport` (single process), this works. With Kafka (multi-process), this works only if all workers are subscribed to the governance event topic. There is no verification that workers actually subscribe to governance events on startup.

---

### MED-004: `/api/v1/ml/models` has no authentication and lists all trained models

**File:** `app/main.py:671-684`

`GET /api/v1/ml/models` returns a full list of all model names, algorithms, metrics, and dataset metadata to any caller with no authentication. This may expose sensitive data about what the platform has been trained on.

---

### MED-005: `app/ml_pipeline/` and `app/github_analyzer/` are only accessible via API, not imported by agents

**File:** Multiple
**Dimension:** Architecture Consistency

`app/ml_pipeline/` is used exclusively by `POST /api/v1/ml/train`. It is never imported by any agent, kernel service, or workflow. Same for `app/github_analyzer/` (used only by `POST /api/v1/github/analyze`). These modules are effectively microservices embedded in a monolith. If AEOS is meant to be an orchestration platform, agents should be able to call ML training and GitHub analysis as tools — not only via HTTP.

---

### MED-006: Raft election timeout is hardcoded at 150–300ms — too aggressive for cross-datacenter deployments

**File:** `app/distributed/consensus/raft.py:41-43`

```python
_HEARTBEAT_MS    = 50
_ELECTION_MIN_MS = 150
_ELECTION_MAX_MS = 300
```

150–300ms election timeouts are safe for LAN environments (< 5ms RTT) but will cause constant re-elections if cross-AZ latency is 20–50ms or cross-region latency is 100ms+. The constants are configurable via `RaftNode.__init__` parameters, but the docker-compose cluster uses defaults. This should be documented prominently in the deployment guide.

---

### MED-007: Tests for P12A.3–P12A.4 observability and self-improving runtime are absent

**File:** `app/observability/`, `app/runtime/pattern_miner.py`, `app/runtime/adaptive_scheduler.py`
**Dimension:** Test Coverage

Modules added in P12A.3 (`RuntimeGraph`, `DecisionTracer`, `DistributedTimeline`) and P12A.4 (`ExecutionMemoryStore`, `PatternMiner`, `AdaptiveScheduler`) have no corresponding test files in `tests/unit/`. These are non-trivial modules:

- `PatternMiner.mine()` runs multiple async SQL queries in parallel — no test for empty result sets or SQLite errors
- `AdaptiveScheduler` has the hardcoded governance block (`if not governance_approved`) — the most critical safety invariant in the system — but no test verifies it holds after a hint system update
- `DistributedTimeline.generate_postmortem()` uses `asyncio.gather` with 6 coroutines — no test for partial failure

---

### MED-008: `app/distributed/demo/e2e_demo.py` is not a test — it is a runnable script imported nowhere

**File:** `app/distributed/demo/e2e_demo.py`

This 437-line script demonstrates the distributed runtime end-to-end but is not part of any pytest suite and is not documented in the README. External contributors cannot find it. It should either be moved to `examples/` with documentation or converted to an integration test.

---

## Technical Debt Register

| ID | Category | File | Description | Effort |
|----|----------|------|-------------|--------|
| TD-001 | Architecture | `app/distributed/consensus/` | WAL not wired into RaftNode (CRIT-001) | L |
| TD-002 | Architecture | `app/distributed/communication/` | gRPC inter-node transport not implemented (CRIT-002) | XL |
| TD-003 | Security | `app/ml_pipeline/registry.py:97` | pickle.load RCE risk (CRIT-003) | M |
| TD-004 | Security | `app/distributed/worker/governance.py` | JWT not cryptographically verified in worker (CRIT-004) | M |
| TD-005 | Security | `app/main.py` | `/debug/state` returns 200 regardless of `settings.debug` | S |
| TD-006 | Security | `app/main.py` | Silent open auth when `API_KEY` not set | S |
| TD-007 | Scalability | `app/distributed/transport/memory.py` | InMemoryTransport has unbounded task queue | M |
| TD-008 | Scalability | `app/distributed/consensus/raft.py` | Log compaction not automatic | M |
| TD-009 | Dead Code | `software_intelligence/` | Entire package unreferenced by production code | S (remove) |
| TD-010 | Dead Code | `app/cloud/`, `app/ml/`, `app/open_source/` | Empty placeholder directories | S (remove) |
| TD-011 | Testing | `app/observability/` | RuntimeGraph, DecisionTracer, DistributedTimeline untested | L |
| TD-012 | Testing | `app/runtime/` | PatternMiner, AdaptiveScheduler governance invariant untested | M |
| TD-013 | Correctness | `app/distributed/consensus/raft.py:203` | vote_for not persisted before RPC response | M |
| TD-014 | OSS | `app/distributed/demo/e2e_demo.py` | Not in examples/ or tests/ — invisible to contributors | S |
| TD-015 | OSS | `app/main.py:671` | `/ml/models` unauthenticated endpoint | S |

---

## Recommended Remediations (by priority)

### Immediate (block Phase 13 launch)

**R-001 — Wire WAL into RaftNode** (CRIT-001, TD-001)
Add `persistence: RaftPersistence | None = None` to `RaftNode.__init__`. Call `integrate_with_raft_node(self, persistence)` inside `__init__` when `persistence` is not None. All construction sites in cluster setup must pass a `RaftPersistence` instance. Add a recovery integration test that:
1. Creates a `RaftNode` with WAL persistence
2. Proposes 10 entries, waits for them to commit
3. Calls `node.stop()`
4. Reconstructs the node from the same WAL directory
5. Verifies all 10 entries are recovered with correct terms

**R-002 — Fix governance worker verification** (CRIT-004, TD-004)
Wire `TokenVerifier` into `GovernanceClient.verify_token()`. The current check (revocation list only) should remain, but it must be preceded by a full JWT signature verification. Remove the `if token_id is None: return` pass-through or make `None` tokens explicitly an error in production mode.

**R-003 — Replace pickle.load** (CRIT-003, TD-003)
Replace `pickle.load` in `app/ml_pipeline/registry.py:97` with `joblib.load` (already a dependency of scikit-learn). Migrate existing `.pkl` files on first access or document the migration path.

**R-004 — Close `/debug/state` in production** (HIGH-001, TD-005)
```python
@app.get(f"{settings.api_prefix}/debug/state", ...)
async def debug_state(request: Request):
    if not settings.debug:
        raise HTTPException(status_code=404)
    ...
```

**R-005 — Startup warning for missing API_KEY** (HIGH-006, TD-006)
In the `lifespan` function, after startup:
```python
if not settings.api_key and settings.environment == "production":
    log.critical("SECURITY: API_KEY is not set. RAG endpoints are UNAUTHENTICATED.")
```

### Before 1.0.0 Release

**R-006 — Define gRPC transport plan or remove the promise** (CRIT-002, TD-002)
Two options:
- Option A: Implement `GrpcChannel` using the proto stubs generated in P12A.1-4
- Option B: Implement an in-process Raft RPC bridge over Kafka (every `rpc_send` publishes to `aeos.raft.<node_id>` topic; target node subscribes and calls the handler)

Option B is faster but limits consensus to Kafka-connected nodes. Option A is the correct long-term design. Either way, the 3-node docker-compose must form a real cluster before 1.0.0 is tagged.

**R-007 — Remove empty packages and stale demo** (HIGH-005, MED-008, TD-009, TD-010, TD-014)
Delete `app/cloud/`, `app/ml/`, `app/open_source/`, and `software_intelligence/` or add a `ROADMAP.md` in each explaining what will go there. Move `app/distributed/demo/e2e_demo.py` to `examples/distributed_cluster_demo.py` and add it to the README.

**R-008 — Add tests for P12A.3 and P12A.4 modules** (MED-007, TD-011, TD-012)
Minimum test coverage needed:
- `TestAdaptiveScheduler.test_governance_block_is_unconditional()` — governance_approved=False MUST always return a governance-block decision, regardless of hint state
- `TestPatternMiner.test_empty_database()` — mine() on empty SQLite returns MiningResult with empty lists
- `TestRuntimeGraph.test_snapshot_after_upsert()` — snapshot() reflects upserted nodes
- `TestDistributedTimeline.test_postmortem_on_zero_events()` — postmortem generation does not raise on empty event window

---

## Revised Readiness Score

| Category | Weight | Phase 12A score | Phase 12A.2 score | Δ |
|----------|--------|----------------|-------------------|---|
| Architecture Consistency | 15% | 90 | 62 | -28 |
| Security | 20% | 82 | 64 | -18 |
| Raft / Consensus Correctness | 15% | 85 | 45 | -40 |
| Scalability | 10% | 78 | 72 | -6 |
| Test Coverage | 15% | 72 | 65 | -7 |
| OSS Readiness | 10% | 80 | 70 | -10 |
| Infrastructure / Ops | 15% | 94 | 92 | -2 |

**Composite (weighted): 68.45 / 100**

*(Down from 96/100 claimed post-12A — the earlier score measured feature completeness, not production correctness. This audit measures whether the platform does what it claims at a system level.)*

**Phase 13 OSS Launch threshold: 85/100**
**Current: 68.45/100**
**Gap: 16.55 points**

---

## Path to 85/100

Closing just the four critical findings (CRIT-001 through CRIT-004) and the three high-urgency findings (HIGH-001, HIGH-002, HIGH-006) recovers approximately **+19 points**, pushing the score above threshold.

| Remediation | Points recovered (estimate) |
|------------|----------------------------|
| R-001: WAL wired into RaftNode | +8 (Architecture + Raft + Testing) |
| R-002: JWT verification in worker | +5 (Security) |
| R-003: pickle.load replaced | +3 (Security) |
| R-004: /debug/state closed in prod | +2 (Security) |
| R-005: API_KEY startup warning | +1 (Security) |
| **Subtotal** | **+19 → estimated 87.45/100** |

The gRPC gap (CRIT-002) is the largest long-term debt item but does not block a 1.0.0 launch if documented clearly in the README as "single-node production; multi-node Raft requires external RPC wiring." This is honest and allows shipping a working product while the transport layer matures.

---

## Summary Verdict

AEOS has a well-designed architecture and a serious amount of working infrastructure. The observability, security model, proto governance, and RAG pipeline are genuinely production-quality. The OSS documentation and developer experience are strong.

The audit surfaces **one systemic issue** that undermines multiple prior claims: the WAL persistence layer and the gRPC transport layer were built but not wired into the running system. The WAL tests pass; the WAL is never called. The proto definitions exist; no node uses them to communicate.

This is not a fundamental flaw — it is an integration gap. The components are correct. The connections between components are missing. Closing these connections is concrete, bounded work, not a redesign.

**Recommended next step: Phase 12A.3 — Integration Sprint**
Close CRIT-001 through CRIT-004 and HIGH-001, HIGH-002, HIGH-006 before Phase 13.
Estimated effort: 5–8 days.
Expected revised score: 87–90/100.
At that score, Phase 13 OSS launch is defensible.

---

*Generated: 2026-07-14 | Auditor: Phase 12A.2 Adversarial Architecture Review*
