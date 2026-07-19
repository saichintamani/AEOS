# 026 — AEOS 5-Year Architectural Longevity Review

**Date:** 2026-07-13  
**Authors:** Platform Architecture Team  
**Status:** APPROVED  
**Horizon:** 2026–2031

---

## 1. Purpose

This document assesses whether AEOS's current architecture can survive 5 years of production operation without a disruptive rewrite. It evaluates interfaces, contracts, extension points, plugin boundaries, and persistence layers for long-term viability.

The review follows the checklist from Google's Site Reliability Engineering "Evolution" chapter and Kleppmann's _Designing Data-Intensive Applications_ Chapter 4 ("Encoding and Evolution").

---

## 2. Interface Stability Assessment

### 2.1 External API (`/api/v1/`)

| Endpoint | Stability | Risk | Mitigation |
|----------|-----------|------|------------|
| `POST /workflows` | **STABLE** | Low | Versioned URL; additive-only changes |
| `GET /tasks/{id}` | **STABLE** | Low | Response schema pinned by contract tests |
| `POST /governance/approve` | **CAUTIOUS** | Medium | Token model may evolve; use field-level versioning |
| `GET /metrics` | **STABLE** | Low | Prometheus text format is backward-compatible |
| `WebSocket /graph` | **VOLATILE** | High | Binary protocol not yet defined; may need versioning |

**5-Year Verdict:** Stable if WebSocket graph feed adopts a versioned envelope (`{"v":1,"payload":...}`) before 2027.

### 2.2 Kafka Topic Schema

All Kafka messages use JSON. Current risk: no schema registry.

**Recommendation:** Adopt Apache Avro + Schema Registry (Confluent or Redpanda) by 2027 Q1.  
**Breaking risk without this:** High. A field rename in `aeos.tasks` messages would require coordinated consumer rollout across all services simultaneously.

### 2.3 gRPC Internal API

Proto files are in `app/distributed/grpc/`. Proto3 forward-compatibility rules are followed. Field additions are safe; field deletions/renames are breaking.

**Process gap:** No CI check enforcing backward-compatible proto changes.  
**Fix:** Add `buf breaking --against` check in CI by 2026 Q3.

---

## 3. Contract Stability Assessment

### 3.1 Invariant Contracts (INV-*)

26 invariants defined in `docs/contracts/016-INVARIANT_CONTRACTS.md`. Current status:

- **Stable (never break):** INV-EXEC-001/002, INV-RAFT-001/002, INV-CONS-001/002 — these are safety properties; any violation is a bug, not a version change.
- **May evolve:** INV-CONS-004 (5s staleness) — threshold may tighten as hardware improves.
- **Likely to be added:** New INV-GOV-* invariants as governance model matures.

**5-Year Verdict:** The invariant framework is extensible (additive) and the numbering scheme (`INV-{CAT}-{NUM}`) supports up to 999 invariants per category. No structural risk.

### 3.2 Protocol Contracts (PROTO-*)

19 protocols in `docs/contracts/017-PROTOCOL_CONTRACTS.md`. Most protocols are internal. Risk comes from:

- PROTO-010 (Governance Token Issuance) — if governance model changes to multi-party approval, the protocol must version
- PROTO-015/016 (Raft Snapshot) — coupled to Raft implementation; any log compaction changes must be backward-compatible

**5-Year Verdict:** Acceptable. Protocol evolution should use versioned message types.

### 3.3 State Machine Contracts (SM-*)

8 state machines. State machines are the most stable contracts — changing a state adds a migration, but removing a state or transition is breaking.

**Risk:** SM-TASK currently has no "PAUSED" state, which several roadmap features require.  
**Mitigation:** Add PAUSED→RUNNING as a valid additive extension when needed; do not remove SUSPENDED.

---

## 4. Extension Points Assessment

### 4.1 Plugin System (`app/runtime/plugin_manager.py`)

- Plugins load via Python entry points (`aeos.plugins`)
- Manifest schema versioned (`manifest_version: "1.0"`)
- **Gap:** No plugin API stability promise. Breaking the `PluginManifest` schema breaks all third-party plugins.
- **Fix:** Publish `PLUGIN_API_STABILITY.md` with semver guarantee before first external release.

### 4.2 Fault Injection (`app/testing/chaos/faults.py`)

`BaseFault` ABC is stable: 3 methods (`inject`, `observe`, `recover`). New faults add classes, not modify the ABC.

**5-Year Verdict:** Stable. ABC contract will not need changes.

### 4.3 Workflow DSL

Defined in `app/sdk/`. DSL is YAML-based. Risk: adding required YAML fields breaks existing workflows.

**Mitigation (already in place):** All new DSL fields must have default values. `workflow_version` field allows per-workflow schema pinning.

### 4.4 Invariant Validators

New invariants added by extending `InvariantEngine` and `InvariantValidator`. Additive — no breaking changes to existing validators.

---

## 5. Persistence Layer Assessment

### 5.1 SQLite (Execution Memory)

`ExecutionMemoryStore` uses SQLite with WAL mode. SQLite is extremely stable (backward-compatible since 2004).

- **Migration:** Uses `CREATE TABLE IF NOT EXISTS` — safe for additive schema changes.
- **Risk:** No schema version table. Column additions to `executions` require ALTER TABLE.
- **Fix:** Add `schema_version` table in next iteration.

