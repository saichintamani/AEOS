# AEOS Phase 12A — System Gap Analysis

**Auditor role**: Principal Distributed Systems Architect + Verification Lead
**Date**: 2026-07-12
**Scope**: Phases 1–11 implementation vs. contracts 015–025
**Method**: Every finding references a specific contract clause, invariant ID, protocol ID, state-machine ID, or ADR.

---

## Executive Summary

AEOS has reached Phase 11 with a production-grade deployment stack, distributed consensus engine, validation framework, and observability layer. Against the 10 verification contracts (015–025), the implementation is **strong on structure and weak on runtime completeness**. The primary gap category is **partial coverage** — the right abstractions exist but don't reach 100% of the cases the contracts specify.

| Category | Score Before P12A | Primary Gap |
|----------|--------------------|-------------|
| Correctness | 68/100 | Invariant coverage: 15/26 INV-* IDs (58%); no replay validator |
| Reliability | 72/100 | Chaos framework exists in tests but not as standalone platform |
| Security | 65/100 | PROTO-002/003/004/005 not validated; Vault not wired |
| Scalability | 55/100 | No formal certification suite; benchmark runner is local-only |
| Observability | 70/100 | No runtime topology graph; no distributed timeline replay |
| Extensibility | 80/100 | Plugin boundary well-defined; versioning risk in gRPC schemas |
| Maintainability | 78/100 | Good modular structure; no mutation testing |
| Operability | 82/100 | Full runbook suite; no automated postmortem generator |
| Intelligence | 60/100 | Learning pipeline not persistent across restarts |
| Community Readiness | 55/100 | Good CONTRIBUTING.md; no formal API stability guarantees |

**Overall: 68.5/100 — below the 95/100 threshold required for Phase 13.**

---

## Section 1 — Architectural Drift (vs. 015-ARCHITECTURE_CONTRACT.md)

### DRIFT-001 — Redis Cluster vs. Redis Sentinel
**Contract**: AC-COMP-003 — "Redis Sentinel MUST NOT be used; Redis Cluster mode only."
**Implementation**: `redis-statefulset.yaml` deploys 3-node primary-replica topology using Redis Sentinel semantics (ordinal-based primary detection). This is NOT Redis Cluster mode (no hash slot sharding).
**`ElastiCache` module**: `num_cache_clusters` parameter implies Replication Group (Sentinel-compatible), not Redis Cluster.
**Risk**: Horizontal scalability cap at 256GB per shard. `INV-MEM-001` (redis key hashtag co-location) is only relevant to cluster mode — currently untestable.
**Remediation**: Migrate Terraform ElastiCache module to Cluster mode (`cluster_mode` block); update redis-statefulset to Redis Cluster with `cluster-enabled yes`.

### DRIFT-002 — ZooKeeper Prohibition Not Formally Verified
**Contract**: AC-COMP-004 — "ZooKeeper MUST NOT be used for AEOS coordination."
**Implementation**: Kafka StatefulSet uses KRaft (correct). No ZooKeeper deployed. However, no automated check verifies this at admission time.
**Remediation**: Add OPA ConstraintTemplate `NoZookeeper` that rejects Pods with image matching `zookeeper`.

### DRIFT-003 — Worker-to-Worker Direct Connection Not Enforced by Network Policy
**Contract**: AC-DEP-001 — "Workers MUST NOT make direct connections to other workers."
**Implementation**: `network-policy.yaml` has default-deny in `aeos-jobs` namespace but does NOT have an explicit "allow worker→kafka only" egress policy that would block worker→worker cross-namespace calls. Workers could theoretically open connections to `aeos-api` ClusterIP.
**Remediation**: Add explicit `deny-worker-to-worker` NetworkPolicy; add AuthorizationPolicy `deny-worker-to-worker` in Istio.

