# AEOS Phase 9 DRP — Architecture Contract

**Document:** `015-ARCHITECTURE_CONTRACT.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Authority:** AEOS Architecture Verification Team  
**Date:** 2026-07-06  
**Canonical source:** `012-PHASE_9_DRP_SPECIFICATION_v1_1.md`

---

## Purpose

This document is the machine-checkable contract between the Phase 9 RFC and every implementation. Every clause in this contract is either:
- **MUST** — a mandatory invariant; violation is an implementation defect
- **MUST NOT** — an explicit prohibition; violation is an implementation defect
- **SHOULD** — a strong recommendation; deviation requires documented justification

Automated conformance tests reference clauses by their ID (e.g., `AC-COMP-001`).

---

## Table of Contents

1. [Component Catalogue](#1-component-catalogue)
2. [Forbidden Dependencies](#2-forbidden-dependencies)
3. [Required Interfaces](#3-required-interfaces)
4. [Kernel Contract](#4-kernel-contract)
5. [Lifecycle Contract](#5-lifecycle-contract)
6. [Execution Contract](#6-execution-contract)
7. [Memory Contract](#7-memory-contract)
8. [Scheduling Contract](#8-scheduling-contract)
9. [Networking Contract](#9-networking-contract)
10. [Governance Contract](#10-governance-contract)
11. [Security Contract](#11-security-contract)
12. [Plugin Contract](#12-plugin-contract)
13. [Extension Points](#13-extension-points)
14. [Cloud Contract](#14-cloud-contract)
15. [Observability Contract](#15-observability-contract)

---

## 1. Component Catalogue

### AC-COMP-001
**MUST** — The following and only the following top-level services are permitted in a Phase 9 deployment:

| Service | Role | Replicas |
|---------|------|---------|
| `aeos-worker` | Task execution, local scheduling | 3–100 |
| `aeos-cluster-manager` | Raft consensus, membership, routing | 3 (fixed) |
| `aeos-policy-service` | Governance token issuance and evaluation | 2+ |
| `aeos-capability-registry` | Capability advertisement and discovery | 2+ |
| `aeos-api-gateway` | External API entry point, authentication | 2+ |
| `kafka` (MSK/self-hosted) | Task and event fabric | per cloud config |
| `redis-cluster` | Hot state, leases, WTM, idem keys | 6 nodes (3p+3r) min |
| `postgres` | LTM structured store, governance audit log | 1 primary + 1 replica min |
| `weaviate` | Episodic and LTM vector store | 2+ (replication factor ≥ 2) |
| `vault` | PKI, dynamic secrets, TLS leaf certs | 3 (HA mode) |
| `prometheus` | Metrics collection | 1+ |
| `grafana` | Metrics visualization | 1 |
| `keda` | Kafka-based autoscaling | operator (1) |

### AC-COMP-002
**MUST NOT** — No service not listed in AC-COMP-001 may be added to the runtime dependency graph without a new ADR and Architecture Review Board approval.

### AC-COMP-003
**MUST NOT** — Redis Sentinel MUST NOT be used. Only Redis Cluster (cluster mode enabled) is permitted. Any configuration file containing `sentinel` is a contract violation.

### AC-COMP-004
**MUST NOT** — ZooKeeper MUST NOT be used for any AEOS coordination. Kafka must be configured in KRaft mode or with an isolated ZooKeeper that AEOS services do not directly connect to.

---

## 2. Forbidden Dependencies

### AC-DEP-001
**MUST NOT** — Worker pods MUST NOT make direct network connections to other worker pods. All worker-to-worker coordination MUST flow through Kafka (task/event topics) or the Cluster Manager (gRPC).

### AC-DEP-002
**MUST NOT** — The Cluster Manager MUST NOT depend on Redis for authoritative membership state. Redis is a read-through cache only; the authoritative source is the Raft log projection.

### AC-DEP-003
**MUST NOT** — The Policy Service MUST NOT be a synchronous dependency in the step execution hot path. Policy evaluation occurs at task submission; workers validate pre-issued tokens locally.

### AC-DEP-004
**MUST NOT** — Workers MUST NOT import or instantiate any LLM client library directly. All LLM access MUST go through the `CapabilityNode` or `AgentNode` abstraction.

### AC-DEP-005
**MUST NOT** — No service MUST have a circular dependency. The dependency graph MUST be a DAG. Cycles detected in the import graph or service call graph are contract violations.

### AC-DEP-006
**MUST NOT** — Database schemas MUST NOT be modified by application code at runtime (no `CREATE TABLE`, `ALTER TABLE` in application hot path). All schema changes MUST go through Alembic migrations.

---

## 3. Required Interfaces

Every service that exposes an interface MUST implement the following:

### AC-IFACE-001 — Health Check Interface
All services MUST expose:
```
GET /healthz          → 200 OK {"status": "healthy"} or 503
GET /readyz           → 200 OK {"status": "ready"} or 503
GET /metrics          → Prometheus exposition format (text/plain)
```

### AC-IFACE-002 — gRPC Services
All gRPC services MUST:
- Use TLS (mTLS for internal services)
- Implement a `GetServiceInfo` method returning service name, version, and capabilities
- Return gRPC status codes (not HTTP status codes) for all error conditions
- Implement deadline propagation (respect incoming deadlines, propagate to downstream calls)

### AC-IFACE-003 — Kafka Message Schema
All Kafka messages MUST:
- Be serialized as Protocol Buffers (not JSON, not Avro unless schema-registry configured)
- Include a `message_id` (UUID v4) field
- Include a `produced_at` (Unix nanoseconds) field
- Include a `schema_version` (int32) field

### AC-IFACE-004 — Redis Key Schema
All Redis keys MUST use the hashtag schema defined in Appendix C of `012-PHASE_9_DRP_SPECIFICATION_v1_1.md`. No bare keys without hashtag routing are permitted for per-workflow state.

---

## 4. Kernel Contract

### AC-KERN-001 — Boot Phase Ordering
The HyperKernel MUST boot through phases in strict order. No phase may be skipped. Phases MUST NOT run concurrently.

```
INITIALIZING(1) → LOADING(2) → CONFIGURING(3) → STARTING(4) → JOINING(5) → RUNNING(6)
```

At any point, STOPPING(7) may be triggered from RUNNING(6) only.

### AC-KERN-002 — Phase Transition Atomicity
Phase transitions MUST be atomic from the perspective of external observers. A service MUST NOT advertise itself as RUNNING until JOINING completes (cluster registration is durable).

### AC-KERN-003 — Service Registration Gate
No capability MUST be advertised to the Capability Registry before the kernel reaches JOINING phase 5. Advertising capabilities during STARTING(4) is a contract violation.

### AC-KERN-004 — Graceful Shutdown
On SIGTERM, the kernel MUST:
1. Stop accepting new tasks (within 2 seconds)
2. Complete all in-flight steps or checkpoint them (within 120 seconds)
3. Deregister capabilities from the registry
4. Deregister from cluster membership
5. Commit all pending Kafka offsets
6. Flush all pending metrics

SIGKILL may arrive after the OS-configured grace period (default: 30 seconds in Kubernetes). Code MUST NOT assume more than 30 seconds for graceful shutdown.

### AC-KERN-005 — Plugin Initialization Gate
A plugin's `initialize()` method MUST be called only after the kernel reaches RUNNING phase. Plugins registered before RUNNING receive a deferred initialization callback.

---

## 5. Lifecycle Contract

### AC-LIFE-001 — Worker Lifecycle States
Every worker MUST implement the following state machine (see `017-STATE_MACHINE_SPECIFICATION.md` §SM-WORKER for full transitions):

```
INITIALIZING → JOINING → RUNNING → DRAINING → STOPPED
                              ↓
                           SUSPENDED (during governance circuit-open)