### 5.2 Redis

Redis data structures in use: SET (membership), HASH (task state), SORTED SET (priority queues), String (leases).

Redis 7.x keys are backward-compatible with Redis 6.x. No risk for 5 years unless Redis deprecates a data type (historically never done).

**Current architecture risk (DRIFT-001):** Sentinel vs. Cluster mode. Must migrate to Cluster before scale certification above Silver tier.

### 5.3 Kafka

KRaft (ZooKeeper-free) is the supported path for Kafka 3.x+. Current implementation correctly uses KRaft.

- **Retention policy:** Not yet configured. 7-day default may be insufficient for replay validation of long-running workflows.
- **Fix:** Add topic-level retention configs per event category: `aeos.tasks` → 30 days, `aeos.checkpoints` → 90 days.

### 5.4 Raft Log

In-memory with optional snapshot. For production, must persist to disk.

- **Risk (HIGH):** Current Raft log is in-memory. A scheduler restart loses all uncommitted entries.
- **Fix:** Wire `RaftNode` to a WAL file (etcd-style) before production. Estimated effort: 2 weeks.

---

## 6. Technology Stack Survivability

| Component | Current | 5-Year Risk | Note |
|-----------|---------|-------------|------|
| Python 3.11+ | asyncio, dataclasses | **LOW** | Python 3.12+ fully compatible |
| FastAPI | 0.100+ | **LOW** | Widely adopted, active development |
| Kafka KRaft | 3.6+ | **LOW** | ZooKeeper EOL pushes adoption |
| Redis 7 | Cluster | **LOW** | Very stable protocol |
| SQLite | 3.40+ | **VERY LOW** | Decade-stable |
| Helm 3 | 3.14+ | **LOW** | No Helm 4 breaking changes expected until 2028 |
| Istio | 1.20+ | **MEDIUM** | API may change; pin version in Helm |
| Terraform | 1.7+ | **LOW** | OpenTofu fork provides continuity |
| OPA/Gatekeeper | 3.14+ | **LOW** | Policy language is stable |

---

## 7. Identified Long-Term Risks

### LONGEVITY-001: In-memory Raft log (CRITICAL)
**Impact:** Scheduler crash loses all uncommitted Raft entries.  
**Mitigation:** WAL file persistence before 2026 Q4.

### LONGEVITY-002: No Kafka schema registry (HIGH)
**Impact:** Schema drift across services; impossible coordinated rollout.  
**Mitigation:** Avro + Schema Registry by 2027 Q1.

### LONGEVITY-003: Redis Sentinel → Cluster migration (HIGH)
**Impact:** DRIFT-001; AC-COMP-003 violated; Silver scale certification will fail.  
**Mitigation:** Migrate ElastiCache to Cluster mode by 2026 Q4.

### LONGEVITY-004: No plugin API stability contract (MEDIUM)
**Impact:** Third-party plugin breakage on any AEOS minor release.  
**Mitigation:** Publish stability policy before first external SDK release.

### LONGEVITY-005: JWT HMAC → RS256/ES256 migration (MEDIUM)
**Impact:** SVS §8 violation; cannot support token verification by external parties.  
**Mitigation:** JWT RS256 migration by 2026 Q3.

### LONGEVITY-006: WebSocket graph feed protocol unversioned (MEDIUM)
**Impact:** Dashboard clients break on any schema change.  
**Mitigation:** Add `{"v": 1, ...}` envelope by first dashboard release.

### LONGEVITY-007: Governance hot-path synchronous timeout (MEDIUM)
**Impact:** AC-DEP-003; governance call blocks execution hot path.  
**Mitigation:** `asyncio.wait_for` with fail-closed default by 2026 Q3.

---

## 8. 5-Year Survivability Verdict

| Dimension | Score | Status |
|-----------|-------|--------|
| API Stability | 8/10 | WebSocket needs versioning |
| Contract Stability | 9/10 | Invariant/protocol frameworks are extensible |
| Extension Points | 7/10 | Plugin API needs stability guarantee |
| Persistence | 6/10 | Raft WAL and Redis Cluster migration critical |
| Technology Stack | 9/10 | Conservative, long-lived choices |
| Security Posture | 6/10 | JWT, Vault, audit log gaps |
| Operational Maturity | 8/10 | DR runbooks, SLOs, dashboards present |

**Overall: 7.6/10 — Conditionally Survivable**

AEOS will survive 5 years if the 7 longevity risks above are addressed by their target dates. Without LONGEVITY-001 (Raft WAL) and LONGEVITY-003 (Redis Cluster), the platform cannot achieve Silver-tier scale certification, capping growth at 25 workers.

---

## 9. Recommended 5-Year Roadmap

| Year | Priority |
|------|----------|
| 2026 Q3 | JWT RS256, governance hot-path fix, buf CI check |
| 2026 Q4 | Raft WAL persistence, Redis Cluster migration |
| 2027 Q1 | Kafka schema registry (Avro), retention policy |
| 2027 Q2 | Plugin API stability contract, schema version table |
| 2028 | Review Istio version, evaluate eBPF-native service mesh |
| 2029–2031 | Evaluate Kafka → Pulsar if multi-tenancy needed; review Python GIL removal impact |