### DRIFT-004 — Policy Service Not Decoupled from Hot Path
**Contract**: AC-DEP-003 — "Policy Service MUST NOT be a synchronous dependency in the step execution hot path."
**Implementation**: `InvariantEngine` runs as a background monitor (30s interval) — correct. However, governance token check (`INV-EXEC-003`: "Governance Token Required") is a synchronous check in the execution path via `InvariantEngine.check_all()`. If the check blocks (e.g., Redis unavailable), execution blocks.
**Risk**: Governance failure cascades to full execution halt — violates fail-closed semantics gracefully.
**Remediation**: Wrap governance checks in `asyncio.wait_for(check, timeout=0.1)` with fail-closed default on timeout.

### DRIFT-005 — LLM Direct Import Not Prevented
**Contract**: AC-DEP-004 — "Workers MUST NOT import LLM client libraries directly; all LLM access via CapabilityNode/AgentNode."
**Implementation**: No import guard exists. `app/agents/` files may import `openai` directly. No linter rule or admission check enforces this.
**Remediation**: Add `ruff` rule `banned-import` for `openai`, `anthropic`, `cohere` in worker module paths; enforce in CI.

### DRIFT-006 — Missing AC-IFACE-001 on Scheduler
**Contract**: AC-IFACE-001 — "All services MUST expose GET /healthz, GET /readyz, GET /metrics."
**Implementation**: Scheduler deployment exposes `/health` and `/health/ready` — note: contract specifies `/healthz` and `/readyz` (with 'z' suffix). Mismatched probe path.
**Remediation**: Align all health paths to `/healthz` / `/readyz` or update contract to match implementation (ADR required).

---

## Section 2 — Invariant Coverage Gaps (vs. 019-INVARIANTS.md)

Current InvariantEngine covers **15 of the invariants**. Based on the 019 spec and AC-INV-001 ("All INV-* invariants MUST be implemented and continuously monitored"), the following are **not yet implemented**:

| Missing ID | Title | Severity | Category |
|------------|-------|----------|----------|
| `INV-CONS-003` | Term Monotonicity Across Restarts | CRITICAL | consensus |
| `INV-EXEC-006` | DLQ Invariant — No Silent Drop | CRITICAL | execution |
| `INV-EXEC-007` | Idempotency Key Uniqueness Window | CRITICAL | execution |
| `INV-EXEC-008` | At-Least-Once Delivery Guarantee | CRITICAL | execution |
| `INV-RAFT-005` | Snapshot Index ≤ Commit Index | CRITICAL | consensus |
| `INV-RAFT-006` | Snapshot Covers All Applied Entries | CRITICAL | consensus |
| `INV-CHKPT-003` | Checkpoint Version Monotonicity | ERROR | checkpoint |
| `INV-CHKPT-004` | Cross-Partition Checkpoint Consistency | CRITICAL | checkpoint |
| `INV-MEM-002` | Memory Eviction Policy Consistency | ERROR | memory |
| `INV-GOV-001` | Governance Token Not Reused | CRITICAL | governance |
| `INV-GOV-002` | Policy Evaluation Before Execution | CRITICAL | governance |

**Gap**: 11 invariants unimplemented. AC-INV-001 compliance: 58%.

---

## Section 3 — Protocol Coverage Gaps (vs. 016-PROTOCOL_SPECIFICATION.md)

ProtocolValidator validates PROTO-001, 006, 008, 009, 019. **Not validated**:

| Missing Protocol | Description | Risk |
|-----------------|-------------|------|
| `PROTO-002` | Node Leave (graceful) | Worker drain not verifiable |
| `PROTO-003` | Node Failure Detection | Suspicion protocol not traced |
| `PROTO-004` | Capability Registration | Capability lifecycle unverified |
| `PROTO-005` | Capability Deregistration | Stale capability detection gap |
| `PROTO-007` | Task Failure & DLQ | DLQ routing unchecked |
| `PROTO-010` | Governance Token Issuance | Token provenance untracked |
| `PROTO-011` | Policy Evaluation Request | Policy decision log absent |
| `PROTO-015` | Snapshot Creation | Raft snapshot protocol unverified |
| `PROTO-016` | Snapshot Installation | Log truncation safety unverified |