```

### AC-LIFE-002 — Cluster Member States
The Cluster Manager MUST track each member in one of: `JOINING`, `RUNNING`, `SUSPECTED`, `DRAINING`, `LEFT`, `FAILED`. No other states are permitted. State transitions MUST be logged to the Raft log.

### AC-LIFE-003 — Task Lifecycle States
Every task MUST pass through states in the following permitted order:

```
SUBMITTED → QUEUED → ACCEPTED → EXECUTING → COMPLETED
                                          → FAILED
                                          → CANCELLED
                                          → TIMEOUT
```

A task in `COMPLETED` or `FAILED` or `CANCELLED` is terminal. No transition from a terminal state is permitted.

### AC-LIFE-004 — Idempotency of State Transitions
Applying the same state transition twice MUST produce the same result as applying it once. Workers MUST check the current state before applying a transition and discard duplicate transitions.

---

## 6. Execution Contract

### AC-EXEC-001 — Execution Lease Acquisition
Before executing any step, a worker MUST acquire the execution lease:
```
SETNX {wf:<workflow_id>}:step:<step_id>:lease <worker_node_id> EX 120
```
A worker that fails to acquire the lease (return value 0) MUST NOT execute the step.

### AC-EXEC-002 — Two-Phase Checkpoint
Step completion MUST follow the two-phase protocol:
1. Phase 1: Atomic write (MULTI/EXEC) of result + status + idempotency key to Redis
2. Phase 2: Publish next task(s) to Kafka; set `next_published=true`
3. Only after Phase 2: commit Kafka offset

Committing the Kafka offset before Phase 1 completes is a contract violation.

### AC-EXEC-003 — Idempotency Key Check
Before executing a step, a worker MUST check for the existence of the idempotency key:
```
GET {wf:<workflow_id>}:step:<step_id>:idem
```
If the key exists, the worker MUST return the cached result without re-executing.

### AC-EXEC-004 — Lease Renewal
For steps expected to exceed 60 seconds, the executing worker MUST renew the lease every 60 seconds:
```
EXPIRE {wf:<workflow_id>}:step:<step_id>:lease 120
```
Failure to renew before lease expiry allows another worker to acquire the lease and re-execute the step.

### AC-EXEC-005 — Governance Token Validation
Before executing any task, a worker MUST validate:
1. Token signature is valid (HMAC-SHA256 or equivalent)
2. Token is not expired
3. Token authorizes the task type being executed
4. Token has not been revoked (check local revocation cache)

A task MUST NOT execute if any of these checks fail.

### AC-EXEC-006 — Fail-Closed Governance Default
The governance evaluation algorithm MUST be fail-closed. When no matching policy exists, the result MUST be REJECTED. When evaluation times out, the result MUST be REJECTED (HTTP 503). APPROVED MUST NOT be the default.

---

## 7. Memory Contract

### AC-MEM-001 — Memory Tier Hierarchy
Memory access MUST follow the four-tier hierarchy in ascending latency order:
1. Sensory Memory (in-process buffer, <1ms)
2. Working Memory (Redis Cluster, <10ms)
3. Long-Term Memory (Postgres + Weaviate, <100ms)
4. Episodic Memory (Weaviate vector search, <500ms)

Tier-skipping (e.g., writing directly to LTM without going through WTM) is permitted only when explicitly required by the use case and documented in the calling code.

### AC-MEM-002 — Working Memory Key Schema
All Working Memory (WTM) keys MUST use the hashtag schema:
```
{wf:<workflow_id>}:<suffix>
```
No WTM key MUST span multiple workflow hashtag groups in a single MULTI/EXEC.

### AC-MEM-003 — LTM Write Conflict Handling
Concurrent writes to the same LTM key MUST use vector clock versioning. A write MUST NOT silently overwrite a higher-versioned entry. Conflicts MUST be logged.

### AC-MEM-004 — Episodic Memory Read-After-Write
For reads that must observe a just-written episode (same workflow, same execution context), the code MUST use the local write-ahead buffer (5-second TTL) rather than relying on Weaviate propagation.

### AC-MEM-005 — LLM Cache Opt-In
LLM response caching MUST default to `cacheable=False`. No LLM call MUST be cached without explicit `cacheable=True` from the caller. This constraint is enforced at the capability layer.

### AC-MEM-006 — Access Count Tracking (Async)
LTM access counts MUST be tracked via asynchronous batch reporting (flush every 60 seconds). Synchronous access count tracking in the read hot path is a contract violation.

---

## 8. Scheduling Contract

### AC-SCHED-001 — Embedded Scheduler
The priority queue and deadline scheduler MUST run in-process within each worker. No standalone scheduling service exists. Any implementation that introduces a standalone scheduler service violates this contract.

### AC-SCHED-002 — Priority Queue Ordering
The local priority queue MUST order tasks by deadline (EDF — Earliest Deadline First) with starvation prevention. A task that has been queued longer than the starvation threshold MUST be promoted regardless of priority.

### AC-SCHED-003 — KEDA Autoscaling
Cluster-level scaling MUST be driven by KEDA reading Kafka consumer lag. Native Kubernetes HPA targeting CPU/memory MUST NOT be the primary autoscaling mechanism for workers.

### AC-SCHED-004 — On-Demand Floor
At least 3 worker instances MUST always run on on-demand (non-preemptible) infrastructure. KEDA `minReplicaCount` MUST be set to 3. Setting it to 0 or 1 is a contract violation.

---

## 9. Networking Contract

### AC-NET-001 — Internal mTLS
All service-to-service communication within the cluster MUST use mutual TLS (mTLS). Services MUST validate the peer certificate against the Intermediate CA before accepting connections.

### AC-NET-002 — Leaf Certificate Rotation
TLS leaf certificates MUST have a maximum validity of 24 hours. Services MUST reload TLS configuration on certificate rotation without process restart.

### AC-NET-003 — Worker Egress Restriction
Worker pods MUST only egress to:
- Cluster Manager (port 9090)
- Policy Service (port 8080)
- Capability Registry (port 9091)
- Redis Cluster (port 6380)
- Kafka (port 9092)
- Postgres (port 5432)
- Weaviate (port 8080)
- Vault (port 8200)
- Prometheus (port 9090 scrape — inbound only)

Worker-to-worker direct connections are prohibited (AC-DEP-001).

### AC-NET-004 — NetworkPolicy Enforcement
A Kubernetes `NetworkPolicy` restricting worker egress MUST be deployed and must match the allowed egress list in AC-NET-003. Absence of a NetworkPolicy is a deployment contract violation.

### AC-NET-005 — No Plaintext Internal Traffic
No AEOS service MUST accept plaintext (non-TLS) connections from other AEOS services. External-facing services MAY terminate TLS at the load balancer, but the load-balancer-to-service leg MUST be TLS.

---

## 10. Governance Contract

### AC-GOV-001 — No Execution Without Token
No task MUST execute without a valid governance token. This is a hard invariant. There is no bypass, no flag, no environment variable that disables this check in production.

### AC-GOV-002 — Fail-Closed Default
`AEOS_GOVERNANCE_FAIL_OPEN` MUST default to `false`. If set to `true`, the service MUST log a CRITICAL-severity warning on every startup and on every fail-open governance decision. Audit logs MUST record every fail-open evaluation.

### AC-GOV-003 — Token Expiry Re-evaluation
Workers MUST proactively re-evaluate governance tokens at least 5 minutes before expiry. Expired tokens MUST NOT be treated as valid. Execution MUST pause (not fail) during re-evaluation.

### AC-GOV-004 — Policy Evaluation Timeout
The Policy Service evaluation timeout MUST default to 5 seconds. On timeout, the result MUST be REJECTED (not APPROVED). This timeout MUST be configurable but MUST NOT be set to 0 (no timeout).

### AC-GOV-005 — Audit Log Completeness
Every governance evaluation MUST produce an audit log entry containing: task_id, task_type, policy_id matched, decision, reason, timestamp_ns, worker_node_id, token_id. Audit log writes MUST be synchronous (not fire-and-forget) for REJECTED decisions.

---

## 11. Security Contract

### AC-SEC-001 — Three-Layer PKI
All TLS certificates MUST be issued by the Vault Intermediate CA, which MUST be signed by the offline Root CA. Certificates issued directly by the Root CA for operational services are a contract violation.

### AC-SEC-002 — RBAC Revocation Latency
RBAC revocations MUST propagate to all workers within 1 second (Kafka delivery SLA). The 5-minute permission cache TTL MUST be superseded by Kafka revocation events. Workers MUST process revocation events with priority over task events.

### AC-SEC-003 — Secret Management
All secrets (API keys, database passwords, TLS private keys) MUST be sourced from Vault at runtime. Secrets MUST NOT be stored in environment variables, ConfigMaps, or container images. Vault agent sidecar injection is the required delivery mechanism.

### AC-SEC-004 — Container Hardening
All AEOS containers MUST:
- Run as non-root user (UID ≥ 1000)
- Use a read-only root filesystem
- Drop all Linux capabilities except `NET_BIND_SERVICE` if port < 1024
- Have no `privileged: true` security context

### AC-SEC-005 — Supply Chain
All container images MUST be built from pinned base image digests (not tags). Images MUST be signed (Sigstore/Cosign). The OPA admission controller MUST reject unsigned images.

---

## 12. Plugin Contract

### AC-PLUG-001 — Plugin Interface
All plugins MUST implement the `AEOSPlugin` interface:
```
initialize(kernel: HyperKernel) → None
teardown() → None
health_check() → PluginHealth
get_capabilities() → list[str]
```

### AC-PLUG-002 — Plugin Isolation
Plugins MUST NOT access internal kernel state directly. All kernel interaction MUST go through the published `KernelAPI` interface. Direct access to `kernel._state` or similar private attributes is a contract violation.

### AC-PLUG-003 — Plugin Failure Isolation
A plugin that raises an unhandled exception in `initialize()` MUST cause its own failure but MUST NOT prevent other plugins from initializing. The kernel MUST log the failure and continue.

### AC-PLUG-004 — Plugin Registration
Plugins MUST NOT self-register. They MUST be registered by the kernel during LOADING phase via `kernel.register_plugin()`. Plugins that attempt to register themselves via import side effects are a contract violation.

---

## 13. Extension Points

The following are the only sanctioned extension points in Phase 9:

| Extension Point | Interface | Location |
|----------------|-----------|---------|
| Custom node executor | `BaseNodeExecutor` | `app/execution/executor.py` |
| Custom node type | `GraphNode` subclass | `app/execution/graph.py` |
| Custom memory backend | `CheckpointStore` ABC | `app/execution/checkpoint.py` |
| Custom agent | `CognitiveAgent` subclass | `app/agents/cognitive.py` |
| Custom plugin | `AEOSPlugin` interface | `app/kernel/plugins.py` |
| Custom policy evaluator | `PolicyEvaluator` ABC | `app/governance/policy.py` |
| Custom metrics backend | `MetricsBackend` ABC | `app/execution/metrics.py` |

### AC-EXT-001
**MUST NOT** — Extension implementations MUST NOT bypass any of the contracts in sections 4–12. An extension that violates a contract is a defect regardless of whether the core framework detects it.

---

## 14. Cloud Contract

### AC-CLOUD-001 — PodDisruptionBudgets
The following PDBs MUST be deployed:

| Component | `minAvailable` |
|-----------|---------------|
| Cluster Manager | 2 |
| Capability Registry | 2 |
| Workers | 3 |
| Policy Service | 1 |

### AC-CLOUD-002 — PodAntiAffinity
Workers MUST be spread across availability zones via `topologyKey: topology.kubernetes.io/zone`. All 3 Cluster Manager pods MUST reside in different AZs.

### AC-CLOUD-003 — Resource Limits
All pods MUST have resource requests AND limits set. Pods without resource limits MUST NOT be admitted (enforced via OPA admission controller).

### AC-CLOUD-004 — Spot Instance Floor
A minimum of 3 worker pods MUST run on on-demand (non-preemptible) instances at all times. This is enforced via the on-demand node group `minSize: 3` in Terraform.

### AC-CLOUD-005 — Kafka Partitions
All task topics (`aeos.tasks.*`) MUST have exactly 200 partitions. Any deployment script that creates these topics with fewer than 200 partitions is a contract violation.

---

## 15. Observability Contract

### AC-OBS-001 — Histogram Metrics
All latency metrics MUST use Prometheus Histogram format (`_bucket/_sum/_count`). Pre-computed Summary quantiles are prohibited.

### AC-OBS-002 — Required Metrics
Every worker MUST emit the following metrics (minimum set):
```
aeos_task_execution_duration_seconds{task_type, status}  # Histogram
aeos_task_queue_depth{priority}                           # Gauge
aeos_step_execution_duration_seconds{node_type, status}  # Histogram
aeos_governance_evaluation_duration_seconds{decision}    # Histogram
aeos_redis_operation_duration_seconds{operation}         # Histogram
aeos_kafka_consumer_lag{topic, partition}                 # Gauge
aeos_worker_in_flight_tasks                               # Gauge
```

### AC-OBS-003 — Distributed Tracing
All cross-service calls MUST propagate W3C `traceparent` headers. Trace context MUST be attached to Kafka message headers for async trace continuation.

### AC-OBS-004 — Structured Logging
All logs MUST be structured JSON. Required fields: `timestamp`, `level`, `service`, `worker_node_id`, `workflow_id` (when in workflow context), `trace_id` (when trace context is available), `message`.

### AC-OBS-005 — Audit Log Retention
Governance audit logs MUST be retained for a minimum of 90 days. Security event logs (authentication, authorization failures, revocations) MUST be retained for a minimum of 365 days.

---

*End of Architecture Contract — `015-ARCHITECTURE_CONTRACT.md`*
