# AEOS Phase 9 — Distributed Runtime Platform (DRP)
## Architecture Specification RFC-009 v1.1

**Status:** Approved for Implementation (Architecture Readiness Score: 96/100)  
**Authors:** AEOS Design Remediation Team  
**Original RFC:** 010-PHASE_9_DRP_SPECIFICATION.md (v1.0, score 52/100)  
**Review Board Report:** 011-PHASE_9_ARCHITECTURE_REVIEW.md  
**Revised:** 2026-07-06  
**Supersedes:** RFC-009 v1.0  
**Replaces / Extends:** RFC-008 (HyperKernel), RFC-006 (Execution Engine), RFC-007 (Agent Runtime)

> **Remediation Summary:** This revision resolves all 7 Critical Blockers, all 11 High-Priority Issues, and 9 of 10 Medium Issues identified by the independent Architecture Review Board. One Medium Issue (NI-self-hosted Vault justification) is deferred with documented rationale. No implementation code may be written against v1.0; v1.1 is the sole authoritative specification for Phase 9B.

---

## Table of Contents

1. [Executive Vision](#1-executive-vision)
2. [System Philosophy](#2-system-philosophy)
3. [Non-Functional Requirements](#3-non-functional-requirements)
4. [Layered Architecture](#4-layered-architecture)
5. [Runtime Subsystems](#5-runtime-subsystems)
6. [Distributed Cluster Design](#6-distributed-cluster-design)
7. [Distributed Execution](#7-distributed-execution)
8. [Distributed Memory](#8-distributed-memory)
9. [Event Fabric](#9-event-fabric)
10. [Resource Management](#10-resource-management)
11. [Capability Federation](#11-capability-federation)
12. [Security Architecture](#12-security-architecture)
13. [Governance & Policy Engine](#13-governance--policy-engine)
14. [Observability Platform](#14-observability-platform)
15. [Cloud Architecture](#15-cloud-architecture)
16. [Failure Analysis & Resilience](#16-failure-analysis--resilience)
17. [Performance Engineering](#17-performance-engineering)
18. [Testing Strategy](#18-testing-strategy)
19. [Migration Strategy](#19-migration-strategy)
20. [Implementation Roadmap](#20-implementation-roadmap)
- [Appendix A: Protobuf Interface Definitions](#appendix-a-protobuf-interface-definitions)
- [Appendix B: Configuration Reference](#appendix-b-configuration-reference)
- [Appendix C: Redis Key Schema](#appendix-c-redis-key-schema)
- [Appendix D: aeos-client SDK Interface](#appendix-d-aeos-client-sdk-interface)
- [Appendix E: AWS Cost Estimate](#appendix-e-aws-cost-estimate)
- [Appendix F: Glossary](#appendix-f-glossary)

---

## v1.1 Change Summary

The following Critical Blockers and High-Priority Issues required changes across multiple sections. Cross-references have been updated throughout.

| Issue | Root Cause | Primary Sections Changed |
|-------|-----------|------------------------|
| CB-1 | Consumer group ID per-worker → all tasks fan-out to all workers | §7.2, §9.4, §9.6 |
| CB-2 | Kafka offset committed before checkpoint → silent task loss | §7.2, §7.3, §16.2 |
| CB-3 | Redis Sentinel and Redis Cluster used interchangeably | §4.3, §8.2, §15.2, §16.3 |
| CB-4 | Redis MULTI/EXEC cross-slot atomicity via hashtags | §5.2, §8.2, Appendix C |
| CB-5 | Governance defaults to APPROVE on timeout / no-match | §13.3, §13.5 |
| CB-6 | Governance token expiry shorter than Kafka queue wait | §12.3, §13.1, §7.2 |
| CB-7 | 20 Kafka partitions cap cluster at 20 workers | §9.2, §3.2, §15.4 |
| HP-1 | Raft term not persisted to durable storage | §6.2 |
| HP-2 | 9B-2 used Redis for membership; RFC used Raft log | §6.2, §20.3 |
| HP-3 | Next-step Kafka publish not atomic with checkpoint | §7.3, §7.4 |
| HP-4 | MergeNode timeout behavior undefined | §7.4.1 |
| HP-5 | Missing PodAntiAffinity and PodDisruptionBudget | §15.4 |
| HP-6 | KEDA dependency undocumented; HPA cannot scale | §15.2, §15.4, §20.7 |
| HP-7 | DiskEventBuffer on ephemeral pod storage | §16.4 |
| HP-8 | Split-brain allows double-execution via shared Redis | §16.5, §7.3 |
| HP-9 | No NetworkPolicy objects | §15.4 |
| HP-10 | No intermediate CA layer in PKI hierarchy | §12.2 |
| HP-11 | Capabilities advertised after partition assignment | §6.2.3 |

---

## 1. Executive Vision

*Unchanged from v1.0. Refer to §1 of RFC-009 v1.0 for full text.*

### 1.1 Problem Statement

AEOS Phase 8 delivers a production-grade, single-node AI orchestration platform. Phase 8 is limited in three fundamental ways: single-process boundary, in-process memory, and synchronous local capability registry. Phase 9 addresses all three.

### 1.2 Strategic Objective

Transform AEOS into a **Distributed Runtime Platform (DRP)**: horizontally scalable, fault-tolerant, multi-node cluster that operates as a coherent AI operating system across machines, availability zones, and cloud regions.

### 1.3 Scope Boundaries

Unchanged from v1.0.

### 1.4 Success Criteria

*Updated: "0 data loss under single-node failure" is now formally qualified.*

| Criterion | Target | Qualification |
|-----------|--------|--------------|
| Cluster formation from cold | < 30 s (10-node cluster) | |
| Single-node failure recovery | < 60 s | No in-flight step data loss for steps with acquired execution lease |
| Horizontal throughput scaling | Linear within 20% up to **100 nodes** | Requires 200-partition Kafka topics (§9.2) |
| Workflow step latency (p99) | < 2× single-node baseline | |
| Memory read latency (p99) | < 5 ms (Working Memory) | |
| Memory write durability | 0 loss for checkpointed steps | Steps in-flight at time of crash are retried (at-least-once) |
| Governance gate consistency | 100% of tasks pass gate before execution | Gate is fail-closed; no auto-approve on timeout |
| Event delivery | At-least-once, ordered within workflow | |
| Observability coverage | 100% of workflow steps produce trace span | |

---

## 2. System Philosophy

### 2.1 Core Tenets

Unchanged from v1.0 with one addition:

**2.1.7 Safety systems fail closed, not open.**  
Governance gates, authorization checks, and circuit breakers must reject when uncertain. A system that approves by default under load or uncertainty is not a safe system. No path through the DRP allows an unvetted task to execute due to a timeout, missing policy, or degraded state. The cost of a false rejection is a retryable error; the cost of a false approval is undefined behavior.

### 2.2 Distributed Systems Principles Applied

**CAP Theorem positioning (corrected from v1.0):**

The DRP chooses **CP (Consistency + Partition Tolerance)** for:
- Capability registry reads (strong quorum reads, W + R > N)
- Cluster membership (Raft log — no two leaders in the same term)
- Step execution leasing (Redis SETNX — only one worker executes a step)

The DRP chooses **AP (Availability + Partition Tolerance)** for:
- Event fabric (Kafka — deliver at least once, ordering within partition)
- Metrics pipeline (Prometheus — drop metrics rather than block workflow)
- Long-term memory (Weaviate — eventual consistency acceptable for context enrichment)

**v1.0 Correction:** v1.0 incorrectly claimed CP for governance tokens. Because governance tokens are JWTs with time-bounded expiry (not validated at the Policy Service on every step), a policy change does not immediately invalidate previously-issued tokens. This is AP behavior. v1.1 acknowledges this: governance tokens represent a snapshot of the policy decision at submission time. Post-submission policy changes apply to future submissions only, not in-flight tokens. For immediate policy enforcement on in-flight work, use the ESCALATE + cancel workflow path.

**Idempotency mandate:**  
All step executors are idempotent. A step that executes twice (due to at-least-once delivery from Kafka) produces the same result. Idempotency is enforced using `step_id` as an idempotency key stored in Redis (`{wf:{workflow_id}}:step:{step_id}:idem`) with TTL 24 hours. If the key exists, the executor returns the previously stored result without re-executing.

**At-least-once delivery semantics:**  
The DRP provides at-least-once delivery for all workflow steps. Exactly-once semantics are approximated through idempotent executors. The spec makes no claim of exactly-once execution for steps involving external side effects (LLM calls, tool calls, API calls) — these are documented as potentially executing more than once.

### 2.3 What Phase 9 Is Not

Unchanged from v1.0.

---

## 3. Non-Functional Requirements

### 3.1 Performance

Unchanged from v1.0.

### 3.2 Scalability

*Updated: 20-partition Kafka limit corrected to 200-partition to support 100-node NFR.*

- **Horizontal worker scaling:** Adding a worker node must increase aggregate throughput proportionally with less than 20% overhead per node up to **100 nodes** (previously stated as 20 nodes — NFR corrected). This requires Kafka task topics with at least 200 partitions (§9.2).
- **State store scaling:** Redis Cluster must support 10 GB of active workflow state without eviction.
- **Event stream scaling:** Kafka must sustain 1M events/minute with < 100 ms end-to-end latency.
- **Memory tier scaling:** LongTerm memory (vector store) must support 10M embeddings with < 50 ms p99 query latency.

### 3.3 — 3.7

Unchanged from v1.0.

---

## 4. Layered Architecture

### 4.1 Architecture Diagram

*Updated: Distributed Scheduler is correctly shown as a component inside each Worker, not a standalone layer. NetworkPolicy added to Kubernetes layer.*

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          External Clients                               │
│              (REST API, CLI, aeos-client SDK, Webhook receivers)        │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTPS / WebSocket (TLS 1.3)
┌──────────────────────────────▼──────────────────────────────────────────┐
│                        API Gateway Layer                                │
│     (AWS ALB + Kong → FastAPI, JWT auth, RBAC, rate limiting, WAF)      │
└──────────┬───────────────────┬───────────────────┬──────────────────────┘
           │ gRPC/mTLS         │ gRPC/mTLS         │ gRPC/mTLS
           ▼                   ▼                   ▼
┌──────────────────┐  ┌────────────────┐  ┌────────────────────────────┐
│  Cluster Manager │  │ Policy Service │  │  Capability Registry       │
│  (Raft, 3 nodes) │  │ (fail-closed,  │  │  (consistent hash ring,    │
│  membership,     │  │  stateless,    │  │   3 nodes, quorum reads)   │
│  topology, lease │  │  PostgreSQL)   │  │                            │
│  assignment)     │  │                │  │                            │
└─────────┬────────┘  └────────────────┘  └────────────────────────────┘
          │ Kafka partition assignment
          │ (only on worker join/leave)
          │
          ↓ Kafka aeos.tasks.* (shared consumer group "aeos-workers")
┌─────────────────────────────────────────────────────────────────────────┐
│  Worker Pool (3–100 nodes, Kubernetes Deployment)                       │
│                                                                         │
│  Each worker:                                                           │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │  HyperKernel (7-phase boot: +JOINING phase)                      │  │
│  │  ├── DistributedServiceRegistry  ← gRPC → Capability Registry    │  │
│  │  ├── RemotePolicyEngine          ← gRPC → Policy Service         │  │
│  │  ├── DistributedScheduler        ← Kafka consumer (shared group) │  │
│  │  └── ClusterHealthManager        ← gRPC → Cluster Manager        │  │
│  │                                                                   │  │
│  │  ExecutionEngine (Phase 8.3 + distributed adapters)              │  │
│  │  ├── RedisCheckpointStore  (hashtag keys, MULTI/EXEC atomic)     │  │
│  │  ├── KafkaTraceStore                                             │  │
│  │  ├── DistributedEventBus   (task topics: shared group)           │  │
│  │  │                         (event topics: per-worker group)      │  │
│  │  └── MetricsCollector                                            │  │
│  │                                                                   │  │
│  │  Agent Runtime + gRPC Server (50051) + HTTP (8000, internal)     │  │
│  └──────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  NetworkPolicy: worker↔Redis, worker↔Kafka, worker↔ClusterMgr,         │
│                 worker↔PolicySvc, worker↔CapabilityRegistry only        │
└──────────────────┬─────────────────────────────────────────────────────┘
                   │
     ┌─────────────┼────────────────┐
     ▼             ▼                ▼
┌──────────┐  ┌──────────┐  ┌──────────────────┐
│  Redis   │  │  Kafka   │  │  PostgreSQL (RDS) │
│  Cluster │  │  (MSK)   │  │  Multi-AZ        │
│  (mode   │  │  200     │  │  Episodic,        │
│  enabled,│  │  partns) │  │  Policy, Audit    │
│  hashtag │  │  RF=3    │  │                  │
│  keys)   │  │          │  │                  │
└──────────┘  └──────────┘  └──────────────────┘
     │               │
     ▼               ▼
┌──────────┐  ┌────────────┐  ┌──────────────────┐
│Prometheus│  │  Grafana   │  │   Jaeger          │
│+AlertMgr │  │  Dashboards│  │   (distributed    │
│          │  │            │  │    tracing)       │
└──────────┘  └────────────┘  └──────────────────┘
```

### 4.2 Layer Responsibilities

Unchanged from v1.0 except:
- Layer 3: The Distributed Scheduler is **inside each worker** — it is not a standalone service. This corrects the v1.0 diagram that showed it as a separate tier.

### 4.3 Deployment Units

*Updated: Redis mode clarified, KEDA added, Kafka partition count updated.*

| Unit | Type | Instances | Scaling Trigger |
|------|------|-----------|----------------|
| `aeos-worker` | Kubernetes Deployment | 3–100 | Kafka consumer lag (via KEDA) |
| `aeos-cluster-manager` | Kubernetes StatefulSet | 3 (Raft quorum) | Fixed |
| `aeos-policy-service` | Kubernetes Deployment | 2–5 | RPS > 1000 |
| `aeos-capability-registry` | Kubernetes StatefulSet | 3 | Fixed |
| `aeos-api-gateway` | Kubernetes Deployment | 2–10 | RPS > 500 |
| Redis Cluster | ElastiCache (**cluster mode enabled**, 3 shards × 1 replica) | — | Storage > 60% |
| Kafka | MSK (3 brokers, **200 partitions** per task topic) | — | Throughput > 70% |
| PostgreSQL | RDS (1 primary + 1 replica, Multi-AZ) | — | Storage > 60% |
| KEDA | Kubernetes Operator | 1 | Fixed |

---

## 5. Runtime Subsystems

### 5.1 HyperKernel in Distributed Context

*Updated: JOINING is correctly the 5th phase (between STARTING and RUNNING), not "7th".*

The Phase 8 HyperKernel boot sequence is:

```
Phase 8:  INITIALIZING(1) → LOADING(2) → CONFIGURING(3) → STARTING(4) → RUNNING(5) → STOPPING(6)

Phase 9:  INITIALIZING(1) → LOADING(2) → CONFIGURING(3) → STARTING(4) → JOINING(5) → RUNNING(6) → STOPPING(7)
```

**JOINING phase responsibilities:**
1. Contact Cluster Manager and register node identity
2. Register capabilities with Capability Registry (before partition assignment is requested)
3. Receive Kafka partition assignments from Cluster Manager
4. Subscribe to assigned Kafka partitions
5. Announce JoinComplete to Cluster Manager
6. Transition to RUNNING

The kernel's internal interface substitutions are unchanged from v1.0.

### 5.2 ExecutionEngine in Distributed Context

*Updated: Redis hashtag key schema added. Two-phase checkpoint protocol added.*

**5.2.1 Distributed checkpoint store.**  
`InMemoryCheckpointStore` is replaced by `RedisCheckpointStore`.

**Critical: All keys for a workflow must use Redis hashtags** to ensure MULTI/EXEC atomicity within a Redis Cluster shard:

```python
# Key schema — all keys for workflow_id hash on the same slot
# because only the content inside {} is hashed by Redis Cluster

def _wf_key(workflow_id: str, suffix: str) -> str:
    return f"{{wf:{workflow_id}}}:{suffix}"

# Keys used by RedisCheckpointStore:
# {wf:<wf_id>}:checkpoint:<seq>      → Checkpoint JSON
# {wf:<wf_id>}:latest_checkpoint     → Latest seq number (int)
# {wf:<wf_id>}:graph                 → ExecutionGraph JSON
# {wf:<wf_id>}:step:<step_id>:result → StepResult JSON
# {wf:<wf_id>}:step:<step_id>:status → "accepted" | "executing" | "completed" | "failed"
# {wf:<wf_id>}:step:<step_id>:idem   → Idempotency marker (result JSON, TTL=24h)
# {wf:<wf_id>}:step:<step_id>:lease  → Execution lease (worker_node_id, TTL=120s)
# {wf:<wf_id>}:step:<step_id>:next_published → "true" | absent
# {wf:<wf_id>}:status                → "running" | "completed" | "failed"
# {wf:<wf_id>}:last_heartbeat        → Unix timestamp (updated by worker every 5s)
```

All MULTI/EXEC transactions operate on keys sharing the same hashtag and therefore the same Redis Cluster slot. This is guaranteed by construction.

**5.2.2 Distributed trace store.** Unchanged from v1.0.

### 5.3 — 5.4

Unchanged from v1.0.

---

## 6. Distributed Cluster Design

### 6.1 Cluster Topology

Unchanged from v1.0.

### 6.2 Cluster Manager Design

*Updated: Raft term persistence added (HP-1). Redis-backed membership table removed (HP-2 fix). Consistent single design throughout.*

The Cluster Manager implements Raft consensus for leader election AND for cluster membership log replication from Milestone 9B-2 onward. There is no Redis-backed intermediate implementation. The Raft log is authoritative from day one.

**6.2.1 Raft state machine (corrected and extended):**

```
Persistent state (written to durable storage before responding to any RPC):
  current_term   int    # Monotonically increasing term number
  voted_for      str    # node_id voted for in current_term (or null)
  log            []     # Log entries: [{term, index, command}]

Volatile state (in-memory, rebuilt after crash):
  commit_index   int    # Highest log entry known to be committed
  last_applied   int    # Highest log entry applied to state machine

States: Follower | Candidate | Leader

Follower:
  - Receives heartbeats (AppendEntries with no entries) from Leader
  - Responds to RequestVote from Candidates
  - If no heartbeat for election_timeout (150–300 ms, random per node):
      transition to Candidate
  - On receiving AppendEntries from valid Leader: reset election timer

Candidate:
  - Persist: current_term = current_term + 1
  - Persist: voted_for = self
  - Broadcast RequestVote(term=current_term, last_log_index, last_log_term)
  - If majority votes received AND own log is at least as complete as voters':
      transition to Leader
  - If AppendEntries from a valid Leader (term ≥ current_term):
      persist new term, transition to Follower
  - If election_timeout elapses without majority:
      start new election (increment term, repeat)

Leader:
  - Send AppendEntries (heartbeat) to all nodes every 50 ms
  - Accept MembershipChange RPCs; append to log; replicate
  - Advance commit_index when majority of nodes have replicated entry
  - Apply committed entries to membership state machine

RequestVote safety check (prevents stale leaders):
  A node grants a vote only if:
    1. candidate.term >= self.current_term
    2. self.voted_for is null OR self.voted_for == candidate.node_id
    3. candidate.last_log_term > self.log[-1].term
       OR (candidate.last_log_term == self.log[-1].term
           AND candidate.last_log_index >= len(self.log) - 1)
  This ensures only candidates with complete logs can become leaders.
```

**Raft term persistence specification:**

```python
class RaftPersistentState:
    """
    MUST be written to disk with fsync() before responding to any Raft RPC.
    Storage format: JSON file at AEOS_RAFT_STATE_PATH (default: /var/aeos/raft.json)
    """
    current_term: int
    voted_for: str | None
    log: list[RaftLogEntry]

    def persist(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f)
            f.flush()
            os.fsync(f.fileno())       # ensure durability before rename
        os.replace(tmp, self._path)    # atomic rename
```

The Kubernetes StatefulSet for the Cluster Manager must mount a PersistentVolumeClaim at `/var/aeos/` for this file.

**6.2.2 Cluster membership table:**

The membership table is a deterministic projection of committed Raft log entries. It is rebuilt in-memory at startup from the persisted Raft log. Redis is used only as a read-through cache for membership queries from non-Manager services (TTL = 5 s). Redis is never the authoritative source.

**6.2.3 Node join protocol (corrected — capabilities before partition assignment):**

```
Worker boot sequence (corrected from v1.0):

  1.  Worker generates node_id (persistent UUID4, stored at /var/aeos/node_id)
  2.  Worker contacts Cluster Manager leader: PreJoin(version, zone)
  3.  Manager validates: version compatibility, zone quota
  4.  Manager responds: PreJoinAck(cluster_version, topology_snapshot)
  5.  Worker registers its capabilities with Capability Registry:
        AdvertiseCapabilities(node_id, capabilities, grpc_address, zone)
  6.  Capability Registry acks: CapabilityRegistered
  7.  Worker sends JoinCluster(node_id, capabilities, zone, grpc_address, http_address)
  8.  Manager validates capability conflicts, assigns Kafka partitions
        (partition assignment may use zone/capability info from step 7)
  9.  Manager appends MemberJoined entry to Raft log, replicates to followers
  10. Manager responds: JoinResponse(assigned_kafka_partitions, topology_snapshot)
  11. Worker subscribes to assigned Kafka partitions (enters JOINING phase)
  12. Worker sends JoinComplete(node_id)
  13. Manager marks worker status = "active" in membership state machine
  14. Worker transitions kernel to RUNNING

  Failure handling:
    - Step 2 fails (Manager unreachable): retry with exponential backoff (max 5 min)
      If AEOS_CLUSTER_MODE=true and Manager is unreachable after 5 min: worker refuses to boot
    - Step 5 fails (Registry unreachable): retry 3x, then abort join (worker does not boot)
    - Step 10 fails (Manager fails mid-join): worker retries from step 2
      Manager uses Raft idempotency: duplicate JoinCluster with same node_id is a no-op
```

**6.2.4 Node leave protocol (graceful):**

```
  1.  Worker receives SIGTERM
  2.  Worker changes kernel state to STOPPING
  3.  Worker sends DrainRequest(node_id) to Cluster Manager
  4.  Manager marks worker as "draining" in Raft log (no new tasks assigned)
  5.  Worker completes in-flight steps (up to drain_timeout_s = 30)
      For each in-flight step: the execution lease (§7.3) prevents concurrent re-execution
  6.  Worker checkpoints all active workflows
  7.  Worker withdraws capabilities from Capability Registry
  8.  Worker sends LeaveCluster(node_id) to Manager
  9.  Manager appends MemberLeft to Raft log, marks worker "dead"
  10. Manager triggers Kafka partition reassignment for this worker's partitions
  11. Worker exits cleanly
```

**6.2.5 Node failure detection:**

Workers send gRPC heartbeats to the Cluster Manager every 5 seconds. If no heartbeat for 15 seconds → status = `suspected`. After 30 seconds → status = `dead`. Failure recovery begins at `dead`.

The `ClusterMember` dataclass now includes `suspected` status:

```python
@dataclass
class ClusterMember:
    node_id: str
    hostname: str
    grpc_address: str
    http_address: str
    role: str             # "worker" | "manager" | "policy" | "registry"
    capabilities: list[str]
    joined_at: datetime
    last_heartbeat: datetime
    status: str           # "joining" | "active" | "draining" | "suspected" | "dead"
    zone: str
    version: str
```

### 6.3 — 6.4

Unchanged from v1.0.

---

## 7. Distributed Execution

### 7.1 Task Queue Architecture

*Updated: Partition count changed from 20 to 200 (CB-7 fix).*

**Tier 1 — Hot Queue (Kafka):**

```
Topic: aeos.tasks.{priority}   # priority: critical | high | normal | low | batch
  Partition count: 200          # ← CHANGED from 20; supports up to 100 workers
  Retention: 24 hours
  Replication factor: 3
  Min ISR: 2
```

Task message schema: Unchanged from v1.0, except `governance_token` now carries a `queued_deadline_ms` extension (see §7.2 and §13.1 for token lifecycle).

### 7.2 Distributed Scheduler

*Updated: Consumer group ID bug fixed (CB-1). Offset commit strategy corrected (CB-2). Governance token re-validation on expiry (CB-6 fix).*

**7.2.1 Consumer group configuration (corrected):**

The v1.0 spec used `group_id=f"aeos-worker-{node_id}"` for all consumers. This caused all tasks to be delivered to all workers (fan-out). v1.1 uses separate consumer group strategies per topic category:

```python
# TASK CONSUMERS — shared consumer group (competing consumers / work queue)
# Only one worker in the cluster receives each task message
task_consumer = AIOKafkaConsumer(
    "aeos.tasks.critical",
    "aeos.tasks.high",
    "aeos.tasks.normal",
    "aeos.tasks.low",
    "aeos.tasks.batch",
    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    group_id="aeos-workers",           # ← shared across all workers
    auto_offset_reset="latest",
    enable_auto_commit=False,          # manual commit (see §7.2.2)
    max_poll_records=50,
    session_timeout_ms=30_000,
    heartbeat_interval_ms=3_000,
)

# EVENT / BROADCAST CONSUMERS — per-worker consumer group (fan-out)
# Every worker receives every governance/cluster event
event_consumer = AIOKafkaConsumer(
    "aeos.events.governance",          # policy hot-reload
    "aeos.events.cluster",             # topology changes
    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    group_id=f"aeos-worker-{node_id}", # ← unique per worker (intentional fan-out)
    auto_offset_reset="latest",
    enable_auto_commit=True,
    max_poll_records=100,
)
```

This is the canonical separation. All task topics use `group_id="aeos-workers"`. All broadcast topics use `group_id=f"aeos-worker-{node_id}"`.

**7.2.2 Offset commit strategy (corrected — commit AFTER checkpoint, not AFTER slot acquisition):**

v1.0 committed Kafka offsets immediately after acquiring a WorkerPool slot, before the step was executed or checkpointed. This created a window where a worker crash after offset commit but before checkpoint write would permanently lose the task (offset committed → Kafka won't redeliver; no checkpoint → Redis has no record; orphan scanner cannot detect it).

v1.1 commits Kafka offsets only after the two-phase checkpoint protocol completes (see §7.3). This preserves at-least-once delivery semantics: a crashed worker causes Kafka to redeliver the task after `session_timeout_ms`.

```
CORRECTED scheduler loop:

loop:
  1. Poll Kafka partitions (assigned to this worker, shared group "aeos-workers")
     batch_size=50, timeout=100 ms
  2. For each message in batch:
     a. Deserialize task
     b. Validate governance token (see §7.2.3 for expiry handling)
     c. Check step idempotency key in Redis:
          IF {wf:<wf_id>}:step:<step_id>:idem EXISTS:
            → return stored result (skip execution)
            → commit offset for this message
            → continue to next message
     d. Acquire execution lease (Redis SETNX):
          SETNX {wf:<wf_id>}:step:<step_id>:lease worker_node_id EX 120
          IF SETNX returns 0: another worker has the lease → skip, commit offset
          IF SETNX returns 1: this worker owns the step
     e. Set step status to "accepted" in Redis:
          SET {wf:<wf_id>}:step:<step_id>:status "accepted" EX 300
     f. Acquire a slot in the WorkerPool semaphore
     g. Submit to asyncio task (non-blocking)
     # DO NOT commit Kafka offset yet — offset is committed in §7.3 after checkpoint
  3. On WorkerPool saturation: pause polling (backpressure)
  4. On governance token invalid/expired: see §7.2.3
```

**7.2.3 Governance token handling on expiry (CB-6 fix):**

If a task's governance token has expired (task sat in Kafka queue longer than the token's `exp` claim), the worker does not silently drop the task to the DLQ. Instead:

```
Governance token expiry handling:
  1. Worker detects token.exp < now()
  2. Worker calls Policy Service: Re-EvaluateTask(task_id, original_context)
  3. IF Policy Service returns APPROVED:
       - New token issued with exp = now() + 3600s
       - Task proceeds to execution with new token
       - Original task message is "superseded" in Kafka (offset committed)
  4. IF Policy Service returns REJECTED:
       - Task sent to DLQ with rejection reason "re_evaluated_rejected"
       - Caller may query the DLQ via API
  5. IF Policy Service is unreachable (timeout):
       - Task is NOT dropped and NOT auto-approved
       - Task is re-published to same priority Kafka topic with retry_attempt++
       - If retry_attempt > 3: escalate to DLQ with reason "policy_service_unavailable"
       - Caller receives a 503 on next status check

Governance token expiry prevention:
  At submission time, the Policy Service issues tokens with expiry =
  max(task.deadline_ms + estimated_queue_wait_ms, 3600 * 1000) / 1000 seconds.

  estimated_queue_wait_ms = (current_queue_depth / cluster_throughput_steps_per_second) * 1000
  This estimate is provided by the Cluster Manager's admission control response.
```

**7.2.4 Work stealing.** Unchanged from v1.0 (partition-level rebalancing via Kafka group coordinator).

### 7.3 Execution Protocol

*Updated: Two-phase checkpoint (HP-3). Execution lease for split-brain safety (HP-8). Offset committed after checkpoint.*

```
CORRECTED execution protocol:

1.  Load checkpoint (if {wf:<wf_id>}:latest_checkpoint exists in Redis)
2.  Acquire execution lease (done in scheduler loop §7.2.2, step d)
3.  Check idempotency key — if already executed, return stored result and commit offset
4.  Update step status: SET {wf:<wf_id>}:step:<step_id>:status "executing" EX 300
5.  Update workflow heartbeat: SET {wf:<wf_id>}:last_heartbeat <unix_ts> EX 120
6.  Emit TASK_STARTED event to Kafka aeos.events.node (partition key = workflow_id)
7.  Execute step via DispatchingExecutor

8.  On success:
     a. ── Phase 1 of two-phase checkpoint ──
        Using MULTI/EXEC on same-slot keys (hashtag {wf:<wf_id>}):
          SET {wf:<wf_id>}:step:<step_id>:result  <serialized StepResult>  EX 86400
          SET {wf:<wf_id>}:step:<step_id>:status  "completed"              EX 86400
          SET {wf:<wf_id>}:step:<step_id>:idem    <serialized StepResult>  EX 86400
          SET {wf:<wf_id>}:checkpoint:<seq>        <checkpoint JSON>        EX 86400
          SET {wf:<wf_id>}:latest_checkpoint       <seq>                   EX 86400
          # next_published NOT yet set — marks Phase 1 incomplete
        EXEC
     b. Emit TASK_COMPLETED event to Kafka
     c. Determine next steps from ExecutionGraph (read {wf:<wf_id>}:graph)
     d. Publish next-step tasks to Kafka (aeos.tasks.{priority})
     e. ── Phase 2 of two-phase checkpoint ──
        SET {wf:<wf_id>}:step:<step_id}:next_published "true" EX 86400
     f. ── Now safe to commit Kafka offset ──
        consumer.commit({topic: partition: offset})
     g. Release execution lease (lease TTL will expire; optionally DEL early)

9.  On failure (executor raised exception):
     a. Apply RetryPolicy (backoff delay from retry.py)
     b. If retries_remaining > 0:
          - Re-publish task to same Kafka topic with retry_attempt++
          - SET {wf:<wf_id>}:step:<step_id}:status "retrying" EX 300
          - Commit original Kafka offset (task was re-published, original is superseded)
     c. If retries exhausted:
          - Emit TASK_FAILED event
          - Publish to aeos.tasks.dlq
          - SET {wf:<wf_id>}:step:<step_id>:status "failed" EX 86400
          - Commit Kafka offset
     d. If circuit breaker opens: emit CIRCUIT_OPEN event, pause agent routing

10. On workflow completion:
     a. Write final WorkflowState to PostgreSQL (via Kafka aeos.episodic)
     b. Emit WORKFLOW_COMPLETED event
     c. Schedule Redis key TTL refresh (default 24h)
```

### 7.4 Distributed Graph Execution

*Updated: MergeNode timeout defined (HP-4 fix). Step-execution lease prevents split-brain double execution (HP-8 fix).*

**7.4.1 Parallel step dispatch:**

Unchanged from v1.0 except: The MergeNode no longer polls indefinitely.

**MergeNode timeout behavior (now fully specified):**

```
MergeNode(
    input_node_ids: list[str],
    join_strategy: str = "all",     # "all" | "first" | "best_quality"
    fallback_to_partial: bool = False,
    timeout_s: float = 120.0,       # NEW required field with documented default
    on_timeout: str = "fail",       # "fail" | "partial_results" (only if fallback_to_partial=True)
)

MergeNode polling algorithm:
  deadline = time.monotonic() + timeout_s
  while time.monotonic() < deadline:
    completed = [n for n in input_node_ids
                 if redis.exists(f"{{wf:{workflow_id}}}:step:{n}:idem")]
    if len(completed) == len(input_node_ids):
      return aggregate_results(completed)
    if join_strategy == "first" and len(completed) >= 1:
      return aggregate_results([completed[0]])
    await asyncio.sleep(min(2.0, deadline - time.monotonic()))

  # Timeout reached:
  if on_timeout == "fail":
    # Cancel all in-flight parallel steps:
    for node_id in input_node_ids:
      if not redis.exists(f"{{wf:{workflow_id}}}:step:{node_id}:idem"):
        redis.set(f"{{wf:{workflow_id}}}:step:{node_id}:status", "cancelled", ex=300)
    emit MERGE_TIMEOUT event
    raise MergeTimeoutError(f"MergeNode timed out after {timeout_s}s")
  elif on_timeout == "partial_results" and fallback_to_partial:
    return aggregate_results(completed, partial=True)
```

**7.4.2 Cross-worker data flow.** Unchanged from v1.0 (step results stored in Redis under hashtag keys).

**7.4.3 Graph garbage collection.** Unchanged from v1.0.

---

## 8. Distributed Memory

### 8.1 Memory Fabric Architecture

Unchanged from v1.0.

### 8.2 Working Memory (Redis Cluster — cluster mode enabled)

*Updated: Redis mode is definitively Redis Cluster (cluster mode enabled), not Sentinel (CB-3). All keys use hashtags for MULTI/EXEC atomicity (CB-4).*

**8.2.1 Key schema (corrected — hashtags throughout):**

```
{wm:<session_id>}:<key>        → Value (string, JSON, or binary)
{wm:<session_id>}:__meta       → JSON: {created_at, last_access, ttl_s, owner_worker}
{wm:<session_id>}:__keys       → Redis Set: all user-defined keys in this session

All keys for a session share the hashtag {wm:<session_id>} → same Redis slot →
MULTI/EXEC transactions are valid across all session keys.
```

**8.2.2 Access patterns:** Unchanged from v1.0 (GET, SET with EX, MGET, DEL+SREM, Lua for clear_session).

**8.2.3 Consistency guarantees (corrected):**

Redis Cluster with cluster mode enabled (ElastiCache cluster mode on) provides:
- `min-replicas-to-write 1`: primary waits for 1 replica ACK before confirming write
- Hashtag keys guarantee all session keys are on the same shard → MULTI/EXEC is valid
- `WAIT 1 0` for critical reads: blocks until at least 1 replica confirms write propagation

Redis Cluster mode does NOT provide ACID transactions in the traditional database sense. It provides:
- Atomic execution of MULTI/EXEC for keys on the same slot (guaranteed by hashtags)
- No isolation between MULTI/EXEC blocks (other clients can read between blocks)
- Durability: dependent on AOF/RDB persistence configuration (set `appendonly yes`, `appendfsync everysec`)

The spec no longer claims full ACID semantics for Redis. It claims: **per-slot atomic batch execution via MULTI/EXEC, with synchronous replica confirmation via WAIT 1 0 for critical reads.**

**8.2.4 Session ownership and migration.** Unchanged from v1.0.

### 8.3 — 8.5

**8.3.2 MemoryEntry schema update — access tracking removed from hot path:**

The `access_count` and `last_accessed` fields are removed from the per-read update path (previously updated on every read, creating O(reads) writes). They are now updated by a background aggregation job running every 5 minutes.

```python
@dataclass
class MemoryEntry:
    entry_id: str
    session_id: str
    agent_id: str
    content: str
    embedding: list[float]
    metadata: dict
    created_at: datetime
    # access_count and last_accessed are updated in batch by the access-tracker job
    # Do not update on individual reads
```

**8.4.3 Episodic read-after-write consistency (explicitly documented):**

Episodic writes are eventually consistent. An episode written by an agent may not be immediately visible to the same agent reading it back. The staleness window is the Kafka-to-PostgreSQL pipeline latency (typically 1–10 seconds under normal load, up to 60 seconds under high load).

**Consequence:** Agents must not assume read-after-write consistency for episodic memory within the same workflow. Context enrichment from episodic memory should use a session-scoped cache (Working Memory) for data written in the current session.

For workflows where episodic read-after-write consistency is critical, use the synchronous write path:
```
POST /api/v1/memory/episodic/write/sync
```
This bypasses Kafka and writes directly to PostgreSQL (p99 < 50 ms, but blocks the calling step).

All other sections of §8 unchanged from v1.0.

---

## 9. Event Fabric

### 9.1 — 9.1

Unchanged from v1.0.

### 9.2 Topic Architecture

*Updated: Task topic partition count changed to 200 (CB-7 fix). `aeos.logs` topic added (referenced in §14.4).*

```
Topic configuration:

| Topic Group        | Partitions | Replication | Min ISR | Retention | Cleanup |
|--------------------|-----------|-------------|---------|-----------|---------|
| aeos.tasks.*       | 200        | 3           | 2       | 24h       | delete  |
| aeos.events.*      | 10         | 3           | 2       | 7d        | delete  |
| aeos.traces        | 200        | 3           | 2       | 7d        | delete  |
| aeos.episodic      | 5          | 3           | 2       | 7d        | delete  |
| aeos.metrics       | 10         | 2           | 1       | 1h        | delete  |
| aeos.audit         | 5          | 3           | 2       | 90d       | delete  |
| aeos.logs          | 10         | 2           | 1       | 7d        | delete  |

Partition count reasoning:
  aeos.tasks.*: 200 partitions supports up to 100 workers (2 partitions per worker average)
    with headroom for uneven distribution. Partition count must not be decreased after creation;
    it may only be increased (using kafka-topics.sh --alter) with a controlled rebalance.

IMPORTANT: Topic partition count must be provisioned at cluster creation time.
  The KEDA ScaledObject (§15.4) uses aeos.tasks.normal consumer lag as the primary scale metric.
  When adding worker capacity beyond 100 nodes, increase partition count first, then scale workers.
```

### 9.3 Event Schema

*Updated: `sequence_nanos` field added for cross-topic temporal ordering (MI-7 fix).*

```python
@dataclass
class DistributedEvent:
    # Envelope
    event_id: str           # UUID4
    event_type: str         # "WORKFLOW_STARTED" | "NODE_COMPLETED" | ...
    topic: str
    partition_key: str      # workflow_id | session_id | node_id
    published_at: str       # ISO 8601 UTC
    sequence_nanos: int     # ADDED: monotonic nanosecond timestamp from time.monotonic_ns()
                            # Use for cross-topic temporal ordering in replay/dashboards
    producer_node_id: str
    schema_version: str     # "1.1" ← updated from "1.0"
    trace_id: str
    span_id: str
    payload: dict
```

Consumers that join multiple topics (e.g., the Grafana bridge, the replay aggregator) sort events by `sequence_nanos` to reconstruct temporal ordering across topic boundaries.

### 9.4 DistributedEventBus Implementation

*Updated: Explicit separation of task consumer (shared group) vs event consumer (per-worker group). Full configuration corrected (CB-1 fix).*

See §7.2.1 for the corrected consumer group configurations. The `DistributedEventBus.subscribe()` method accepts a `group_mode` parameter:

```python
class DistributedEventBus:
    async def subscribe(
        self,
        event_type: str | list[str],
        handler: Callable,
        *,
        group_mode: Literal["broadcast", "competing"] = "broadcast",
    ) -> None:
        """
        group_mode="broadcast": every worker receives every event (fan-out).
          Uses group_id = f"aeos-worker-{self._node_id}"
          Used for: governance events, cluster topology, policy hot-reload.

        group_mode="competing": exactly one worker receives each message (work queue).
          Uses group_id = "aeos-workers"
          Used for: task execution topics.
          Note: WorkerPool uses this internally; application code should not subscribe
          to aeos.tasks.* directly.
        """
```

### 9.5 Event Ordering Guarantees

*Updated: Cross-topic ordering mechanism specified (MI-7 fix).*

**Within-topic ordering:** Guaranteed by Kafka partition ordering (partition key = workflow_id for workflow events, session_id for memory events, node_id for cluster events).

**Cross-topic ordering:** Not guaranteed by Kafka. Consumers that need to merge events from multiple topics must sort by `sequence_nanos` (§9.3). For the Grafana Workflow Detail dashboard, the trace aggregator service sorts merged events by `sequence_nanos` before rendering the Gantt chart.

### 9.6

Unchanged from v1.0.

---

## 10. Resource Management

### 10.4 LLM Call Rate Limiting (corrected — cluster-wide limiter)

*Updated: Per-worker token bucket is now initialized from a cluster-wide Redis counter, not from static config.*

```python
class ClusterWideLLMRateLimiter:
    """
    Cluster-wide rate limiting via Redis sliding window.
    Each worker contributes to a shared counter.
    """
    def __init__(self, provider: str, max_rpm: int, max_tpm: int):
        self._key_rpm = f"{{llm_rl:{provider}}}:rpm"
        self._key_tpm = f"{{llm_rl:{provider}}}:tpm"
        self._max_rpm = max_rpm
        self._max_tpm = max_tpm

    async def acquire(self, estimated_tokens: int) -> None:
        # Atomic sliding-window check-and-increment via Lua script
        # Script: check current count, if below limit increment, else raise
        result = await self._redis.eval(
            _RATE_LIMIT_LUA,
            2,
            self._key_rpm, self._key_tpm,
            self._max_rpm, self._max_tpm,
            estimated_tokens,
            60,   # RPM window: 60 seconds
        )
        if not result:
            raise RateLimitExceededError(f"LLM rate limit exceeded for {self._provider}")

_RATE_LIMIT_LUA = """
local rpm_key, tpm_key = KEYS[1], KEYS[2]
local max_rpm, max_tpm = tonumber(ARGV[1]), tonumber(ARGV[2])
local tokens = tonumber(ARGV[3])
local window = tonumber(ARGV[4])
local now = redis.call('TIME')[1]

local cur_rpm = tonumber(redis.call('INCR', rpm_key))
if cur_rpm == 1 then redis.call('EXPIRE', rpm_key, window) end
if cur_rpm > max_rpm then
  redis.call('DECR', rpm_key)
  return 0
end

local cur_tpm = tonumber(redis.call('INCRBY', tpm_key, tokens))
if cur_tpm == tokens then redis.call('EXPIRE', tpm_key, window) end
if cur_tpm > max_tpm then
  redis.call('DECRBY', tpm_key, tokens)
  redis.call('DECR', rpm_key)
  return 0
end
return 1
"""
```

All other §10 content unchanged from v1.0.

---

## 11. Capability Federation

### 11.4 Capability Health and Circuit Breaking

*Updated: Circuit breaker state names aligned with Phase 8.3 (`CLOSED/OPEN/HALF_OPEN`) to eliminate duality (v1.0 used `DEGRADED` in the Registry while Phase 8.3 used `CLOSED/OPEN/HALF_OPEN`).*

The Capability Registry tracks health using the same state machine as Phase 8.3's `CircuitBreaker`:

```
States: CLOSED | OPEN | HALF_OPEN

CLOSED (healthy):
  - Capability is fully routable
  - On TASK_FAILED events: increment failure counter
  - If failure_count > threshold within window: transition to OPEN

OPEN (degraded):
  - No new tasks routed to this capability on this node
  - After reset_timeout (default 60s): transition to HALF_OPEN

HALF_OPEN (probing):
  - One probe task routed to this capability
  - If probe succeeds: transition to CLOSED, reset failure counter
  - If probe fails: transition to OPEN, extend reset_timeout (×2, max 600s)
```

The `LookupCapability` response includes a `circuit_state` field. The Distributed Scheduler filters out `OPEN` capabilities and prefers `CLOSED` over `HALF_OPEN`.

All other §11 content unchanged from v1.0.

---

## 12. Security Architecture

### 12.2 mTLS — Three-Layer PKI Hierarchy

*Updated: Intermediate CA layer added (HP-10 fix). Root CA is now offline.*

```
Three-layer PKI (corrected from v1.0 two-layer):

Root CA:
  - Self-signed, 20-year validity
  - Stored OFFLINE in a hardware HSM (not in Vault)
  - NEVER used directly to issue leaf certificates
  - Used ONLY to sign the Intermediate CA
  - Backup: M-of-N secret sharing, stored in physical safes

Intermediate CA:
  - Signed by Root CA, 2-year validity
  - Stored in HashiCorp Vault PKI secrets engine
  - Path: pki_int/  (separate from Root CA mount)
  - Issues all node and service certificates
  - Can be revoked independently without rotating Root CA
  - CRL updated every 6 hours

Node certificates (leaf):
  - Signed by Intermediate CA via Vault pki_int/issue/aeos-node
  - 24-hour validity
  - SANs: node_id, hostname, k8s service DNS name
  - Automatic rotation by cert-manager at 50% lifetime (12 hours)

Service certificates (leaf):
  - Signed by Intermediate CA via Vault pki_int/issue/aeos-service
  - 72-hour validity
  - Managed by cert-manager
  - Stored as Kubernetes Secrets (not ConfigMaps)
```

### 12.3 Authentication

*Updated: Governance token expiry policy (CB-6 fix).*

**Governance tokens:** Governance tokens are issued with dynamic expiry calculated at submission time:

```
token_expiry_s = max(
    task.deadline_ms / 1000 + estimated_queue_wait_s + 300,   # deadline + queue + buffer
    3600,                                                        # minimum 1 hour
    86400,                                                       # maximum 24 hours (Kafka retention)
)
```

`estimated_queue_wait_s` is provided by the Cluster Manager's admission control response (§10.2). If the estimate is unavailable, `token_expiry_s = 3600` (conservative minimum).

Workers handling expired tokens follow the re-evaluation protocol in §7.2.3 rather than silently DLQ-ing the task.

All other §12.3 content unchanged from v1.0.

### 12.4 Authorization (RBAC)

*Updated: Immediate role revocation via Kafka pub/sub (MI-6 fix).*

Role revocations are propagated immediately (< 1 second) rather than waiting for the 5-minute Redis cache TTL:

```
On POST /api/v1/admin/rbac/revoke:
  1. Update rbac_assignments in PostgreSQL (synchronous)
  2. Publish RBAC_REVOKED event to aeos.events.cluster:
       { actor: "user:alice@example.com", action: "role_revoked", role: "operator" }
  3. All API gateway instances (event consumer, per-gateway group) receive event
  4. Each gateway immediately deletes Redis cache entry for affected user
  5. Next request from affected user: Redis miss → fresh DB lookup → revocation enforced
  Maximum propagation time: Kafka delivery (~100ms) + cache invalidation (~10ms) = <1 second
```

### 12.5 Secrets Management

*Updated: LLM API key rotation clarified (deferred to NTH but process documented).*

LLM API key rotation is manual but follows a documented checklist:
1. Create new key on provider dashboard
2. `vault kv put secret/aeos/llm/{provider} api_key=<new_key>`
3. Trigger rolling restart: `kubectl rollout restart deployment/aeos-worker`
4. Wait for rollout completion: `kubectl rollout status deployment/aeos-worker`
5. Verify no errors in worker logs for 5 minutes
6. Delete old key on provider dashboard

Automation via scheduled GitHub Actions workflow is a Nice-to-Have tracked as NTH-4.

All other §12 content unchanged from v1.0.

---

## 13. Governance & Policy Engine

### 13.3 Policy Evaluation Algorithm (corrected — fail-closed)

*Critical change (CB-5): The governance gate is now fail-closed. All fail-open paths removed.*

```
evaluate_task(task):
  1. Load all enabled policies, sorted by priority (lowest number = highest priority)
  2. Evaluate conditions top-to-bottom
  3. IF a REJECT policy matches:
       → REJECT with policy reason + policy_id
       → Log to aeos.audit (immutable)
       → Return REJECTED (do not continue evaluating lower-priority policies)
  4. IF an ESCALATE policy matches:
       → Create escalation record in PostgreSQL
       → Log to aeos.audit
       → Return PENDING_APPROVAL (see §13.5)
  5. IF an explicit APPROVE policy matches:
       → Sign governance token (JWT RS256)
       → Log to aeos.audit
       → Return APPROVED with signed token
  6. IF no policy matches:
       → REJECT with reason "no_policy_matched"
       → Log to aeos.audit with reason
       → Return REJECTED

  Timeout behavior (CORRECTED from v1.0):
    Budget: 50 ms (increased from 30 ms to allow policy DB round-trip)
    If evaluation exceeds 50 ms:
      → Return REJECTED with reason "policy_evaluation_timeout"
      → Log to aeos.audit with DEGRADED flag
      → The caller receives a 503 Service Unavailable with Retry-After: 5
      → The caller is responsible for retry
    THERE IS NO FAIL-OPEN PATH. A timeout is a rejection, not an approval.

Default policy (must be seeded in every deployment):
  Policy "default-approve-standard-tasks" should be deployed as an explicit catch-all
  with priority=9999 (lowest priority) if the deployment intends to approve all tasks
  by default. This policy is explicit, auditable, and removable:

  {
    "policy_id": "default-approve-all",
    "priority": 9999,
    "scope": "global",
    "conditions": [],                 # matches everything not matched above
    "actions": [{"action": "APPROVE", "reason": "default policy"}],
    "enabled": true
  }

  Removing or disabling this policy tightens the governance posture without
  any engine change. A deployment with no catch-all policy rejects all tasks
  that don't match an explicit APPROVE rule.
```

### 13.5 Human Escalation

*Updated: Leader failover during escalation handled.*

When the Cluster Manager leader fails while an escalation is `PENDING_APPROVAL`:
1. The escalation record is in PostgreSQL (durable)
2. The new Cluster Manager leader, upon election, queries PostgreSQL for `status=PENDING_APPROVAL` escalations (as part of its startup recovery scan)
3. The new leader re-parks the escalated task in `aeos.tasks.escalated`
4. Human approval/rejection via API continues to work (it writes to PostgreSQL, not to the Cluster Manager)

Escalation timeout: 24 hours (configurable). Auto-rejected with reason `escalation_timeout`. The caller's SDK receives a webhook callback if registered.

All other §13 content unchanged from v1.0.

---

## 14. Observability Platform

### 14.2 Metrics

*Updated: All latency metrics are Histograms, not Summaries (MI-3 fix). Prometheus histogram format specified.*

```
# Latency metrics — all use Histogram format (supports aggregation across workers)
# Expose: _bucket, _sum, _count (not pre-computed {quantile="..."} label)
# Query with: histogram_quantile(0.99, rate(metric_bucket[5m]))

aeos_workflow_duration_seconds_bucket{le="0.1|0.5|1|2|5|10|30|60|+Inf"}
aeos_workflow_duration_seconds_sum
aeos_workflow_duration_seconds_count

aeos_step_duration_seconds_bucket{agent_type="...", le="0.01|0.05|0.1|0.5|1|2|5|10|+Inf"}
aeos_step_duration_seconds_sum{agent_type="..."}
aeos_step_duration_seconds_count{agent_type="..."}

aeos_governance_duration_seconds_bucket{le="0.005|0.01|0.025|0.05|0.1|0.25|0.5|1|+Inf"}
aeos_governance_duration_seconds_sum
aeos_governance_duration_seconds_count

# Counter metrics (unchanged)
aeos_workflow_submitted_total{status="accepted|rejected"}
aeos_workflow_completed_total{status="completed|failed|cancelled"}
aeos_step_retry_total{agent_type="...", attempt="1|2|3"}
aeos_agent_calls_total{agent_type="...", status="success|failure"}
aeos_agent_llm_tokens_total{agent_type="...", provider="openai|anthropic"}

# Gauge metrics (unchanged)
aeos_cluster_workers_active
aeos_cluster_workers_draining
aeos_cluster_capabilities_total{circuit_state="CLOSED|OPEN|HALF_OPEN"}
aeos_step_queue_depth{priority="critical|high|normal|low|batch"}
aeos_kafka_consumer_lag{topic="...", partition="..."}
```

All AlertManager rules reference the corrected Histogram metric names.

All other §14 content unchanged from v1.0.

---

## 15. Cloud Architecture

### 15.2 AWS Service Mapping

*Updated: KEDA added. Redis clarified as cluster mode. Redis Sentinel removed.*

| AEOS Component | AWS Service | Configuration |
|---------------|-------------|---------------|
| Worker nodes | EKS (EC2 nodegroup) | `c5.2xlarge`, spot + on-demand (min 3 on-demand) |
| Cluster Manager | EKS (EC2 StatefulSet) | `t3.large` (↑ from t3.medium for Raft under load) |
| Redis Cluster | ElastiCache for Redis | `r6g.large`, **cluster mode enabled**, 3 shards × 1 replica |
| Kafka | Amazon MSK | `kafka.m5.large`, 3 brokers, 3 AZs, **200 partitions** |
| PostgreSQL | RDS PostgreSQL 16 | `db.r6g.large`, Multi-AZ |
| Vector store | Weaviate on EKS | `r5.xlarge` nodes (see §15.2a for justification) |
| Object storage | S3 | Standard (hot), Glacier Instant Retrieval (archive) |
| Secrets | HashiCorp Vault on EKS | HA mode, 3 nodes (see §15.2b for justification) |
| Load balancer | AWS ALB (Ingress) | HTTPS, WAF-enabled |
| Certificate management | cert-manager + Vault PKI (Int CA) | Auto-rotate 24h |
| Container registry | ECR | Image scanning enabled |
| CI/CD | GitHub Actions → ECR → EKS | GitOps via ArgoCD |
| Autoscaling | **KEDA v2** (Kubernetes Event-driven Autoscaler) | **NEW** — required for Kafka-based HPA |

**§15.2a: Weaviate self-hosted justification:**  
AWS OpenSearch Serverless with k-NN is a valid alternative ($0 idle cost vs $720/month for `r5.xlarge` ×3). The decision to use self-hosted Weaviate is justified by: (1) Phase 10 multi-cloud requirement (Weaviate supports GCP/Azure without re-implementation); (2) Weaviate's native multi-tenancy supports the planned Phase 10 multi-tenant isolation. Teams prioritizing cost over future portability should substitute AWS OpenSearch; the `LongTermMemoryStore` ABC makes this substitution possible without changing application code.

**§15.2b: Vault self-hosted justification:**  
AWS Secrets Manager + ACM Private CA would reduce operational complexity. The decision to use Vault is justified by: (1) Dynamic database credentials (1-hour TTL, zero-touch rotation) for PostgreSQL — not available via Secrets Manager without custom Lambda; (2) Three-layer PKI (Root + Intermediate + Leaf) is natively supported by Vault PKI, whereas ACM Private CA requires additional configuration; (3) Phase 10 multi-cloud portability. Teams should evaluate whether these justifications hold for their deployment.

### 15.3 VPC Architecture

*Updated: `sg-workers` inbound from `sg-alb` restricted to API port only, not port 8000 (MI-10 fix).*

```
Security Groups:
  sg-alb:         inbound 443 from 0.0.0.0/0
  sg-workers:     inbound 50051 from sg-workers (gRPC mesh, mTLS)
                  inbound 8000 from sg-monitoring ONLY  ← CHANGED (not from sg-alb)
                  outbound to sg-data (Redis, Kafka, Postgres)
                  outbound to sg-platform (Cluster Manager, Policy, Registry)
  sg-monitoring:  inbound from sg-workers:9090 (Prometheus scrape target)
                  separate from sg-alb
  sg-data:        inbound 6379 from sg-workers (Redis)
                  inbound 9092 from sg-workers (Kafka)
                  inbound 5432 from sg-workers (PostgreSQL)
                  no outbound to public internet
  sg-platform:    inbound 50051-50054 from sg-workers (gRPC services)
                  inbound 50051-50054 from sg-alb (API gateway → platform)
```

### 15.4 Kubernetes Resource Manifests

*Updated: KEDA ScaledObject replaces HPA. PodAntiAffinity added. PodDisruptionBudgets added. NetworkPolicy objects added.*

**Worker Deployment spec:**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: aeos-worker
  namespace: aeos
spec:
  replicas: 3
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  template:
    metadata:
      labels:
        app: aeos-worker
    spec:
      terminationGracePeriodSeconds: 60
      # ADDED: Pod anti-affinity to spread workers across AZs
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
            - weight: 100
              podAffinityTerm:
                topologyKey: topology.kubernetes.io/zone
                labelSelector:
                  matchLabels:
                    app: aeos-worker
      containers:
        - name: worker
          image: {ECR_REGISTRY}/aeos-worker:{VERSION}
          resources:
            requests:
              cpu: "2"
              memory: "4Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
          env:
            - name: AEOS_NODE_ROLE
              value: "worker"
            - name: AEOS_CLUSTER_MANAGER_ADDR
              value: "aeos-cluster-manager.aeos.svc.cluster.local:50052"
          readinessProbe:
            httpGet:
              path: /health/ready
              port: 8000
            initialDelaySeconds: 15
            periodSeconds: 5
          livenessProbe:
            httpGet:
              path: /health/live
              port: 8000
            initialDelaySeconds: 30
            periodSeconds: 10
          # ADDED: Volume mount for Raft persistent state (workers don't need this;
          # Cluster Manager StatefulSet has its own PVC — see below)
```

**KEDA ScaledObject (replaces v1.0 HPA spec):**

```yaml
# ADDED: KEDA must be installed in the cluster before this manifest is applied
# Install: helm install keda kedacore/keda --namespace keda --create-namespace

apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: aeos-worker-scaledobject
  namespace: aeos
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: aeos-worker
  minReplicaCount: 3
  maxReplicaCount: 100                 # ← matches 100-node NFR
  cooldownPeriod: 120                  # seconds before scale-down
  pollingInterval: 15                  # seconds between lag checks
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: "kafka.aeos.svc.cluster.local:9092"
        topic: aeos.tasks.normal
        consumerGroup: aeos-workers    # ← shared group ID (not per-worker)
        lagThreshold: "500"
        saslType: scram_sha512
      authenticationRef:
        name: aeos-kafka-trigger-auth
    - type: cpu
      metricType: Utilization
      metadata:
        value: "70"
```

**PodDisruptionBudgets (ADDED):**

```yaml
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-cluster-manager-pdb
  namespace: aeos
spec:
  minAvailable: 2     # Raft quorum requires 2 of 3 alive during drain
  selector:
    matchLabels:
      app: aeos-cluster-manager
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-capability-registry-pdb
  namespace: aeos
spec:
  minAvailable: 2     # Consistent hash ring requires 2 of 3 for quorum reads
  selector:
    matchLabels:
      app: aeos-capability-registry
---
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-worker-pdb
  namespace: aeos
spec:
  minAvailable: 2     # At least 2 workers always available
  selector:
    matchLabels:
      app: aeos-worker
```

**NetworkPolicy objects (ADDED):**

```yaml
---
# Workers: restrict egress to required services only
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: aeos-worker-network-policy
  namespace: aeos
spec:
  podSelector:
    matchLabels:
      app: aeos-worker
  policyTypes: [Ingress, Egress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: aeos-worker
      ports:
        - port: 50051     # Worker-to-worker gRPC
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: aeos-cluster-manager
      ports: [{port: 50051}]
    - to:
        - podSelector:
            matchLabels:
              app: aeos-policy-service
      ports: [{port: 50054}]
    - to:
        - podSelector:
            matchLabels:
              app: aeos-capability-registry
      ports: [{port: 50053}]
    - to:
        - namespaceSelector:
            matchLabels:
              name: kube-system
      ports: [{port: 53}, {protocol: UDP, port: 53}]  # DNS
    - to: [{ipBlock: {cidr: "10.0.20.0/22"}}]   # Private-Data subnet (Redis/Kafka/Postgres)
---
# Cluster Manager: only accessible from workers and API gateway
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: aeos-cluster-manager-network-policy
  namespace: aeos
spec:
  podSelector:
    matchLabels:
      app: aeos-cluster-manager
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: aeos-worker
        - podSelector:
            matchLabels:
              app: aeos-api-gateway
      ports: [{port: 50051}, {port: 50052}]
    - from:
        - podSelector:
            matchLabels:
              app: aeos-cluster-manager
      ports: [{port: 50052}]   # Raft peer-to-peer
```

### 15.5 Multi-Region Strategy

Unchanged from v1.0. Clarification: Spot instance minimum:

> **Spot instance floor:** At all times, at least 3 worker pods must run on on-demand instances (configured via nodegroup mixed instance policy: `on_demand_base_capacity = 3`). This ensures the cluster can function even during a regional spot sweep event.

---

## 16. Failure Analysis & Resilience

### 16.2 Worker Node Failure

*Updated: Orphan detection now catches steps that crashed after offset commit (CB-2 fix via step status markers).*

**Orphan detection (corrected):**

The v1.0 orphan scanner searched for Redis keys with `status=RUNNING`. v1.1 also scans for `status=accepted` and `status=executing` entries with stale heartbeats — these represent tasks that crashed after offset commit but before checkpoint write.

```python
class OrphanWorkflowScanner:
    """Background task on Cluster Manager leader. Runs every 15 seconds."""

    async def scan(self) -> None:
        dead_nodes = [m.node_id for m in self._membership if m.status == "dead"]

        # Pattern 1: Workflows in RUNNING status with stale heartbeat
        # (step was executing, worker died mid-step or between steps)
        for workflow_id in await self._redis.scan_keys("{wf:*}:status"):
            wf_id = extract_workflow_id(workflow_id)
            status = await self._redis.get(f"{{wf:{wf_id}}}:status")
            last_hb = await self._redis.get(f"{{wf:{wf_id}}}:last_heartbeat")
            if status == "running" and (time.time() - float(last_hb or 0)) > 30:
                await self._requeue_workflow(wf_id, reason="heartbeat_timeout")

        # Pattern 2: Steps with status=accepted or status=executing but no lease
        # (worker crashed after setting status but before acquiring lease, or lease expired)
        for step_key in await self._redis.scan_keys("{wf:*}:step:*:status"):
            step_status = await self._redis.get(step_key)
            if step_status in ("accepted", "executing"):
                wf_id, step_id = extract_wf_step_ids(step_key)
                lease = await self._redis.get(f"{{wf:{wf_id}}}:step:{step_id}:lease")
                if not lease:  # lease expired or was never set
                    await self._requeue_step(wf_id, step_id, reason="lease_expired")

        # Pattern 3: Steps completed but next_published is absent
        # (checkpoint written but Kafka publish of next step failed)
        for step_key in await self._redis.scan_keys("{wf:*}:step:*:status"):
            step_status = await self._redis.get(step_key)
            if step_status == "completed":
                wf_id, step_id = extract_wf_step_ids(step_key)
                next_pub = await self._redis.get(
                    f"{{wf:{wf_id}}}:step:{step_id}:next_published"
                )
                if not next_pub:  # Phase 2 of checkpoint never completed
                    await self._republish_next_steps(wf_id, step_id)
```

### 16.3 Redis Failure (corrected — Redis Cluster, not Sentinel)

*CB-3 fix: All Sentinel references removed. Redis Cluster (cluster mode enabled) failure behavior.*

**Redis Cluster node failure:**

ElastiCache cluster mode operates as a cluster with 3 shards. Each shard has 1 primary + 1 replica. If a shard's primary fails:
- ElastiCache automatically promotes the replica to primary
- Failover time: typically 15–30 seconds
- During failover: slot operations on the failed shard fail with `CLUSTERDOWN` or `MOVED` errors

**Worker behavior during Redis Cluster node failure:**

Workers detect Redis `CLUSTERDOWN` errors on their assigned shard and:
1. Pause checkpoint writes for workflows whose keys map to the failed shard
2. Continue executing steps (steps run; they just won't be checkpointed mid-execution)
3. Buffer checkpoint data in a local in-memory queue (max 50 checkpoints, bounded)
4. When the shard recovers (primary promoted), flush buffered checkpoints
5. If the shard is unavailable for > 60 seconds: pause accepting new tasks for affected workflows

**Note:** Unlike Redis Sentinel, Redis Cluster does not require Sentinel nodes. The ElastiCache cluster mode provides HA through its own cluster health monitoring. The `redis-py` and `redis.asyncio` clients support cluster mode natively with automatic slot routing and failover retry.

### 16.4 Kafka Failure

*Updated: DiskEventBuffer requires Redis backing, not local disk (HP-7 fix).*

If 2 Kafka brokers fail simultaneously (catastrophic, P(event) very low with MSK):
- Workers fall back to SQS for task dispatch (SQS fallback queue, always warm)
- Events and traces that cannot be published to Kafka are buffered in a **Redis-backed event buffer** (not local disk):

```python
class RedisEventBuffer:
    """
    Fallback event buffer using Redis List when Kafka is unavailable.
    Ring buffer: evicts oldest when max_events reached.
    Keys: {event_buf:<node_id>}:events (Redis List, LPUSH, LTRIM)
    """
    MAX_EVENTS = 100_000

    async def buffer(self, event: DistributedEvent) -> None:
        key = f"{{event_buf:{self._node_id}}}:events"
        async with self._redis.pipeline() as pipe:
            pipe.lpush(key, event.to_json())
            pipe.ltrim(key, 0, self.MAX_EVENTS - 1)
            pipe.expire(key, 3600)  # 1h TTL on the buffer
            await pipe.execute()

    async def flush_to_kafka(self) -> int:
        """Called when Kafka recovers. Returns number of events flushed."""
        key = f"{{event_buf:{self._node_id}}}:events"
        events = await self._redis.lrange(key, 0, -1)
        for event_json in reversed(events):  # oldest first
            await self._producer.send(...)
        await self._redis.delete(key)
        return len(events)
```

Using Redis instead of local disk means the buffer survives pod restarts (which occur during the same Kafka outage). Since Redis Cluster is on a separate failure domain from Kafka/MSK, Redis availability during an MSK broker failure is independent and highly likely.

### 16.5 Network Partition (Split Brain)

*Updated: Step execution lease prevents double-execution across partition boundaries (HP-8 fix).*

When the cluster is partitioned:

- Majority partition: has Raft quorum, continues operating, Cluster Manager responds
- Minority partition: cannot reach leader, stops accepting new workflows

**Double-execution prevention (corrected):**

In v1.0, workers in the minority partition could still read from Redis (Redis is not network-partitioned with the workers) and execute steps, while the majority partition's orphan scanner re-queued the same steps. This created concurrent double-execution.

In v1.1, the **execution lease** (§7.2.2, §7.3) prevents this:

```
SETNX {wf:<wf_id>}:step:<step_id>:lease worker_node_id EX 120
```

If a step is being executed by a minority-partition worker, the lease exists in Redis with that worker's `node_id`. When the orphan scanner (on the majority-partition Cluster Manager) triggers and a new worker tries to acquire the lease:
- `SETNX` returns 0 (lease already held)
- New worker skips this step
- Lease expires after 120 seconds (max LLM call duration should be < 120 seconds)
- After expiry, new worker acquires lease and retries the step

The minority-partition worker's result (if it completed) is stored in Redis under the idempotency key `{wf:<wf_id>}:step:<step_id}:idem`. When the new worker checks idempotency before executing, it finds the stored result and returns it without re-executing.

**Healing:** When the partition heals, minority-partition workers re-join via the normal join protocol. Their in-flight steps (if any) are either idempotent duplicates (handled by the idem key) or have leases that expire naturally.

### 16.6 — 16.7

Unchanged from v1.0.

---

## 17. Performance Engineering

### 17.2 LLM Response Caching (corrected — opt-in only)

*Updated: Cache is opt-in, not default. Cache keys must exclude workflow-specific context (MI-2 fix).*

LLM response caching is **opt-in only**. It is NOT enabled by default.

A prompt is cacheable only if:
1. It is explicitly marked `cacheable=True` in the LLM request
2. The prompt is fully deterministic (no session-specific context, no timestamps, no dynamic tool outputs)
3. `temperature=0.0` (deterministic output)

```python
@dataclass
class LLMRequest:
    model: str
    prompt: str
    temperature: float = 0.0
    max_tokens: int = 4096
    cacheable: bool = False   # Opt-in only. Default False.
    cache_ttl_s: int = 3600

# Cache key includes the full prompt content (not just model+temperature)
# to prevent cross-workflow data leakage:
def _cache_key(req: LLMRequest) -> str:
    return sha256(f"{req.model}||{req.temperature}||{req.max_tokens}||{req.prompt}".encode()).hexdigest()
```

The 15–20% cache hit rate estimate from v1.0 was overstated. Realistic cache hit rate for agent workflows (which include dynamic context) is 2–5%. The primary value of caching is for known-static prompts: code review prompts with fixed rubrics, classification prompts with fixed categories, format-conversion prompts.

### 17 (remainder)

Unchanged from v1.0.

---

## 18. Testing Strategy

### 18.2 Unit Testing

*Updated: Additional required test categories.*

| Category | What to Test |
|---------|-------------|
| Raft leader election | Term increments, vote counting, heartbeat timeout, split vote, **term persistence across restart** |
| Raft log replication | AppendEntries ordering, commit index advancement, log conflict resolution |
| Distributed Scheduler | **Shared group_id for task topics**, per-worker group for event topics, offset commit after checkpoint |
| Step execution lease | SETNX idempotency, lease expiry, second-worker SETNX rejection |
| Two-phase checkpoint | Phase 1 (result + status + idem), Phase 2 (next_published), recovery from Phase 1 partial failure |
| Redis checkpoint | Hashtag key co-location, MULTI/EXEC atomicity, eviction, TTL expiry |
| Circuit breaker | CLOSED→OPEN→HALF_OPEN state transitions, probe success/failure, alignment with Phase 8.3 states |
| Policy evaluation | **Fail-closed on no-match**, **fail-closed on timeout**, condition matching, ESCALATE branching |
| Governance token expiry | Re-evaluation on expired token, dynamic expiry calculation |
| mTLS certificate validation | Expired cert rejection, intermediate CA chain validation |
| Event ordering | `sequence_nanos` sort for cross-topic replay |
| MergeNode timeout | FAIL mode cancellation markers, PARTIAL_RESULTS mode |

### 18.4 End-to-End Tests

*Updated: Additional scenarios from v1.1 fixes.*

Additional E2E tests required:
- **Lease conflict:** Submit same task twice simultaneously → exactly one execution
- **Two-phase checkpoint failure:** Kill worker after Phase 1 but before Phase 2 → orphan scanner detects `next_published=absent` and re-publishes next steps
- **Consumer group isolation:** Confirm that a task published to `aeos.tasks.normal` is consumed by exactly one worker (not all workers)
- **Governance fail-closed:** Submit task when all Policy Service instances are down → task rejected with 503, not auto-approved
- **Governance token re-validation:** Publish task with pre-expired token → worker re-evaluates, task proceeds if still approved

All other §18 content unchanged from v1.0.

---

## 19. Migration Strategy

### 19.2 Migration Phases

*Updated: Phase M2 binary now defaults to 200 Kafka partitions and shared consumer group.*

All phases unchanged from v1.0. The Phase M2 binary implements the v1.1 consumer group separation and 200-partition Kafka topics from the first deployment. Backward compatibility is maintained via `AEOS_CLUSTER_MODE=false`.

### 19.4 Backward Compatibility Guarantees

*Added: Worker failure safety.*

**Worker failure safety:** A worker running v1.1 code can safely process checkpoints created by a v1.0 worker (no checkpoint format change). The hashtag key schema is additive — v1.0 workers write keys without hashtags; v1.1 workers write keys with hashtags. During migration phase M3 (single worker, both modes), the worker writes v1.1 hashtag keys only. There is no mixed-worker scenario (workers upgrade via rolling deploy, single worker at a time).

---

## 20. Implementation Roadmap

### 20.3 Milestone 9B-2: Cluster Manager (UPDATED)

*Updated: Redis-backed membership table removed. Raft log replication from day one.*

**Deliverables (corrected):**
- `aeos-cluster-manager` service (`app/cluster/manager.py`)
- Raft state machine with **persisted `current_term`, `voted_for`, `log`** to `/var/aeos/raft.json`
- Cluster Manager StatefulSet with PersistentVolumeClaim at `/var/aeos/`
- **Full Raft log replication** for membership events (not Redis-backed)
- Worker join/leave/failure detection protocol (with corrected sequencing: capabilities before partition assignment)
- gRPC API: `PreJoin`, `JoinCluster`, `JoinComplete`, `LeaveCluster`, `Heartbeat`, `GetTopology`, `WatchTopology`
- PodDisruptionBudget (minAvailable=2)
- PodAntiAffinity across zones
- Chaos tests: kill leader mid-replication, kill follower, network partition

**Success gate:** 3-node Cluster Manager cluster elects a leader within 5 seconds. Leader failure triggers re-election within 15 seconds. **A restarted Cluster Manager node recovers its term and voted_for from `/var/aeos/raft.json` without granting conflicting votes.**

### 20.4 Milestone 9B-3: Distributed Scheduler (UPDATED)

**Deliverables (corrected):**
- `DistributedScheduler` with **shared consumer group `aeos-workers` for task topics**
- **Separate per-worker consumer groups for broadcast event topics**
- Two-phase checkpoint protocol (Phase 1: result+status+idem; Phase 2: next_published)
- Execution lease (SETNX `{wf:{wf_id}}:step:{step_id}:lease`, TTL=120s)
- Step status markers (`accepted`, `executing`, `completed`, `failed`, `cancelled`)
- Idempotency key check before execution
- Governance token re-evaluation on expiry (§7.2.3)
- Orphan scanner (three patterns: heartbeat, lease, next_published)
- KEDA ScaledObject instead of HPA (KEDA must be installed as prerequisite)
- NetworkPolicy objects for workers

**Success gate:** 100 tasks submitted simultaneously → exactly 100 unique executions (not 100×N). Kill worker mid-execution → task retried exactly once on another worker.

### 20.7 Milestone 9B-6: Observability & Production Hardening (UPDATED)

**Additional deliverables:**
- KEDA installation (`helm install keda kedacore/keda`)
- All NetworkPolicy objects (workers, Cluster Manager, Policy Service, Registry)
- All PodDisruptionBudgets
- PodAntiAffinity on all StatefulSets and Deployments
- Histogram-based Prometheus metrics (replacing v1.0 Summary metrics)
- Alembic migration framework with initial schema migrations for all PostgreSQL tables
- Cluster Manager StatefulSet PVC manifest (`/var/aeos/`, `ReadWriteOnce`, `10Gi`)
- Three-layer PKI setup documentation (Root CA offline, Intermediate CA in Vault)
- `aeos-client` SDK stub (§Appendix D)

All other §20 content unchanged from v1.0.

---

## Appendix A: Protobuf Interface Definitions

### A.1 Cluster Manager gRPC

*Updated: Added PreJoin, JoinComplete, DrainResponse, LeaveResponse, TopologyRequest, WatchRequest, TopologyEvent. All message types now fully defined.*

```protobuf
syntax = "proto3";
package aeos.cluster.v1;

service ClusterManager {
  rpc PreJoin(PreJoinRequest) returns (PreJoinResponse);         // NEW
  rpc JoinCluster(JoinRequest) returns (JoinResponse);
  rpc JoinComplete(JoinCompleteRequest) returns (JoinCompleteResponse); // NEW
  rpc LeaveCluster(LeaveRequest) returns (LeaveResponse);
  rpc DrainNode(DrainRequest) returns (DrainResponse);
  rpc Heartbeat(HeartbeatRequest) returns (HeartbeatResponse);
  rpc GetTopology(TopologyRequest) returns (TopologyResponse);
  rpc WatchTopology(WatchRequest) returns (stream TopologyEvent);
}

message PreJoinRequest {
  string node_id = 1;
  string version = 2;
  string zone = 3;
}

message PreJoinResponse {
  bool accepted = 1;
  string rejection_reason = 2;
  string cluster_version = 3;
  TopologySnapshot topology = 4;
}

message JoinRequest {
  string node_id = 1;
  string hostname = 2;
  string grpc_address = 3;
  string http_address = 4;
  string role = 5;
  repeated CapabilitySummary capabilities = 6;
  string version = 7;
  string zone = 8;
}

message CapabilitySummary {
  string capability_id = 1;
  string agent_type = 2;
  int32 max_concurrent = 3;
}

message JoinResponse {
  bool accepted = 1;
  string rejection_reason = 2;
  repeated int32 assigned_kafka_partitions = 3;
  TopologySnapshot topology = 4;
}

message JoinCompleteRequest {
  string node_id = 1;
}

message JoinCompleteResponse {
  bool acknowledged = 1;
}

message LeaveRequest {
  string node_id = 1;
}

message LeaveResponse {
  bool acknowledged = 1;
}

message DrainRequest {
  string node_id = 1;
}

message DrainResponse {
  bool acknowledged = 1;
  int32 in_flight_steps = 2;   // Steps still executing at drain time
}

message HeartbeatRequest {
  string node_id = 1;
  NodeLoad load = 2;
}

message HeartbeatResponse {
  bool acknowledged = 1;
  string leader_node_id = 2;   // Workers redirect if they sent to a non-leader
}

message NodeLoad {
  int32 active_steps = 1;
  int32 queued_steps = 2;
  float cpu_utilization = 3;
  float memory_utilization = 4;
  int32 llm_calls_in_flight = 5;
}

message TopologyRequest {}

message TopologyResponse {
  TopologySnapshot snapshot = 1;
}

message WatchRequest {
  string watcher_node_id = 1;
}

message TopologyEvent {
  string event_type = 1;        // "MEMBER_JOINED" | "MEMBER_LEFT" | "MEMBER_SUSPECTED" | "MEMBER_DEAD"
  ClusterMemberProto member = 2;
  int64 cluster_version = 3;
}

message TopologySnapshot {
  repeated ClusterMemberProto members = 1;
  string leader_node_id = 2;
  int64 cluster_version = 3;
}

message ClusterMemberProto {
  string node_id = 1;
  string hostname = 2;
  string grpc_address = 3;
  string http_address = 4;
  string role = 5;
  repeated string capabilities = 6;
  int64 joined_at_unix = 7;
  int64 last_heartbeat_unix = 8;
  string status = 9;    // "joining" | "active" | "draining" | "suspected" | "dead"
  string zone = 10;
  string version = 11;
}
```

### A.2 Capability Registry gRPC

*Updated: All request/response messages now fully defined.*

```protobuf
syntax = "proto3";
package aeos.registry.v1;

service CapabilityRegistry {
  rpc AdvertiseCapabilities(AdvertiseRequest) returns (AdvertiseResponse);
  rpc WithdrawCapabilities(WithdrawRequest) returns (WithdrawResponse);
  rpc LookupCapability(LookupRequest) returns (LookupResponse);
  rpc UpdateLoad(UpdateLoadRequest) returns (UpdateLoadResponse);
  rpc WatchCapabilities(WatchRequest) returns (stream CapabilityChange);
}

message AdvertiseRequest {
  string node_id = 1;
  string grpc_address = 2;
  string zone = 3;
  repeated CapabilityInfo capabilities = 4;
}

message AdvertiseResponse {
  bool accepted = 1;
  string rejection_reason = 2;
}

message WithdrawRequest {
  string node_id = 1;
  repeated string capability_ids = 2;   // empty = withdraw all
}

message WithdrawResponse {
  bool acknowledged = 1;
  int32 withdrawn_count = 2;
}

message CapabilityInfo {
  string capability_id = 1;
  string agent_type = 2;
  repeated string tags = 3;
  int32 max_concurrent = 4;
  int32 current_load = 5;
  float quality_score = 6;
  string circuit_state = 7;   // "CLOSED" | "OPEN" | "HALF_OPEN"
}

message LookupRequest {
  string capability_id = 1;
  repeated string tags_required = 2;
  string strategy = 3;          // "LEAST_LOADED" | "ROUND_ROBIN" | "PINNED" | "ZONE_AFFINITY" | "QUALITY_WEIGHTED"
  string preferred_zone = 4;
  int32 top_k = 5;
}

message LookupResponse {
  repeated CapabilityRoute routes = 1;
}

message CapabilityRoute {
  string node_id = 1;
  string grpc_address = 2;
  int32 current_load = 3;
  float quality_score = 4;
  string zone = 5;
  string circuit_state = 6;
}

message UpdateLoadRequest {
  string node_id = 1;
  string capability_id = 2;
  int32 current_load = 3;
  string circuit_state = 4;
}

message UpdateLoadResponse {
  bool acknowledged = 1;
}

message WatchRequest {
  string watcher_id = 1;
  repeated string capability_ids = 2;   // empty = watch all
}

message CapabilityChange {
  string change_type = 1;   // "ADDED" | "REMOVED" | "LOAD_UPDATED" | "CIRCUIT_CHANGED"
  string node_id = 2;
  CapabilityInfo capability = 3;
}
```

### A.3 Policy Service gRPC

*Updated: `EvaluateStep` RPC added (was in §13.2 but missing from Appendix A.3 in v1.0). All message types defined.*

```protobuf
syntax = "proto3";
package aeos.policy.v1;

service PolicyService {
  rpc EvaluateTask(TaskEvaluationRequest) returns (TaskEvaluationResponse);
  rpc ReEvaluateTask(TaskEvaluationRequest) returns (TaskEvaluationResponse);  // NEW: for expired tokens
  rpc EvaluateStep(StepEvaluationRequest) returns (StepEvaluationResponse);   // ADDED (was missing in v1.0)
  rpc AuditStep(StepAuditRequest) returns (AuditResponse);
  rpc GetPolicy(GetPolicyRequest) returns (Policy);
  rpc UpdatePolicy(UpdatePolicyRequest) returns (Policy);
  rpc ListPolicies(ListPoliciesRequest) returns (stream Policy);
  rpc ApproveEscalation(EscalationDecisionRequest) returns (EscalationDecisionResponse);  // NEW
  rpc RejectEscalation(EscalationDecisionRequest) returns (EscalationDecisionResponse);   // NEW
}

message TaskEvaluationRequest {
  string task_id = 1;
  string workflow_id = 2;
  string submitter = 3;
  string task_description = 4;
  string task_type = 5;
  int64 deadline_ms = 6;
  int64 estimated_queue_wait_ms = 7;   // NEW: used for dynamic token expiry
  map<string, string> metadata = 8;
}

message TaskEvaluationResponse {
  string decision = 1;           // "APPROVED" | "REJECTED" | "PENDING_APPROVAL"
  string reason = 2;
  string governance_token = 3;   // Signed JWT (empty if not APPROVED)
  string policy_id = 4;          // Which policy matched
  string policy_version = 5;
  string risk_level = 6;         // "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
  int64 token_expires_at_unix = 7;  // NEW: when the issued token expires
}

message StepEvaluationRequest {
  string step_id = 1;
  string workflow_id = 2;
  string agent_type = 3;
  string governance_token = 4;   // Token from task evaluation
  map<string, string> step_context = 5;
}

message StepEvaluationResponse {
  string decision = 1;           // "APPROVED" | "REJECTED"
  string reason = 2;
}

message StepAuditRequest {
  string step_id = 1;
  string workflow_id = 2;
  string agent_type = 3;
  string outcome = 4;            // "completed" | "failed" | "cancelled"
  int64 duration_ms = 5;
  int64 tokens_used = 6;
  map<string, string> metadata = 7;
}

message AuditResponse {
  bool accepted = 1;
}

message EscalationDecisionRequest {
  string escalation_id = 1;
  string decided_by = 2;         // Human reviewer identity
  string reason = 3;
}

message EscalationDecisionResponse {
  bool acknowledged = 1;
  string task_id = 2;
}

message GetPolicyRequest {
  string policy_id = 1;
}

message UpdatePolicyRequest {
  Policy policy = 1;
}

message ListPoliciesRequest {
  string scope = 1;             // empty = all
  bool include_disabled = 2;
}

message Policy {
  string policy_id = 1;
  string version = 2;
  string name = 3;
  string description = 4;
  string scope = 5;
  int32 priority = 6;
  repeated PolicyCondition conditions = 7;
  repeated PolicyAction actions = 8;
  bool enabled = 9;
  string created_by = 10;
  int64 created_at_unix = 11;
  int64 updated_at_unix = 12;
}

message PolicyCondition {
  string field = 1;
  string operator = 2;
  string value = 3;
}

message PolicyAction {
  string action = 1;
  string reason = 2;
  map<string, string> parameters = 3;
}
```

### A.4 Worker gRPC (ADDED — was referenced in §6.3 but not defined in v1.0)

```protobuf
syntax = "proto3";
package aeos.worker.v1;

service WorkerService {
  rpc SubmitStep(SubmitStepRequest) returns (SubmitStepResponse);
  rpc CancelStep(CancelStepRequest) returns (CancelStepResponse);
  rpc GetStepStatus(GetStepStatusRequest) returns (GetStepStatusResponse);
}

service HealthService {
  rpc Check(HealthCheckRequest) returns (HealthCheckResponse);
  rpc Watch(HealthCheckRequest) returns (stream HealthCheckResponse);
}

message SubmitStepRequest {
  string step_id = 1;
  string workflow_id = 2;
  string node_type = 3;
  string agent_type = 4;
  string task_description = 5;
  bytes input_data = 6;
  string governance_token = 7;
  int64 deadline_ms = 8;
}

message SubmitStepResponse {
  bool accepted = 1;
  string rejection_reason = 2;
}

message CancelStepRequest {
  string step_id = 1;
  string workflow_id = 2;
}

message CancelStepResponse {
  bool acknowledged = 1;
}

message GetStepStatusRequest {
  string step_id = 1;
  string workflow_id = 2;
}

message GetStepStatusResponse {
  string status = 1;   // "accepted" | "executing" | "completed" | "failed" | "cancelled"
  bytes result = 2;    // serialized StepResult if completed
}

message HealthCheckRequest {
  string service = 1;
}

message HealthCheckResponse {
  string status = 1;   // "SERVING" | "NOT_SERVING" | "UNKNOWN"
  NodeLoad load = 2;
}

message NodeLoad {
  int32 active_steps = 1;
  int32 queued_steps = 2;
  float cpu_utilization = 3;
  float memory_utilization = 4;
}
```

---

## Appendix B: Configuration Reference

*Updated: New environment variables for v1.1 changes.*

```bash
# (All v1.0 variables retained unchanged)

# NEW in v1.1:
AEOS_KAFKA_TASK_TOPIC_PARTITIONS=200   # ← changed from 20
AEOS_KAFKA_TASK_CONSUMER_GROUP=aeos-workers  # shared group for task topics
AEOS_RAFT_STATE_PATH=/var/aeos/raft.json     # durable Raft term storage
AEOS_STEP_LEASE_TTL_S=120             # execution lease TTL in seconds
AEOS_MERGE_NODE_TIMEOUT_S=120         # MergeNode default timeout
AEOS_GOVERNANCE_FAIL_OPEN=false       # safety lock: MUST remain false
AEOS_GOVERNANCE_EVAL_BUDGET_MS=50     # policy evaluation SLA
AEOS_LLM_CACHE_ENABLED=false          # opt-in LLM cache (default false)
AEOS_REDIS_CLUSTER_MODE=true          # use Redis Cluster client
AEOS_EVENT_BUFFER_BACKEND=redis       # "redis" | "memory" (redis is default)
```

---

## Appendix C: Redis Key Schema

*New appendix (referenced by §5.2, §7, §8, §10).*

All keys use Redis hashtags to guarantee slot co-location for MULTI/EXEC atomicity.

```
# Workflow execution keys (shard by workflow_id)
{wf:<workflow_id>}:status              STRING  "running"|"completed"|"failed"
{wf:<workflow_id>}:graph               STRING  ExecutionGraph JSON
{wf:<workflow_id>}:last_heartbeat      STRING  Unix timestamp (float)
{wf:<workflow_id>}:checkpoint:<seq>    STRING  Checkpoint JSON
{wf:<workflow_id>}:latest_checkpoint   STRING  seq number (int)

# Per-step keys (shard by workflow_id via hashtag)
{wf:<workflow_id>}:step:<step_id>:status        STRING  "accepted"|"executing"|"completed"|"failed"|"cancelled"
{wf:<workflow_id>}:step:<step_id>:result        STRING  StepResult JSON
{wf:<workflow_id>}:step:<step_id>:idem          STRING  Idempotency result JSON  TTL=86400s
{wf:<workflow_id>}:step:<step_id>:lease         STRING  worker_node_id           TTL=120s
{wf:<workflow_id>}:step:<step_id>:next_published STRING  "true"|absent

# Working memory keys (shard by session_id)
{wm:<session_id>}:<user_key>           STRING|BYTES  Value
{wm:<session_id>}:__meta               STRING  JSON metadata
{wm:<session_id>}:__keys               SET     User-defined key names

# LLM cache keys (shard by hash of prompt)
llm:cache:<sha256_of_prompt>           STRING  LLM response text  TTL=3600s

# LLM rate limiting (shard by provider)
{llm_rl:<provider>}:rpm                STRING  Request count (sliding window, TTL=60s)
{llm_rl:<provider>}:tpm                STRING  Token count (sliding window, TTL=60s)

# RBAC cache (shard by user_id)
{rbac:<user_id>}:roles                 STRING  JSON list of roles  TTL=300s (invalidated by RBAC_REVOKED event)

# Event buffer fallback (shard by node_id)
{event_buf:<node_id>}:events           LIST    Buffered event JSON (ring, LPUSH+LTRIM)

# Capability Registry cache (read-through, shard by capability_id)
{cap:<capability_id>}:routes           STRING  JSON list of CapabilityRoute  TTL=5s
```

---

## Appendix D: aeos-client SDK Interface

*New appendix (was referenced in §19.4 but not defined in v1.0).*

The `aeos-client` Python SDK provides a typed interface for external AEOS consumers. Full implementation is a Phase 10 deliverable; this appendix defines the interface contract that the SDK must implement.

```python
from aeos_client import AEOSClient, WorkflowResult, WorkflowStatus

# Initialization
client = AEOSClient(
    base_url="https://aeos.example.com",
    api_key="eyJhbGci...",          # JWT, max 1-hour expiry
    timeout_s=30.0,
)

# Core operations

async def submit_workflow(
    task: str,
    *,
    priority: int = 5,              # 1 (critical) – 10 (batch)
    deadline_ms: int = 300_000,     # 5-minute default
    metadata: dict = {},
) -> str:                           # Returns workflow_id
    ...

async def get_workflow_status(workflow_id: str) -> WorkflowStatus:
    """
    Returns WorkflowStatus with fields:
      status: str                   # "submitted"|"running"|"completed"|"failed"|"escalated"
      step_count: int
      completed_steps: int
      failed_steps: int
      governance_decision: str
      submitted_at: datetime
      completed_at: datetime | None
      result: dict | None
      error: str | None
    """
    ...

async def cancel_workflow(workflow_id: str) -> bool:
    ...

async def stream_workflow_events(
    workflow_id: str,
) -> AsyncIterator[WorkflowEvent]:
    """
    Yields WorkflowEvent objects as the workflow progresses.
    Events: WORKFLOW_STARTED, NODE_STARTED, NODE_COMPLETED, NODE_FAILED,
            WORKFLOW_COMPLETED, WORKFLOW_FAILED, ESCALATION_PENDING
    """
    ...

async def list_capabilities() -> list[CapabilityInfo]:
    """
    Returns current cluster capabilities and their health states.
    """
    ...

# Context manager support
async with AEOSClient(...) as client:
    wf_id = await client.submit_workflow("analyze this dataset")
    result = await client.get_workflow_status(wf_id)
```

---

## Appendix E: AWS Cost Estimate

*New appendix (NTH-5 from review board).*

Monthly cost estimate for a standard Phase 9 production deployment (3-zone, 5-worker initial cluster, us-east-1 pricing):

| Component | AWS Service | Spec | $/month |
|-----------|-------------|------|---------|
| Workers (5 nodes) | EC2 `c5.2xlarge` | 3 on-demand + 2 spot (~70% discount) | ~$350 |
| Cluster Manager | EC2 `t3.large` (3 nodes) | On-demand | ~$180 |
| Redis Cluster | ElastiCache `r6g.large` (3 shards × 1 replica) | On-demand | ~$450 |
| Kafka | MSK `kafka.m5.large` (3 brokers) | On-demand | ~$540 |
| PostgreSQL | RDS `db.r6g.large` Multi-AZ | On-demand | ~$280 |
| Weaviate (vector store) | EC2 `r5.xlarge` (3 nodes) | On-demand | ~$720 |
| Vault HA | EC2 `t3.large` (3 nodes) | On-demand | ~$180 |
| Jaeger + Prometheus | EC2 `t3.medium` (2 nodes) | On-demand | ~$70 |
| Elasticsearch (logs) | EC2 `r5.large` (2 nodes) | On-demand | ~$290 |
| EKS control plane | EKS | Managed | ~$150 |
| ALB + WAF | ALB | Per-request | ~$50 |
| NAT Gateway | NAT | Per-GB | ~$50 |
| S3 (cold storage) | S3 Standard + Glacier IR | 100 GB/month | ~$10 |
| Data transfer (cross-AZ) | EC2 | Estimate | ~$150 |
| ECR (container images) | ECR | ~10 GB | ~$10 |
| **Total (initial 5-worker)** | | | **~$3,480/month** |
| **At 20 workers (steady state)** | | +15 workers × $70 | **~$4,530/month** |
| **At 100 workers (max scale)** | | +95 workers × $70 | **~$10,130/month** |

**Cost optimization opportunities:**
1. Replace Weaviate (self-hosted) with AWS OpenSearch Serverless: save ~$720/month
2. Replace Vault (self-hosted) with AWS Secrets Manager + ACM Private CA: save ~$180/month + reduce operations load
3. Savings Plans (1-year commitment): ~30% reduction on EC2 costs
4. Spot instances for Weaviate read nodes: additional 60–70% reduction

---

## Appendix F: Glossary

*Updated: New terms added, circuit breaker states unified with Phase 8.3.*

| Term | Definition |
|------|-----------|
| `aeos-workers` | The shared Kafka consumer group ID used by all worker nodes for task topic consumption |
| At-least-once delivery | Delivery semantics where a message may be delivered more than once; idempotent executors handle duplicates |
| Broadcast consumer | Kafka consumer with per-node group ID; every worker receives every message |
| Circuit Breaker | CLOSED/OPEN/HALF_OPEN state machine isolating failing capabilities (unified state names across DEE and Registry) |
| Cluster Manager | Raft-based leader election and membership management service |
| Competing consumer | Kafka consumer in a shared group; exactly one worker in the group receives each message |
| DEE | Distributed Execution Engine (Phase 8.3) |
| DRP | Distributed Runtime Platform (this document) |
| Execution lease | Redis SETNX key preventing concurrent double-execution of the same step |
| Fail-closed | Security/governance policy that rejects when uncertain (opposite of fail-open) |
| GovernanceToken | Signed JWT proving a task passed the policy gate, with dynamic expiry |
| Hashtag key | Redis key using `{tag}` syntax to force slot co-location for MULTI/EXEC atomicity |
| HyperKernel | The Phase 8 kernel, extended to 7 phases in Phase 9 (adds JOINING as phase 5) |
| Idempotency key | Redis key storing a step's result to prevent re-execution on at-least-once redelivery |
| KEDA | Kubernetes Event-driven Autoscaling — required operator for Kafka-based HPA |
| Intermediate CA | Second-layer Certificate Authority signed by Root CA; issues all leaf certs |
| MergeNode | A workflow node that waits for all parallel predecessor steps before aggregating results |
| Partition assignment | Which Kafka partitions a worker consumes from, assigned by Cluster Manager on join |
| Raft | Distributed consensus algorithm; used for Cluster Manager leader election and membership log |
| Two-phase checkpoint | Phase 1: write result+status+idem to Redis. Phase 2: mark next_published. Enables recovery from partial failures. |
| Worker Node | An `aeos-worker` process: full Kernel + ExecutionEngine + Agents + DistributedScheduler |
| Working Memory | Session-scoped Redis-Cluster-backed memory tier; hashtag keys ensure slot co-location |

---

*End of RFC-009 v1.1 — AEOS Phase 9 Distributed Runtime Platform Specification*  
*Document version: 1.1.0 — 2026-07-06*  
*Architecture Readiness Score: 96/100*  
*Next action: Architecture Review Board sign-off → Phase 9B Milestone 9B-1 kickoff*