**Gap**: 9 of 19 protocols unvalidated.

---

## Section 4 — State Machine Coverage Gaps (vs. 017-STATE_MACHINE_SPECIFICATION.md)

StateMachineValidator defines 8 machines. **Missing**:

| Missing Machine | Missing Transitions |
|----------------|---------------------|
| `SM-GOVERNANCE` | Full machine exists in code but not wired to live governance token events |
| `SM-CAPABILITY` | UNREGISTERED→ACTIVE transition not triggered by actual capability registration events |

Both machines are **defined** but not **connected to live event streams** — transitions are only validated on explicit API calls, not on the actual execution path.

---

## Section 5 — Correctness Risks (vs. 024-DISTRIBUTED_CORRECTNESS_SPEC.md)

### CORRECT-001 — No Replay Validator
**Spec ref**: DCS §4 — "Execution traces MUST be replayable to verify correctness."
**Gap**: No replay framework exists. Traces are logged but not replayable.

### CORRECT-002 — Exactly-Once Illusion Unverified End-to-End
**Spec ref**: DCS §6 — "Exactly-once execution illusion MUST be verifiable from Kafka offset + checkpoint + Redis idempotency key."
**Gap**: The three components (Kafka, checkpoint, Redis) are individually tracked but no cross-system validator joins them into an exactly-once proof.

### CORRECT-003 — No Cluster Consistency Validator
**Spec ref**: DCS §5 — "Cluster membership MUST be consistent across Raft log, Redis membership cache, and gRPC channel registry within 5 seconds (INV-CONS-004)."
**Gap**: INV-CONS-004 checks staleness threshold but there is no validator that queries all three views simultaneously and compares them.

### CORRECT-004 — Log Matching Property Not Tested
**Spec ref**: DCS §3.2 — "If two logs contain an entry with the same index and term, all earlier entries are identical."
**Gap**: `RaftNode` implements this internally but no external test verifies log matching property across a multi-node cluster with injected failures.

---

## Section 6 — Scalability Bottlenecks (vs. 022-PERFORMANCE_BENCHMARK_SPEC.md)

### SCALE-001 — InvariantEngine Runs on Every Check Call
The `InvariantEngine.check_all()` runs all 15 invariants serially. At high throughput (1000+ tasks/min), the 30s background loop plus per-request trace checks create contention on shared state.
**Contract**: PBS §5 — "Invariant checks MUST NOT add > 10ms to task dispatch latency."
**Measured**: Not yet benchmarked.

### SCALE-002 — No Kafka Partition Auto-Scaling
Kafka is configured with `num.partitions=3` (static). At Gold certification (50 workers, 1M tasks), consumer parallelism is capped at 3.
**Remediation**: Configure `num.partitions=12` minimum; implement KEDA `KafkaTopic` trigger for worker HPA.

### SCALE-003 — Raft Throughput Cap
`RaftNode._replicate()` is serial per proposal. Single leader processes proposals sequentially via `asyncio` event loop. This is correct but throughput-limited.
**Contract**: PBS §3 — "Raft log throughput MUST sustain 10k proposals/second."
**Gap**: Not benchmarked; no pipelining or batching implemented.

---

## Section 7 — Security Weaknesses (vs. 023-SECURITY_VALIDATION_SPEC.md)

### SEC-001 — Vault Not Wired
**Contract**: SVS §2 — "All secrets MUST be served from Vault with dynamic lease rotation."
**Implementation**: ESO pulls from AWS Secrets Manager. Vault is listed in AC-COMP-001 but not deployed. Kubernetes manifest for Vault StatefulSet does not exist.
**Risk**: Static secrets in Secrets Manager; no dynamic credential rotation.

