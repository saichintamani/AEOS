# AEOS Phase 9 DRP — Architecture Decision Records

**Document:** `014-ARCHITECTURE_DECISION_RECORDS.md`  
**Status:** Approved  
**Produced by:** AEOS Design Remediation Team  
**Date:** 2026-07-06  
**Applies to:** Phase 9 — Distributed Runtime Platform (v1.1 specification)

---

## Overview

This document captures every significant architectural decision made during the Phase 9 DRP design and remediation process. Each ADR records the context, decision, alternatives considered, rationale, trade-offs, and the outcome of the decision. ADRs in status "Accepted" are binding for Phase 9B implementation.

ADRs are immutable once accepted. If a decision changes, a new superseding ADR is written; this document is not edited retroactively.

---

## ADR Index

| ID | Title | Status | Triggered by |
|----|-------|--------|-------------|
| [ADR-001](#adr-001) | Redis Cluster over Redis Sentinel | Accepted | CB-3, CB-4 |
| [ADR-002](#adr-002) | Kafka Consumer Group Separation by Topic Role | Accepted | CB-1 |
| [ADR-003](#adr-003) | Two-Phase Checkpoint with Late Offset Commit | Accepted | CB-2 |
| [ADR-004](#adr-004) | Fail-Closed Governance with Explicit Seed Policy | Accepted | CB-5 |
| [ADR-005](#adr-005) | Dynamic Governance Token Expiry | Accepted | CB-6 |
| [ADR-006](#adr-006) | Kafka Task Topic Partitions: 200 | Accepted | CB-7 |
| [ADR-007](#adr-007) | Raft Consensus for Cluster Membership | Accepted | HP-1, HP-2 |
| [ADR-008](#adr-008) | KEDA over Native Kubernetes HPA | Accepted | HP-4 |
| [ADR-009](#adr-009) | Execution Lease for Split-Brain Prevention | Accepted | HP-8 |
| [ADR-010](#adr-010) | Three-Layer PKI via HashiCorp Vault | Accepted | HP-10, NTH-2 |
| [ADR-011](#adr-011) | Immediate RBAC Revocation via Kafka Pub/Sub | Accepted | HP-11 |
| [ADR-012](#adr-012) | Weaviate for Episodic / Long-Term Vector Memory | Accepted | NTH-1 |
| [ADR-013](#adr-013) | Histogram Metrics (not Summary) for Latency | Accepted | MI-5 |
| [ADR-014](#adr-014) | LLM Response Cache: Opt-In Only | Accepted | MI-3 |
| [ADR-015](#adr-015) | Membership Table as Raft Log Projection | Accepted | HP-2 |
| [ADR-016](#adr-016) | Embedded Distributed Scheduler (not Standalone Service) | Accepted | HP-5 |
| [ADR-017](#adr-017) | Governance Tokens are AP (not CP) | Accepted | HP-9 |
| [ADR-018](#adr-018) | At-Least-Once Delivery with Idempotent Executors | Accepted | Design baseline |
| [ADR-019](#adr-019) | CRDT Conflict Resolution Deferred to Phase 10 | Accepted | MI-7 |
| [ADR-020](#adr-020) | Spot Instances with On-Demand Floor of 3 | Accepted | MI-10 |

---

## ADR-001

### Redis Cluster over Redis Sentinel

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** CB-3 (Redis Sentinel/Cluster confusion), CB-4 (MULTI/EXEC cross-slot)

#### Context

AEOS Phase 9 requires a distributed hot state store for workflow state, step results, idempotency keys, and execution leases. The store must support:
- Horizontal scale (100-node NFR requires more throughput than a single Redis instance can provide)
- Atomic multi-key operations within a workflow (MULTI/EXEC for two-phase checkpoint)
- High availability (no single point of failure)

Two Redis deployment modes were under consideration. v1.0 incorrectly mixed both, resulting in a design that was incoherent.

#### Decision

**Redis Cluster (cluster mode enabled)** is the deployment mode for AEOS Phase 9.

All workflow-scoped keys use the hashtag pattern `{wf:<workflow_id>}:<suffix>` to co-locate on a single hash slot, enabling `MULTI/EXEC` atomicity within a workflow.

#### Alternatives Considered

**Option A: Redis Sentinel**
- Sentinel provides HA for a single-primary Redis instance via automatic failover
- Pros: Simpler operational model; no cross-slot restrictions
- Cons: Single primary = single write bottleneck; no horizontal scale; cannot meet 100-node throughput NFR; MULTI/EXEC available on all keys (simpler, but throughput-limited)
- Rejected because: throughput ceiling too low for 100-node NFR

**Option B: Redis Cluster (selected)**
- Cluster mode automatically shards data across multiple primaries
- Pros: Horizontal scale; multiple write primaries; meets 100-node throughput NFR
- Cons: MULTI/EXEC restricted to keys on same hash slot; requires hashtag discipline
- Selected because: only option that scales horizontally

**Option C: Apache Cassandra**
- Wide-column store with tunable consistency
- Pros: Extremely high throughput; AP by default
- Cons: No atomic multi-key transactions; TTL management complex; operationally heavier; no built-in SETNX equivalent
- Rejected because: no atomic SETNX needed for execution leases

**Option D: etcd**
- Strongly consistent key-value store based on Raft
- Pros: Correct by design; watch API native; transactions supported
- Cons: Not designed for high-frequency writes (throughput ceiling ~10k ops/sec); TTL management is awkward; not suitable for hot workflow state
- Rejected because: throughput insufficient for workflow hot state at scale; we already use Raft separately for cluster membership

#### Rationale

Redis Cluster is the only option that satisfies all three requirements: horizontal scale, HA, and atomic per-workflow transactions. The hashtag constraint adds implementation discipline but is fully manageable with the `_wf_key()` helper function.

#### Trade-offs

| Factor | Redis Cluster | Redis Sentinel |
|--------|--------------|----------------|
| Horizontal scale | Yes (N primaries) | No (1 primary) |
| MULTI/EXEC | Same-slot only | All keys |
| Operational complexity | Higher | Lower |
| Failure blast radius | Per-shard | Full instance |
| Meets 100-node NFR | Yes | No |

#### Consequences

1. All code touching Redis must use the `_wf_key()` helper; bare key strings are prohibited
2. MULTI/EXEC must never span keys from different workflows
3. Redis Cluster must be provisioned with a minimum of 3 primaries + 3 replicas (standard 6-node Redis Cluster minimum)
4. Cluster mode requires TLS; `AEOS_REDIS_URL` must use `rediss://` scheme

---

## ADR-002

### Kafka Consumer Group Separation by Topic Role

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** CB-1 (consumer group ID collision)

#### Context

AEOS workers consume messages from two categories of Kafka topics with different delivery semantics:

1. **Task topics** (`aeos.tasks.*`): Each task message should be processed by exactly one worker. This requires competing-consumer semantics.
2. **Event/broadcast topics** (`aeos.events.*`): Each event message should be processed by every worker (e.g., governance policy updates, cluster membership changes). This requires fan-out semantics.

v1.0 used a single `group_id="aeos-workers"` for all topics, which inadvertently applied competing-consumer semantics to broadcast topics — in a 10-worker cluster, each governance event reached only 1 of 10 workers.

#### Decision

Workers maintain **two separate Kafka consumers** with different group configurations:

- **Task consumer:** `group_id="aeos-workers"` (shared across all workers — one worker per message)
- **Event consumer:** `group_id=f"aeos-worker-{node_id}"` (unique per worker — every worker receives every message)

#### Alternatives Considered

**Option A: Single consumer, topic-based routing in application code**
- One consumer subscribes to all topics; application logic routes based on topic name
- Pros: Single consumer connection; simpler lifecycle management
- Cons: Shared group ID still applies competing-consumer semantics to all topics; routing complexity hidden in application code; still broken
- Rejected because: does not solve the group ID problem

**Option B: Separate consumer per topic category (selected)**
- Task consumer: shared group; event consumer: per-worker group
- Pros: Kafka semantics correctly model intent; no application-level routing needed
- Cons: Two consumer connections per worker; slightly higher broker connection overhead
- Selected because: correct by design; intent is explicit in configuration

**Option C: Use Kafka Streams or a pub/sub abstraction**
- Abstract the delivery semantics difference behind a library
- Pros: Cleaner API; hides Kafka details
- Cons: Adds dependency; overkill for two consumer types; Kafka Streams requires KTable which adds complexity
- Rejected because: over-engineering for a solved configuration problem

#### Rationale

The competing-consumer vs fan-out distinction is a fundamental Kafka semantic. The correct fix is at the group ID configuration level, not the application logic level. Two consumers with correctly scoped group IDs is the idiomatic Kafka solution.

#### Trade-offs

| Factor | Single consumer | Dual consumer (selected) |
|--------|----------------|--------------------------|
| Broadcast correctness | Wrong (CB-1) | Correct |
| Connection overhead | 1 connection | 2 connections |
| Configuration clarity | Low | High |
| Operational complexity | Lower | Marginally higher |

#### Consequences

1. Workers allocate two AIOKafkaConsumer instances on startup
2. `AEOS_KAFKA_WORKER_NODE_ID` must be unique across all workers (default: hostname + process ID)
3. Event consumer group IDs must not be shared — sharing would silently revert to competing-consumer semantics
4. Consumer group naming convention: task groups use `aeos-<role>`, event groups use `aeos-worker-<node_id>`

---

## ADR-003

### Two-Phase Checkpoint with Late Offset Commit

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** CB-2 (Kafka offset committed before checkpoint)

#### Context

Each worker step involves two state transitions that must be durable before the task can be considered complete:
1. The step result must be written to Redis (so downstream steps can read it)
2. The next task(s) must be published to Kafka (so the workflow can continue)
3. The Kafka offset must be committed (so the task is not redelivered)

v1.0 committed the Kafka offset at step acquisition time — before any of these were durable. A crash between offset commit and Redis write would permanently lose the task.

#### Decision

The checkpoint protocol is two-phase with Kafka offset committed only after Phase 2 completes:

**Phase 1 (atomic via MULTI/EXEC):**
- Write result blob to `{wf:<id>}:step:<n>:result`
- Set status to `COMPLETED` at `{wf:<id>}:step:<n>:status`
- Write idempotency key to `{wf:<id>}:step:<n>:idem` (24h TTL)

**Phase 2:**
- Publish next task(s) to Kafka
- Set `{wf:<id>}:step:<n>:next_published=true`

**Kafka commit:** Committed only after Phase 2 completes (step 8f of the scheduler loop).

#### Alternatives Considered

**Option A: Commit offset at slot acquisition (v1.0 approach)**
- Pros: Simplest implementation; no crash recovery complexity
- Cons: Task lost if worker crashes after offset commit but before Redis write; unrecoverable
- Rejected because: unrecoverable data loss on crash

**Option B: Exactly-once semantics via Kafka transactions**
- Kafka supports transactional producers that make publish + offset commit atomic
- Pros: Mathematically exactly-once for the Kafka→Redis→Kafka path
- Cons: Requires Kafka transactional producer (performance overhead); does not cover external side effects (LLM calls, tool calls); Redis write is still outside the Kafka transaction boundary
- Rejected because: external side effects (LLM/tool) cannot be made exactly-once regardless; at-least-once + idempotency is the industry-standard approach for this class of problem

**Option C: Two-phase checkpoint (selected)**
- Pros: Recoverable from any crash point; orphan scanner handles `next_published=absent` case; idempotency keys prevent double-execution on retry
- Cons: Three Redis writes per step (result, status, idem); `next_published` flag requires orphan scanner implementation
- Selected because: provably recoverable; doesn't require Kafka transactional overhead

#### Rationale

At-least-once delivery with idempotent executors is the correct model for distributed workflow execution. The two-phase checkpoint creates three observable intermediate states, all of which the orphan scanner can detect and recover:
- State 1: No Redis keys (Phase 1 not started) → requeue
- State 2: Redis keys present, `next_published` absent → re-publish and set flag
- State 3: `next_published=true` but offset not committed → commit offset (idempotent re-commit)

#### Trade-offs

| Factor | Early offset commit | Two-phase checkpoint (selected) |
|--------|--------------------|---------------------------------|
| Crash safety | None | Full (3 recovery states) |
| Redis writes per step | 1 | 3 |
| Implementation complexity | Low | Medium |
| Orphan scanner required | No | Yes |
| Data loss on crash | Possible | Not possible |

#### Consequences

1. Orphan scanner must be implemented and run continuously (§16.2)
2. All step executors must be idempotent (safe to re-execute if the idempotency key is absent)
3. Idempotency keys use 24h TTL — tasks that are retried more than 24 hours after original execution will re-execute (acceptable: retries at that interval are human-driven)
4. Phase 1 MULTI/EXEC failure (e.g., Redis Cluster shard failure) causes the task to be redelivered by Kafka — this is safe because no state was written

---

## ADR-004

### Fail-Closed Governance with Explicit Seed Policy

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** CB-5 (fail-open governance default)

#### Context

AEOS is an autonomous agent execution platform. Every task that executes consumes compute, may access external APIs, and may take real-world actions (send emails, modify data, call external services). An incorrect governance decision has operational, security, and compliance consequences.

v1.0 had a governance engine that defaulted to approving tasks when no matching policy existed, and approving tasks on policy evaluation timeout. This "fail-open" design meant the governance layer provided no protection for novel task types or during Policy Service degradation.

#### Decision

The AEOS governance engine is **fail-closed**:

1. If no policy matches a task type: **REJECTED** with reason `no_policy_matched`
2. If policy evaluation times out (default 5s): **REJECTED** with reason `policy_evaluation_timeout`, HTTP 503
3. A catch-all deny policy **must be seeded** as an explicit database record at deployment time
4. `AEOS_GOVERNANCE_FAIL_OPEN=false` is the required default; setting it to `true` logs a CRITICAL security warning

#### Alternatives Considered

**Option A: Fail-open with allowlist (v1.0 approach)**
- Default: approve; policy defines what to deny
- Pros: More permissive; easier to onboard new task types
- Cons: Any gap in policy coverage = unauthorized task execution; Policy Service outage = all tasks execute uncontrolled
- Rejected because: completely defeats the purpose of a governance layer

**Option B: Fail-open with logging**
- Default: approve but log when no policy matches
- Pros: Operational continuity; auditable
- Cons: Logs are not enforcement; compliance requires rejection, not logging
- Rejected because: logging is not governance

**Option C: Fail-closed with explicit seed policy (selected)**
- Default: deny; policy defines what to approve
- Pros: Correct security posture; Policy Service outage queues tasks rather than bypassing governance; novel task types require explicit policy before executing
- Cons: New task types require policy authoring before deployment; Policy Service downtime causes task queuing (operational impact)
- Selected because: correct security posture; operational impact of queuing is preferable to correctness impact of unauthorized execution

**Option D: Fail-closed with configurable override**
- Same as Option C but with `AEOS_GOVERNANCE_FAIL_OPEN=true` escape hatch
- Pros: Allows temporary override during incident response
- Cons: Override can be silently enabled, removing protection
- Accepted as compromise: override allowed but must log CRITICAL security warning with explicit acknowledgment

#### Rationale

The governing principle, added to §2.1.7: "Safety systems fail closed. A governance timeout is not approval." Any system that provides safety guarantees must define the safe state as the default state. For governance, the safe state is rejection — it preserves system integrity at the cost of availability. Availability is recoverable; unauthorized execution of an agent action may not be.

#### Trade-offs

| Factor | Fail-open | Fail-closed (selected) |
|--------|-----------|------------------------|
| Novel task types | Auto-approved | Rejected until policy exists |
| Policy Service outage | All tasks execute | All tasks queue |
| Security posture | Weak | Strong |
| Operational impact | None | Task queuing during outage |
| Compliance posture | Fails audit | Passes audit |

#### Consequences

1. Every deployment must run a policy seeding script before accepting tasks
2. The catch-all deny policy must be the lowest-priority policy in the database
3. Policy authoring is a required part of any new task type onboarding
4. Policy Service SLA becomes more critical — extended outage causes task queue backup
5. Policy Service circuit breaker (§13.4) must hold tasks in PENDING state, not drop them

---

## ADR-005

### Dynamic Governance Token Expiry

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** CB-6 (governance token expiry with no recovery path)

#### Context

Governance tokens authorize a workflow to execute. They are issued at task submission time and must remain valid throughout execution. A fixed 1-hour TTL (v1.0) fails for:
- Long-running batch jobs (hours to days)
- Tasks with uncertain queue wait times (token may expire before execution begins)
- Tasks requiring Policy Service re-evaluation mid-execution (no protocol existed)

#### Decision

Governance token expiry is calculated dynamically at issuance time:

```
expiry_seconds = max(
    task.deadline_unix - now_unix + queue_wait_estimate_s + 300,
    3600,    # floor: 1 hour minimum
    86400,   # ceiling: 24 hours maximum (security bound)
)
```

Workers re-evaluate expired tokens by calling the Policy Service 5 minutes before expiry. If the Policy Service is unavailable, the task is paused (not failed) and re-evaluation retried on the circuit breaker schedule.

#### Alternatives Considered

**Option A: Fixed 1-hour TTL (v1.0 approach)**
- Pros: Simple
- Cons: Fails for long-running tasks; no recovery path
- Rejected because: incorrect for any task with deadline > 1 hour

**Option B: Token-less governance (check on every step)**
- Per-step governance check at execution time
- Pros: Always uses current policy state
- Cons: Policy Service becomes a synchronous dependency for every step; high latency and availability impact
- Rejected because: Policy Service unavailability blocks all step execution

**Option C: Dynamic TTL with re-evaluation protocol (selected)**
- Token issued with a TTL covering the task's expected lifetime + buffer
- Workers proactively re-evaluate before expiry
- Pros: Long-running tasks supported; Policy Service is decoupled from step execution; re-evaluation is async
- Cons: Token represents a snapshot of policy at submission time; policy changes between submission and re-evaluation may not be reflected
- Selected because: correct balance of decoupling and safety

**Option D: Token refresh via background daemon**
- A background daemon proactively refreshes tokens on a schedule
- Pros: Transparent to workers
- Cons: Daemon is a new infrastructure component; single point of failure; complex coordination with worker lifecycle
- Rejected because: unnecessary complexity; per-worker re-evaluation is simpler and more reliable

#### Rationale

The key insight is that governance tokens are AP (Accepted in ADR-017). They represent a policy snapshot at submission time, not a live policy query. The dynamic TTL + re-evaluation protocol maintains this AP characteristic while ensuring tokens remain valid for the task's actual lifetime.

The 24-hour ceiling is a security bound: even if a task has a 7-day deadline, a token should not authorize execution for 7 days without re-evaluation. Daily re-evaluation provides a checkpoint where revoked policies take effect.

#### Trade-offs

| Factor | Fixed TTL | Dynamic TTL (selected) |
|--------|-----------|------------------------|
| Long-running task support | No | Yes |
| Policy freshness | 1-hour window | Re-evaluated at expiry |
| Implementation complexity | Low | Medium |
| Policy Service coupling | Low | Low (async re-eval) |
| Security bound | 1 hour | 24 hours (ceiling) |

#### Consequences

1. Workers must track token expiry and proactively re-evaluate
2. Queue wait estimate must be available at token issuance time (from Kafka lag metrics)
3. Tasks paused during re-evaluation must resume from last checkpoint (not restart)
4. 24-hour token ceiling means tasks with deadlines > 24 hours require at least one re-evaluation

---

## ADR-006

### Kafka Task Topic Partitions: 200

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** CB-7 (20 partitions cap cluster at 20 workers)

#### Context

Kafka assigns at most one partition per consumer in a shared consumer group at any point in time. With 20 partitions, a maximum of 20 workers can receive tasks concurrently. The Phase 9 NFR requires 100-node scalability.

#### Decision

All task topics (`aeos.tasks.*`) are created with **200 partitions**.

Derivation:
```
target_workers = 100  (NFR §3.2)
burst_factor   = 2.0  (headroom for burst parallelism)
partitions     = ceil(100 × 2.0) = 200
```

Partition count is immutable: Kafka allows increasing partitions but not decreasing (without topic recreation). 200 is provisioned from day one.

#### Alternatives Considered

**Option A: 20 partitions (v1.0)**
- Caps cluster at 20 workers
- Directly contradicts 100-node NFR
- Rejected

**Option B: 100 partitions (1:1 with NFR)**
- Supports exactly 100 workers; no headroom
- A temporary spike to 101 workers would leave 1 worker idle
- Rejected because: no headroom for burst

**Option C: 200 partitions (selected)**
- Supports up to 200 concurrent workers consuming from the same consumer group
- Provides 2× headroom for burst parallelism
- Costs: Kafka broker overhead scales approximately linearly with partition count; 200 partitions on a 3-broker MSK cluster is operationally normal
- Selected because: satisfies NFR with margin; operationally feasible

**Option D: 1000 partitions**
- Maximum future-proofing
- Rejected because: per-partition overhead at 1000 is non-trivial on small clusters; Phase 9 NFR is 100 nodes, not 500

#### Rationale

Kafka partition count is a deployment-time decision with long-term consequences. Under-provisioning (v1.0) created a hard cap on cluster size. Over-provisioning (1000) adds unnecessary broker overhead. 200 partitions provides 2× headroom over the NFR, which is the standard infrastructure planning multiple.

The immutability constraint means the correct answer must be provisioned at cluster initialization. Increasing partitions later requires careful coordination (triggering consumer group rebalance; may cause brief processing pauses).

#### Trade-offs

| Partitions | Max workers | Broker overhead | Headroom |
|-----------|-------------|-----------------|---------|
| 20 | 20 | Minimal | None |
| 100 | 100 | Low | None |
| **200 (selected)** | **200** | **Normal** | **2×** |
| 1000 | 1000 | High | 10× (excessive) |

#### Consequences

1. Topic creation script must specify `--partitions 200`
2. Partition count documented in deployment runbook as immutable post-creation
3. If partition count must change, procedure is: create new topic → migrate consumers → drain old topic → delete old topic
4. KEDA `maxReplicaCount` set to 100 (not 200) — partition count ceiling ≠ deployment target

---

## ADR-007

### Raft Consensus for Cluster Membership

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-1 (Raft persistent state not fsynced), HP-2 (membership table from Redis)

#### Context

AEOS Phase 9 requires a distributed cluster membership system that is:
- Authoritative (no stale reads during partitions)
- Fault-tolerant (continues operating with minority node failures)
- Consistent (all nodes agree on the same membership state)

v1.0 used Redis as the authoritative membership source, which is not a consensus system and can return stale data during partitions.

#### Decision

**Raft consensus** is used for cluster membership:
- Cluster Manager nodes form a Raft group (3 nodes minimum)
- Cluster membership is stored in the Raft log; the Raft state machine is the authoritative source
- Redis is a read-through cache (5s staleness bound) for membership reads that don't require strict consistency
- For routing decisions requiring < 5s staleness, workers query the Raft leader directly via gRPC

Persistent state (`currentTerm`, `votedFor`, log entries) must be **fsynced to disk before any RPC response** is sent.

#### Alternatives Considered

**Option A: Redis as authoritative membership source (v1.0)**
- Pros: Simple; low latency reads
- Cons: Not a consensus system; stale reads during partitions; no safety guarantee on split-brain
- Rejected because: incorrect under partition (CP requirement for membership)

**Option B: ZooKeeper**
- Industry-proven distributed coordination service; used by Kafka itself
- Pros: Production-hardened; rich API (watches, ephemeral nodes)
- Cons: Additional operational dependency; ZooKeeper is a Java service (AEOS is Python-first); Kafka is already being migrated away from ZooKeeper (KRaft mode)
- Rejected because: operational overhead; adds a Java service to a Python platform

**Option C: etcd**
- Raft-based key-value store; used by Kubernetes for cluster state
- Pros: Production-hardened Raft implementation; gRPC API; watch support
- Cons: General-purpose; AEOS membership model requires domain-specific log entry types; embedding etcd adds dependency complexity
- Considered but not selected: acceptable alternative, but embedding a custom Raft implementation gives more control over the membership log schema and integration with the kernel lifecycle

**Option D: Custom Raft implementation (selected)**
- Implement Raft within the Cluster Manager service
- Pros: Full control over log entry schema; direct integration with HyperKernel lifecycle; no external service dependency
- Cons: Implementation complexity; correctness requires careful testing; not production-hardened on day one
- Selected because: Phase 9 is a greenfield system; the Raft implementation is scoped to a single service with a small, well-defined API surface

#### Rationale

Cluster membership is a CP requirement (the CAP analysis in §2.2). During a network partition, the system must sacrifice availability for consistency: the minority partition must not accept new workflows. Only a consensus algorithm (Raft, Paxos, or equivalent) provides this guarantee. Redis, ZooKeeper, and etcd are all valid choices; custom Raft was selected for integration control.

#### Consequences

1. Cluster Manager must be deployed as exactly 3 nodes (Raft quorum: 2 of 3)
2. Raft term and vote must be persisted to WAL with fsync before responding to any RPC
3. Membership table in Redis is a cache only — code must not treat it as authoritative
4. `PodDisruptionBudget: minAvailable: 2` required for Cluster Manager to protect Raft quorum
5. Raft leader election adds up to ~300ms latency during failover (Raft election timeout)

---

## ADR-008

### KEDA over Native Kubernetes HPA

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-4 (HPA cannot read Kafka consumer lag)

#### Context

AEOS workers must scale based on Kafka consumer lag: if the task queue grows, more workers should be deployed; if the queue is empty, workers can scale down. Native Kubernetes HPA supports CPU/memory metrics natively and custom metrics via adapters, but reading Kafka consumer lag requires the `keda.sh` or a custom Prometheus → Kafka lag adapter.

#### Decision

**KEDA (Kubernetes Event-Driven Autoscaling)** is the required autoscaling mechanism for AEOS workers. A `ScaledObject` resource replaces the HPA.

#### Alternatives Considered

**Option A: Native HPA with CPU/memory metrics**
- Pros: No additional operator; built-in to Kubernetes
- Cons: CPU/memory are lagging indicators of queue depth; a burst of tasks doesn't immediately increase CPU until processing begins; scaling is reactive rather than predictive
- Rejected because: queue depth is the correct leading indicator; CPU is a lagging indicator

**Option B: Native HPA with custom metrics (Prometheus adapter)**
- Custom Prometheus adapter scrapes Kafka consumer group lag exporter, exposes it as a Kubernetes custom metric, HPA reads it
- Pros: No KEDA dependency
- Cons: 3-component chain (Kafka → lag exporter → Prometheus → adapter → HPA); multi-minute end-to-end delay; complex configuration; fragile dependency chain
- Rejected because: KEDA is the purpose-built solution; the 3-component chain is the exact problem KEDA was created to solve

**Option C: KEDA (selected)**
- KEDA is a CNCF project; operator reads Kafka lag directly
- Pros: Single operator; native Kafka support; configurable lag threshold and polling interval; minReplicaCount/maxReplicaCount built-in; production-hardened at Uber, Microsoft, etc.
- Cons: Additional operator to install and maintain; KEDA CRD version compatibility with Kubernetes API
- Selected because: purpose-built for this use case; mature project; eliminates the 3-component chain

**Option D: Custom autoscaler**
- Build a controller that watches Kafka lag and calls Kubernetes scale API
- Pros: Full control
- Cons: Reinventing KEDA; not production-hardened; significant implementation effort
- Rejected because: unnecessary when KEDA exists

#### Rationale

KEDA is the CNCF-standard solution for event-driven autoscaling in Kubernetes. Using it instead of the multi-adapter chain reduces operational complexity and improves scaling responsiveness. The Kafka trigger with `lagThreshold: "500"` means one additional worker is added for every 500 messages of queue lag, providing predictive scale-out before workers become CPU-saturated.

#### Trade-offs

| Factor | Native HPA (CPU) | Custom adapter chain | KEDA (selected) |
|--------|-----------------|---------------------|-----------------|
| Kafka lag as metric | No | Yes (complex) | Yes (native) |
| Operational complexity | Low | High | Medium |
| Scale-out latency | Minutes | Minutes | 15-30 seconds |
| CNCF project | Yes | No | Yes |
| Requires additional operator | No | Yes (adapter) | Yes (KEDA) |

#### Consequences

1. KEDA operator must be installed before Phase 9B deployment
2. KEDA version compatibility with the Kubernetes version in use must be verified
3. `ScaledObject` replaces `HorizontalPodAutoscaler` — both cannot target the same Deployment
4. KEDA `minReplicaCount: 3` enforces the on-demand floor (see ADR-020)

---

## ADR-009

### Execution Lease for Split-Brain Prevention

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-8 (split-brain double-execution not prevented)

#### Context

In a distributed cluster with network partitions, Raft consensus prevents the minority partition from accepting new workflows. However, for workflows already in-flight (in `EXECUTING` state) at the time of a partition, both halves of the cluster may attempt to execute the same step — Raft does not prevent re-execution of existing in-flight steps, only new acceptances.

#### Decision

Each workflow step is protected by a **Redis execution lease** (distributed mutex):

```
SETNX {wf:<workflow_id>}:step:<step_id>:lease <worker_node_id> EX 120
```

- `SETNX` (set if not exists) returns 1 on success (worker acquired lease, may execute)
- Returns 0 on failure (another worker holds lease; this worker skips the step)
- Lease TTL: 120 seconds; workers renew every 60 seconds for long-running steps
- On step completion: `DEL` the lease key

#### Alternatives Considered

**Option A: No lease (v1.0 approach)**
- Raft prevents new workflow acceptance but does not prevent re-execution of in-flight steps
- Rejected because: in-flight step double-execution is a real failure mode during partition

**Option B: Two-phase locking via Cluster Manager**
- Workers request a lock from the Cluster Manager (Raft leader) before executing each step
- Pros: Strongly consistent (CP); no Redis dependency for locking
- Cons: Every step execution requires a gRPC round-trip to Cluster Manager; significantly increases step latency; Cluster Manager becomes a bottleneck
- Rejected because: performance impact unacceptable for high-throughput workflows

**Option C: Redis execution lease (selected)**
- Redis SETNX provides a lightweight distributed mutex
- Pros: Low latency (same Redis cluster used for checkpoints); O(1) per step; TTL-based cleanup
- Cons: Redis is AP, not CP — during a Redis Cluster shard failure, both workers may simultaneously believe they hold the lease (brief window during failover)
- Selected because: for the split-brain prevention use case, Redis availability is sufficient; the window of dual-lease during Redis failover is milliseconds, not the minutes-long partition scenario being protected against

**Option D: Idempotency keys only (without lease)**
- Accept that double-execution may occur; rely on idempotency to make it safe
- Pros: Simpler; no lease management
- Cons: Idempotency prevents duplicate side effects, but double-execution during a long step wastes resources (two LLM calls, two tool invocations) and may cause race conditions in non-idempotent external APIs
- Accepted as supplementary: idempotency keys are still used alongside leases; leases prevent the double-execution, idempotency provides a safety net if leases are unavailable

#### Rationale

The execution lease is the minimal intervention that prevents concurrent double-execution without adding a synchronous Cluster Manager round-trip to every step. Combined with idempotency keys (ADR-003), the system provides defense-in-depth: the lease prevents the race, and idempotency handles any residual duplicates.

#### Consequences

1. Lease key must use the workflow hashtag schema: `{wf:<id>}:step:<n>:lease`
2. Lease TTL (120s) must exceed the maximum expected step duration for the 95th percentile step
3. Workers executing steps longer than 60 seconds must run a background renewal coroutine
4. Orphan scanner must handle the case where a lease exists for a step with status=FAILED (worker crashed after writing status but before deleting lease)

---

## ADR-010

### Three-Layer PKI via HashiCorp Vault

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-10 (security groups too permissive), NTH-2 (Vault vs Secrets Manager)

#### Context

AEOS Phase 9 requires TLS for all service-to-service communication (worker → Cluster Manager, worker → Policy Service, worker → Redis, etc.). Leaf certificates must be short-lived to limit the blast radius of private key compromise.

#### Decision

A three-layer PKI is used:

1. **Root CA:** Offline certificate authority (20-year validity, air-gapped); signs Intermediate CA certificate only
2. **Intermediate CA:** HashiCorp Vault PKI secrets engine (2-year validity); signs leaf certificates
3. **Leaf certificates:** 24-hour validity; auto-rotated by Vault agent sidecar running in each pod

#### Alternatives Considered

**Option A: AWS Certificate Manager (ACM)**
- Pros: Managed service; no operational overhead; auto-renewal via ACM
- Cons: ACM certificates cannot be exported (cannot be used for mutual TLS between internal services without ALB); ACM is AWS-specific (lock-in); no PKI secrets engine (cannot issue short-lived certs for internal services)
- Rejected because: insufficient for internal mTLS; AWS-only

**Option B: AWS Secrets Manager (for storing static long-lived certs)**
- Store pre-generated certificates in Secrets Manager; rotate manually or via Lambda
- Pros: Simple; managed by AWS
- Cons: Manual rotation is error-prone; long-lived certs increase blast radius; no automatic rotation for internal services
- Rejected because: does not solve the short-lived cert issuance requirement

**Option C: cert-manager (Kubernetes operator)**
- CNCF operator for certificate issuance in Kubernetes; supports Vault backend
- Pros: Kubernetes-native; CNCF project; no external service dependency
- Cons: cert-manager + Vault backend is effectively two components; cert-manager alone (ACME) requires public DNS for domain validation (not suitable for internal services)
- Considered: cert-manager can be used alongside Vault; may be added in Phase 9B for Kubernetes-native certificate management

**Option D: HashiCorp Vault PKI secrets engine (selected)**
- Vault is the industry-standard secrets management platform
- Pros: PKI secrets engine generates short-lived certs on demand; cloud-agnostic; dynamic secrets; lease revocation; fine-grained access control
- Cons: Vault is an operational dependency; requires HA deployment (3-node Vault cluster with Raft storage)
- Selected because: purpose-built for this use case; cloud-agnostic; dynamic cert issuance; mature operational playbook

#### Rationale

The three-layer PKI is the industry-standard certificate hierarchy for enterprise PKI. The offline Root CA ensures the root of trust is never exposed to network-accessible systems. The Vault Intermediate CA can be rotated without re-signing every leaf certificate. 24-hour leaf certs ensure that a compromised private key becomes invalid within one day, without manual intervention.

#### Trade-offs

| Factor | ACM | cert-manager | Vault PKI (selected) |
|--------|-----|-------------|---------------------|
| Short-lived cert issuance | No | Yes (w/ Vault) | Yes (native) |
| Cloud agnostic | No (AWS) | Yes | Yes |
| mTLS for internal services | No | Yes | Yes |
| Operational overhead | Low | Medium | Medium-High |
| Dynamic secrets | No | No | Yes |

#### Consequences

1. Vault must be deployed as a 3-node HA cluster with Raft storage backend
2. Vault agent sidecar must be injected into every AEOS pod for cert renewal
3. 24-hour leaf cert rotation means all services must reload TLS config without restart (hot reload)
4. Root CA private key must be stored offline (not in Vault); procedure for intermediate CA re-signing documented in runbook

---

## ADR-011

### Immediate RBAC Revocation via Kafka Pub/Sub

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-11 (RBAC revocation latency: 5 minutes)

#### Context

When an API key is revoked (e.g., compromised credential, terminated employee), the revocation must propagate to all workers within a security-acceptable timeframe. v1.0 used a 5-minute permission cache TTL, meaning a revoked key remained valid for up to 5 minutes.

#### Decision

RBAC revocation events are published to `aeos.events.governance` Kafka topic. All workers subscribe to this topic via their per-worker consumer group (established in ADR-002). Cache entries are invalidated immediately on receipt of a revocation event.

Maximum revocation latency: < 1 second (Kafka delivery latency from producer to all worker consumers).

Permission cache TTL retained at 5 minutes for normal reads; the Kafka revocation event provides out-of-band cache invalidation that supersedes the TTL.

#### Alternatives Considered

**Option A: 5-minute TTL only (v1.0)**
- Pros: Simple
- Cons: 5-minute window after revocation
- Rejected because: 5 minutes is unacceptable for security-critical revocation

**Option B: Zero-TTL cache (always check authorization service)**
- Every permission check calls the Authorization Service synchronously
- Pros: Immediate consistency
- Cons: Authorization Service becomes a synchronous critical path dependency for every action; latency impact; single point of failure
- Rejected because: availability impact unacceptable

**Option C: Short TTL (e.g., 30 seconds)**
- Reduce cache TTL to 30 seconds
- Pros: Better than 5 minutes; no new infrastructure
- Cons: 30-second window still allows significant unauthorized access; does not eliminate the window
- Rejected because: doesn't solve the problem, just reduces it

**Option D: Kafka revocation events (selected)**
- Revocation event published to Kafka; workers consume and invalidate immediately
- Pros: < 1 second propagation; no polling; leverages existing Kafka infrastructure; fan-out semantics already established
- Cons: Requires workers to maintain a Kafka consumer for governance events (already required for CB-1 fix); Kafka delivery latency is not zero (typically 10-200ms)
- Selected because: correct solution; leverages existing infrastructure; acceptable latency

#### Rationale

The combination of 5-minute cache TTL (for normal reads) + immediate Kafka revocation events (for revocations) provides the correct trade-off: normal reads are fast (cache hit) with acceptable staleness (5 minutes for non-revoked permissions), and revocations propagate immediately. This is the standard pattern for event-driven cache invalidation.

#### Consequences

1. Permission cache must support explicit key invalidation (not just TTL expiry)
2. Workers must process revocation events with higher priority than task events
3. Revocation event schema must include: `entity_id`, `entity_type` (api_key, user, role), `revoked_permissions`, `effective_at`
4. If a worker's event consumer is lagging (e.g., consumer restart), it must replay revocation events from the last committed offset before processing new tasks

---

## ADR-012

### Weaviate for Episodic / Long-Term Vector Memory

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** NTH-1 (Weaviate vs OpenSearch justification)

#### Context

AEOS Phase 9 requires a vector database for episodic memory (semantic search over past execution traces) and long-term memory (knowledge retrieval). The database must support:
- Dense vector similarity search (cosine/dot product)
- Structured metadata filtering alongside vector search
- Schema-flexible object storage (memory entries have variable schemas)

#### Decision

**Weaviate** is the vector database for AEOS Phase 9.

#### Alternatives Considered

**Option A: OpenSearch with k-NN plugin**
- Pros: CNCF-adjacent; familiar Elasticsearch API; supports vector search via k-NN plugin
- Cons: Vector search is secondary capability (added plugin, not native); requires separate embedding pipeline; k-NN index is separate from document index (two-query hybrid search is complex); heavier operational footprint
- Rejected because: vector search is a plugin, not a first-class feature; embedding pipeline is an additional component

**Option B: Pinecone**
- Fully managed cloud-native vector database
- Pros: Zero operational overhead; highly optimized
- Cons: No self-hosted option; vendor lock-in; data leaves the cluster; compliance implications
- Rejected because: data residency requirements; vendor lock-in

**Option C: pgvector (Postgres extension)**
- Vector similarity search inside Postgres
- Pros: Single database for structured + vector data; no additional service
- Cons: Not optimized for high-dimensional vector search at scale; index quality degrades above ~1M vectors; HNSW index in pgvector is newer and less battle-tested than Weaviate's
- Considered for future: acceptable for Phase 9 initial deployment if Weaviate proves operationally complex; can be revisited in Phase 10

**Option D: Weaviate (selected)**
- Native vector database with HNSW indexing
- Pros: HNSW index is first-class (not a plugin); native multi-tenancy; GraphQL + REST API; schema-flexible objects; self-hosted option; active CNCF ecosystem
- Cons: Additional service to operate; smaller community than Elasticsearch; Kubernetes Helm chart is less mature than OpenSearch
- Selected because: purpose-built vector database; HNSW as first-class index; no separate embedding pipeline required

#### Consequences

1. Weaviate must be deployed with replication factor ≥ 2 for HA
2. Write consistency: QUORUM; read consistency: ONE (documented staleness: up to 500ms)
3. For same-workflow read-after-write, workers use a local write-ahead buffer (5s TTL) before Weaviate propagates
4. Weaviate schema must be versioned; schema migrations are manual (Weaviate does not auto-migrate)

---

## ADR-013

### Histogram Metrics (not Summary) for Latency

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** MI-5 (Prometheus Summary quantiles cannot be aggregated)

#### Context

AEOS Phase 9 has 3–100 worker nodes, each emitting latency metrics. Prometheus offers two metric types for quantile computation: Summary (pre-computed quantiles per process) and Histogram (bucketed counters that support cross-process aggregation).

#### Decision

All latency metrics in AEOS Phase 9 use **Prometheus Histograms** (`_bucket/_sum/_count`). Quantiles are computed via `histogram_quantile()` in Prometheus recording rules, not pre-computed at collection time.

Bucket boundaries: `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]` seconds.

#### Rationale

Pre-computed Summary quantiles cannot be mathematically combined. To compute the true P99 across 10 workers, you cannot average 10 individual P99 values — the result is not the 99th percentile of the combined distribution. Histograms (buckets + sums + counts) can be correctly aggregated via `sum()` across instances before applying `histogram_quantile()`.

With 3–100 workers, cluster-wide quantiles are a first-class operational requirement. Summary quantiles are a footgun in this context.

#### Trade-offs

| Factor | Summary | Histogram (selected) |
|--------|---------|---------------------|
| Cross-worker aggregation | Incorrect | Correct |
| Storage cost | Lower | Higher (buckets) |
| Accuracy | Exact (per process) | Approximate (bucket interpolation) |
| Grafana compatibility | Full | Full |

#### Consequences

1. All existing Phase 8 latency metrics must be converted to Histograms before Phase 9 observability dashboard is built
2. Prometheus recording rules must be written for P50/P95/P99 using `histogram_quantile()`
3. Bucket boundaries must be tuned to the expected latency distribution (step execution: 0.1–10s; governance: 0.001–1s)

---

## ADR-014

### LLM Response Cache: Opt-In Only

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** MI-3 (LLM cache default should be opt-in)

#### Context

AEOS workers make LLM API calls that can be expensive (latency + cost). A response cache can serve repeated identical prompts from Redis instead of calling the API. However, if the cache is opt-out (default: cache everything), prompt responses from one workflow may be served to another workflow that happens to have an identical prompt — potentially leaking workflow-specific information.

#### Decision

LLM response caching is **opt-in** (`cacheable=False` by default). Callers must explicitly pass `cacheable=True` to enable caching.

Cache keys include the full prompt content (SHA-256 hash) but not the `workflow_id` — cached responses are cross-workflow by design when opted in. Callers who opt in acknowledge that any workflow with an identical prompt may receive the cached response.

#### Rationale

The security risk of default-on caching outweighs the cost savings. A formatting instruction prompt (`cacheable=True` appropriate) is indistinguishable from a user-data-containing prompt (`cacheable=False` required) at the cache layer. The only safe default is opt-in.

Opt-in caching is appropriate for: deterministic, non-sensitive prompts (fixed formatting, code generation with static examples). Never opt-in for: prompts containing user PII, workflow-specific secrets, or context that must not leak between workflows.

#### Consequences

1. Existing code that assumes caching is automatic must be audited and updated
2. Cache key must not include `workflow_id` (cross-workflow caching is the opt-in value proposition)
3. Cache entries must have a TTL (default: 1 hour) to prevent indefinite serving of stale LLM responses
4. `AEOS_LLM_CACHE_DEFAULT_TTL_S` environment variable configures the TTL

---

## ADR-015

### Membership Table as Raft Log Projection

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-2 (membership table served from Redis — stale reads)

#### Context

Routing decisions (which worker can execute a given capability) require an accurate view of cluster membership. During a network partition, Redis may return stale membership data (showing departed workers as available, or not showing newly joined workers).

#### Decision

The authoritative cluster membership state is the **projection of the Raft log** maintained by the Cluster Manager's state machine. Redis is a read-through cache with a 5-second staleness bound.

- Normal routing reads: use Redis cache (low latency, bounded staleness)
- Routing requiring < 5s staleness: query Raft leader via gRPC (`GetMembership` RPC)
- During Raft leader election: routing reads return the last cached state (bounded staleness acceptable for brief election window ~300ms)

#### Rationale

Raft log projection is the canonical distributed systems pattern for derived state. The membership table IS the Raft log; Redis is merely a read cache for operational convenience. Making this distinction explicit prevents the class of bugs where code treats the cache as authoritative.

#### Consequences

1. Every Raft log entry that changes membership (JOIN, LEAVE, FAIL) must update Redis after the log entry is committed
2. Redis update failures must not fail the Raft commit — log the failure and retry asynchronously
3. Workers that read from Redis must handle the case where the membership data is stale (bounded by 5s TTL)
4. Monitoring must alert when Redis membership cache diverges from Raft state machine for more than 10 seconds

---

## ADR-016

### Embedded Distributed Scheduler (not Standalone Service)

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-5 (Distributed Scheduler drawn as standalone tier)

#### Context

Phase 8.3 implemented a priority queue and deadline scheduler in `app/execution/priority.py`. The v1.0 architecture diagram showed this as a standalone "Distributed Scheduler" service tier, implying a separate deployed service.

#### Decision

The **priority queue and deadline scheduler run in-process within each worker**. There is no standalone Distributed Scheduler service.

Each worker maintains its own local priority queue for the tasks assigned to it via Kafka partitions. Global scheduling is achieved by KEDA autoscaling (scale up when lag grows) and Kafka partition assignment (work distributed across workers by Kafka consumer group rebalancing).

#### Rationale

A standalone scheduler service is a single point of failure and an unnecessary network hop. The existing `PriorityQueue` and `DeadlineScheduler` implementations are lightweight in-memory structures that are correct to run per-worker. Kafka consumer group rebalancing provides the distributed scheduling function that the standalone scheduler was incorrectly drawn to provide.

#### Consequences

1. The architecture diagram no longer shows a standalone Distributed Scheduler service
2. Workers import `app.execution.priority.PriorityQueue` and `DeadlineScheduler` directly
3. Priority assignment must happen at task publication time (the producer sets the priority in the task message, which routes to the appropriate priority-tier topic)
4. Cross-worker priority comparison is not needed — within a worker, local EDF is correct; across workers, KEDA handles global load balancing

---

## ADR-017

### Governance Tokens are AP (not CP)

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** HP-9 (CAP positioning incorrect for governance tokens)

#### Context

v1.0 classified governance tokens as CP (consistent and partition-tolerant). The CAP analysis matters because it determines what happens during a network partition: CP systems block (or reject) rather than serve potentially stale data; AP systems continue serving, potentially with stale data.

#### Decision

**Governance tokens are AP.** They represent a snapshot of policy at task submission time. Workers use the token they received at submission — they do not re-query the Policy Service on every step.

CP systems in AEOS Phase 9:
- Cluster membership (Raft)
- Step execution (execution leases)
- Capability registry reads (quorum reads from Raft state machine)

AP systems in AEOS Phase 9:
- Event fabric (Kafka — at-least-once, not coordinated)
- Metrics collection (best-effort)
- Governance tokens (snapshot at submission time)
- LTM reads (eventual consistency via Weaviate)

#### Rationale

The governance token is issued once (at submission), signed (tamper-evident), and travels with the task envelope. During execution, workers validate the token's signature and expiry — no Policy Service round-trip. This is fundamentally AP: the token represents the policy that was in effect at submission time, and it continues to be valid (in the absence of expiry or revocation) regardless of Policy Service availability.

CP behavior (re-evaluating policy on every step) would make the Policy Service a synchronous dependency for every step — violating the availability requirement and introducing significant latency.

#### Consequences

1. Policy changes after task submission do not affect in-flight tasks (until token expiry or revocation event)
2. For high-sensitivity tasks, operators may configure shorter token TTLs to force earlier re-evaluation
3. Revocation (ADR-011) is the mechanism for immediate policy enforcement on in-flight tasks
4. The CAP documentation in §2.2 must clearly explain the submission-time snapshot semantics to avoid confusion

---

## ADR-018

### At-Least-Once Delivery with Idempotent Executors

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** Design baseline (not a specific review issue)

#### Context

Distributed messaging systems (Kafka) provide at-least-once delivery guarantees. Exactly-once semantics require transactional producers and consumers, and even then, cannot cover external side effects (LLM calls, tool invocations, API calls).

#### Decision

AEOS Phase 9 adopts **at-least-once delivery with idempotent executors** as the durability model. No claim of exactly-once is made for external side effects.

Every step executor must be idempotent: executing the same step twice with the same idempotency key produces the same result as executing it once. The idempotency key is stored in Redis with a 24-hour TTL.

LLM calls, tool calls, and external API calls are not idempotent by nature — the idempotency layer prevents re-execution by checking the Redis key before calling the external API. If the key exists, the cached result is returned without a new API call.

#### Rationale

Exactly-once for external side effects is not achievable in a distributed system without 2-phase commit across all external systems — including LLM APIs, external tools, and third-party services that AEOS does not control. At-least-once + idempotency is the industry-standard approach for this class of problem (used by Stripe, Temporal, AWS Step Functions, etc.).

The two-phase checkpoint protocol (ADR-003) ensures the idempotency key is written atomically with the result in Phase 1, preventing the window where a result exists but the key does not.

#### Consequences

1. All step executors must check the Redis idempotency key before executing
2. Step inputs must be fully deterministic from the task message (no executor-local non-determinism that would cause different results on retry)
3. The 24-hour idempotency window means late retries (> 24 hours after original execution) may re-execute — acceptable for the expected task retry patterns

---

## ADR-019

### CRDT Conflict Resolution Deferred to Phase 10

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** MI-7 (CRDTs for AP memory conflict resolution)

#### Context

Long-Term Memory (LTM) is an AP system (see ADR-017). Multiple workers may write to the same LTM key concurrently. Without a conflict resolution strategy, one write will overwrite the other (last-write-wins), potentially losing data.

CRDTs (Conflict-free Replicated Data Types) provide mathematically guaranteed conflict-free merge for specific data type operations. They are the correct long-term solution for AP memory conflict resolution.

#### Decision

CRDT implementation is **deferred to Phase 10**. Phase 9 uses vector clock versioning with last-write-wins (LWW) merge as an interim strategy, with explicit documentation of the merge conflict risk.

#### Rationale

Implementing CRDTs correctly is a significant engineering effort (requires CRDT type selection for each memory schema, merge function implementation, vector clock propagation, and operational tooling). Phase 9's primary goal is distributed execution correctness; LTM conflict resolution is a secondary concern that can be addressed incrementally.

LWW with vector clocks provides better observability than pure LWW (conflicts are detectable even if not automatically resolved) and is a stepping stone to CRDT implementation.

#### Consequences

1. LTM writes include a vector clock component
2. Conflict detection: writes that would overwrite a higher-versioned entry are logged as conflicts
3. Phase 10 must implement proper CRDT merge before LTM is used for high-concurrency writes
4. Phase 9 documentation explicitly warns: "Do not use LTM for high-concurrency write patterns until Phase 10 CRDT implementation"

---

## ADR-020

### Spot Instances with On-Demand Floor of 3

**Status:** Accepted  
**Date:** 2026-07-06  
**Triggered by:** MI-10 (spot instance preemption not addressed)

#### Context

AEOS workers can run on EC2 Spot instances to reduce cost (60–70% savings vs on-demand). However, Spot instances are preemptible with 2-minute warning. A cluster running 100% Spot instances is vulnerable to full eviction during AWS capacity events, losing all in-flight work and causing cold-start delays when capacity returns.

#### Decision

A minimum of **3 on-demand worker instances** must always be running. Spot instances scale from 0 up to 97 additional workers (total max: 100, matching the NFR).

The 3 on-demand floor:
1. Matches KEDA `minReplicaCount: 3` (ensures workers are always available)
2. Prevents cold-start latency during Spot capacity events
3. Maintains processing continuity during partial Spot preemption

Spot preemption handling: workers subscribe to the EC2 instance metadata endpoint for 2-minute preemption warnings. On warning receipt: stop consuming new tasks, publish in-flight step checkpoints to Redis, deregister from capability registry, allow Kafka consumer group rebalance.

#### Trade-offs

| Configuration | Cost | Resilience | Cold-start |
|--------------|------|------------|-----------|
| 100% Spot | Lowest | Worst | High |
| 3 on-demand floor + Spot (selected) | Low | Good | Minimal |
| 100% on-demand | Highest | Best | None |

#### Consequences

1. EKS node group configuration: 2 node groups (on-demand: min 3, max 3; spot: min 0, max 97)
2. KEDA `minReplicaCount: 3` must be honored by on-demand node group (not evictable)
3. Spot preemption handler is a required component of the worker process (not optional)
4. Checkpoint writes during preemption must complete within the 2-minute window — step results must not require more than 90 seconds to checkpoint

---

*End of Architecture Decision Records — `014-ARCHITECTURE_DECISION_RECORDS.md`*