### SEC-002 — mTLS Not Enforced Between Internal Components in Dev Mode
**Implementation**: Istio PeerAuthentication is STRICT in production. But `docker-compose.cluster.yml` and local mode have no TLS at all.
**Contract**: SVS §5 — "All internal communication MUST use mTLS."
**Remediation**: Add mTLS termination option for docker-compose mode using self-signed certs or `step-ca`.

### SEC-003 — JWT Algorithm Not Pinned
**Contract**: SVS §8 — "JWT tokens MUST use RS256 or ES256; HS256 is forbidden."
**Gap**: JWT secret key is symmetric (HMAC) based on the ESO secret name `jwt_secret_key`. No asymmetric key pair configured.

### SEC-004 — No Audit Log for Governance Decisions
**Contract**: SVS §9 — "Every governance decision MUST produce an immutable audit log entry."
**Gap**: InvariantEngine logs violations to Python logger. No immutable append-only audit store.

---

## Section 8 — Recovery Risks (vs. 020-FAILURE_INJECTION_PLAN.md)

### RECOV-001 — No Automated Fault Injection
**Contract**: FIP §1 — "Failure injection MUST be executable in CI against staging."
**Gap**: `chaos/` module exists in test files but is not a standalone platform and not in CI.

### RECOV-002 — Split-Brain Recovery Unverified
**Contract**: FIP §7 — "Split brain scenario MUST be injected and Raft must converge correctly."
**Gap**: No test verifies that two concurrent leaders (due to network partition) converge when the partition heals.

### RECOV-003 — Checkpoint Corruption Recovery Unverified
**Contract**: FIP §10 — "Corrupt checkpoint MUST trigger orphan recovery (PROTO-009), not silent failure."
**Gap**: No fault injector corrupts checkpoints; PROTO-009 path is unit-tested but not chaos-tested.

---

## Section 9 — Technical Debt

| Debt Item | File | Severity |
|-----------|------|----------|
| `redis_coordinator.py` expected by tests but split into two files | `app/distributed/coordination/` | Medium |
| `kafka_transport.py` not at documented path | `app/distributed/transport/kafka.py` | Low |
| `app/verification/` directory does not exist | Phase 12A creates it | High |
| `reports/` directory does not exist | Phase 12A creates it | High |
| Health endpoint path mismatch (`/health` vs `/healthz`) | `app/main.py` | Medium |
| `INV-CONS-003` term-monotonicity across restarts requires persistent state | `raft.py` | High |
| No mutation testing in CI | CI pipeline | Medium |
| Governance token is checked synchronously in execution hot path | `invariants.py` | High |

---

## Remediation Roadmap (to reach 95/100)

| Priority | Work Item | Workstream | Impact |
|----------|-----------|------------|--------|
| P0 | Implement 11 missing invariants | P12A.1 | +18 correctness |
| P0 | Implement replay validator | P12A.1 | +8 correctness |
| P0 | Wire SM-GOVERNANCE + SM-CAPABILITY to live events | P12A.1 | +5 correctness |
| P1 | Implement 9 missing protocol validators | P12A.1 | +12 correctness |
| P1 | Cluster consistency validator (3-view join) | P12A.1 | +6 correctness |
| P1 | Chaos platform with 12 fault types | P12A.2 | +15 reliability |
| P1 | Fix governance hot-path timeout | Core | +5 reliability |
| P2 | Runtime topology graph + decision tracer | P12A.3 | +10 observability |
| P2 | Kafka partition count → 12; KEDA trigger | Infrastructure | +8 scalability |
| P2 | Scale certification Bronze→Platinum | P12A.5 | +10 scalability |
| P3 | Execution memory + pattern miner | P12A.4 | +8 intelligence |
| P3 | Vault deployment + dynamic secrets | Security | +10 security |
| P3 | JWT RS256 migration | Security | +5 security |
| P3 | Longevity review + readiness report | P12A.6 | +5 maintainability |
