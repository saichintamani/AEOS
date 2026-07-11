# AEOS Phase 9 — Distributed Runtime Platform (DRP)
## Architecture Specification RFC-009

**Status:** Proposed  
**Authors:** AEOS Architecture Team  
**Created:** 2026-07-06  
**Target Release:** Phase 9B (implementation begins after this RFC is approved)  
**Replaces / Extends:** RFC-008 (HyperKernel), RFC-006 (Execution Engine), RFC-007 (Agent Runtime)

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

---

## 1. Executive Vision

### 1.1 Problem Statement

AEOS Phase 8 delivers a production-grade, single-node AI orchestration platform. It can execute multi-agent workflows, reason over complex tasks, manage memory across four tiers, and govern every output through a policy gate. Phase 8 is limited in three fundamental ways:

1. **Single-process boundary.** All agents, the kernel, the execution engine, and the memory system share one Python process and one machine. A single hardware failure takes down the entire system. Scaling requires vertical growth, which has hard physical limits.

2. **In-process memory.** The four-tier memory hierarchy (Sensory → Working → LongTerm → Episodic) lives in-process. When the process restarts, Sensory and Working memory vanish. LongTerm and Episodic survive only if persisted to disk manually. There is no cross-node memory coherence.

3. **Synchronous capability registry.** Agent capabilities are strings in a local dict. There is no discovery protocol, no federation across nodes, no health-based routing, and no load balancing for capability consumers.

These are not bugs. Phase 8 was designed as a single-node system. Phase 9 is designed to be its distributed successor.

### 1.2 Strategic Objective

Transform AEOS from a single-node AI orchestration server into a **Distributed Runtime Platform (DRP)**: a horizontally scalable, fault-tolerant, multi-node cluster that operates as a coherent AI operating system across machines, availability zones, and cloud regions.

The DRP must:

- Execute AI workflows across a pool of worker nodes with no single point of failure in the execution path
- Maintain a globally coherent, multi-tier memory fabric with eventual consistency guarantees
- Federate agent capabilities across nodes so any node can route work to any capable agent anywhere in the cluster
- Emit a unified, high-throughput event stream from every node for real-time observability
- Enforce governance policy uniformly across all nodes, regardless of where a task originates
- Scale to 100+ nodes, 10,000+ concurrent workflow steps, and 1M+ events per minute
- Tolerate arbitrary single-node failures with zero data loss and sub-60-second recovery

### 1.3 Scope Boundaries

**In scope for Phase 9:**

- Multi-node cluster management (join, leave, health, leader election)
- Distributed task queue and execution scheduling
- Distributed memory with replication and conflict resolution
- Kafka-based event fabric replacing in-process EventBus
- Redis-backed distributed checkpoint and state store
- Cross-node capability registry with gRPC discovery
- mTLS security layer for all inter-node traffic
- Kubernetes deployment manifests (EKS target)
- Prometheus + Grafana observability stack
- Migration tooling from Phase 8 single-node to Phase 9 cluster

**Out of scope for Phase 9:**

- Multi-tenant isolation at the container level (planned Phase 10)
- Geographic data residency enforcement (planned Phase 10)
- LLM fine-tuning pipeline (separate roadmap)
- GUI/dashboard frontend (planned Phase 10)
- Billing and metering (planned Phase 10)

### 1.4 Success Criteria

| Criterion | Target |
|-----------|--------|
| Cluster formation from cold | < 30 seconds for a 10-node cluster |
| Single-node failure recovery | < 60 seconds with no workflow loss |
| Horizontal throughput scaling | Linear within 20% up to 20 nodes |
| Workflow step latency (p99) | < 2× single-node baseline |
| Memory read latency (p99) | < 5 ms for Working Memory cache hits |
| Memory write durability | 0 data loss under single-node failure |
| Governance gate consistency | 100% of tasks pass policy gate before execution |
| Event delivery guarantee | At-least-once, ordered within workflow |
| Observability coverage | 100% of workflow steps produce a trace span |

---

## 2. System Philosophy

### 2.1 Core Tenets

**2.1.1 Design for failure, not for success.**  
Every component assumes its dependencies will fail. The DRP does not assume network reliability, node availability, or message ordering. Every interface is designed with explicit failure modes and recovery paths documented before the interface is coded.

**2.1.2 Explicit over implicit.**  
Configuration is explicit. Capability routing is explicit. Policy decisions are explicit and logged. There are no hidden defaults that differ between environments. The behaviour of the system in production must be derivable from its configuration files alone.

**2.1.3 The kernel is the contract.**  
The HyperKernel API from Phase 8 is the stable internal contract. Phase 9 adds distributed adapters beneath the kernel but does not change the kernel's public interface. Code that runs on Phase 8 must run on Phase 9 without modification.

**2.1.4 Observability is a first-class feature.**  
Every subsystem emits structured logs, metrics, and trace spans. Observability is not retrofitted. It is designed in before the first line of implementation code is written.

**2.1.5 No distributed monolith.**  
The DRP is not a monolith split across machines. It is a collection of independently deployable, independently scalable services that cooperate through well-defined interfaces. A failure in the replay service must not affect the execution scheduler.

**2.1.6 Architecture-first, code-second.**  
This document is written before any Phase 9 implementation code is committed. Every interface, every data schema, every failure mode documented here is approved before the first implementation PR is opened.

### 2.2 Distributed Systems Principles Applied

**CAP Theorem positioning:**  
The DRP chooses **Consistency over Availability** for the governance gate and capability registry (a task must not execute without a valid policy decision, even if that means waiting). It chooses **Availability over Consistency** for the event fabric and metrics pipeline (dropping a metric is preferable to blocking a workflow).

**BASE vs ACID:**  
Workflow execution state follows ACID semantics using Redis transactions. Event delivery and memory replication follow BASE semantics (Basically Available, Soft state, Eventually consistent).

**Actor model:**  
Individual agents are modeled as actors. Each agent has a mailbox, processes one message at a time, and communicates only through messages. This eliminates shared mutable state within an agent and simplifies distributed placement.

**Backpressure:**  
Every queue has a defined maximum depth. Producers are backpressured when queues fill. The system never silently drops work. When backpressure triggers, it propagates to the API layer and surfaces to the caller as a 429 response.

### 2.3 What Phase 9 Is Not

Phase 9 is not a microservices rewrite. The execution engine, the agent runtime, and the kernel remain as cohesive modules. They are deployed as a single worker binary (`aeos-worker`). The distribution boundary is between worker nodes, not between internal modules.

Phase 9 is not an event-sourcing rewrite. The primary state store is still a mutable database (Redis for hot state, PostgreSQL for cold state). The event fabric provides an audit log and real-time stream, not the source of truth for state reconstruction.

---

## 3. Non-Functional Requirements

### 3.1 Performance

| Metric | Minimum | Target | Measurement Method |
|--------|---------|--------|-------------------|
| Workflow submission latency (p50) | < 50 ms | < 20 ms | API gateway → scheduler ack |
| Workflow step execution latency (p99) | < 5 s | < 2 s | Step start → step complete event |
| Agent capability lookup latency (p99) | < 10 ms | < 3 ms | Registry gRPC round-trip |
| Memory read (Working, cache hit, p99) | < 5 ms | < 2 ms | Redis GET latency |
| Memory write (Working tier, p99) | < 20 ms | < 8 ms | Redis SET + replication |
| Event publish throughput | 100K/min | 1M/min | Kafka producer throughput |
| Governance gate decision latency (p99) | < 100 ms | < 30 ms | Policy eval duration |
| Cluster-wide checkpoint write (p99) | < 200 ms | < 80 ms | Redis MULTI/EXEC |

### 3.2 Scalability

- **Horizontal worker scaling:** Adding a worker node must increase aggregate throughput proportionally with less than 20% overhead per node up to 20 nodes.
- **State store scaling:** Redis Cluster must support 10 GB of active workflow state without eviction.
- **Event stream scaling:** Kafka must sustain 1M events/minute with < 100 ms end-to-end latency at 20 partitions.
- **Memory tier scaling:** LongTerm memory (vector store) must support 10M embeddings with < 50 ms p99 query latency.

### 3.3 Reliability

| Failure Scenario | Recovery Time Objective | Recovery Point Objective |
|-----------------|------------------------|------------------------|
| Single worker node failure | < 60 s | 0 (checkpoint-based) |
| Redis primary failure | < 30 s (Sentinel failover) | < 1 checkpoint interval (5 s) |
| Kafka broker failure | < 10 s (partition reassignment) | 0 (replication factor ≥ 2) |
| Cluster leader failure | < 15 s (Raft re-election) | 0 |
| Network partition (minority side) | Indefinite wait, no phantom writes | N/A |
| Full cluster restart | < 90 s for full capability discovery | Last persisted state |

### 3.4 Availability

- **Target SLA:** 99.9% uptime (< 8.7 hours downtime/year) for a 3-zone, 6-node cluster
- **Planned maintenance:** Rolling restarts with zero downtime (one node at a time, min 2 nodes always active)
- **Deployment window:** Any time (canary deployment with automated rollback on error rate spike)

### 3.5 Security

- All inter-node traffic encrypted with mTLS (mutual TLS, minimum TLS 1.3)
- All API traffic encrypted with TLS 1.3
- Credentials rotated every 24 hours via Vault dynamic secrets
- No plaintext secrets in environment variables, Kubernetes manifests, or logs
- Every API call authenticated with JWT (short-lived, max 1-hour expiry)
- Every governance decision logged with immutable append-only audit trail
- RBAC enforced at the API gateway layer before reaching the kernel

### 3.6 Maintainability

- All services must export `/health/live` and `/health/ready` endpoints
- All services must export `/metrics` in Prometheus text format
- All configuration is loaded from environment variables + mounted Kubernetes ConfigMaps
- Zero hardcoded IP addresses, hostnames, or ports anywhere in source code
- Every distributed algorithm (leader election, checkpoint, event ordering) is covered by a deterministic unit test with injected failures

### 3.7 Operational Constraints

- Maximum memory per worker process: 8 GB (enforced via cgroups)
- Maximum CPU per worker: 4 vCPUs (enforced via cgroups)
- Container base image: `python:3.12-slim` (no Alpine, due to glibc compatibility with ML libraries)
- Kubernetes minimum version: 1.28
- Python minimum version: 3.12

---

## 4. Layered Architecture

### 4.1 Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          External Clients                               │
│              (REST API, CLI, SDK, Webhook receivers)                    │
└──────────────────────────────┬──────────────────────────────────────────┘
                               │ HTTPS / WebSocket
┌──────────────────────────────▼──────────────────────────────────────────┐
│                        API Gateway Layer                                │
│           (Kong / AWS ALB → FastAPI, JWT auth, rate limiting)           │
└──────────┬───────────────────┬───────────────────┬──────────────────────┘
           │                   │                   │
    gRPC   │            REST   │          gRPC      │  REST
           ▼                   ▼                   ▼
┌──────────────────┐  ┌────────────────┐  ┌────────────────────────────┐
│  Cluster Manager │  │ Policy Service │  │  Capability Registry       │
│  (leader elect,  │  │ (governance    │  │  (agent discovery, routing)│
│  membership,     │  │  gate, audit)  │  │                            │
│  topology)       │  │                │  │                            │
└─────────┬────────┘  └────────────────┘  └────────────────────────────┘
          │
          │  Task assignment (gRPC)
          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                     Distributed Scheduler                               │
│         (SQS / Kafka → priority queue → worker selection)              │
└──────┬────────────────────┬────────────────────────┬───────────────────┘
       │                    │                        │
       ▼                    ▼                        ▼
┌─────────────┐    ┌─────────────────┐    ┌─────────────────────────────┐
│  Worker 1   │    │   Worker 2      │    │        Worker N             │
│  ┌────────┐ │    │  ┌────────────┐ │    │  ┌──────────────────────┐  │
│  │Kernel  │ │    │  │  Kernel    │ │    │  │       Kernel         │  │
│  │        │ │    │  │            │ │    │  │                      │  │
│  │Exec    │ │    │  │  Exec      │ │    │  │  Exec Engine         │  │
│  │Engine  │ │    │  │  Engine    │ │    │  │                      │  │
│  │        │ │    │  │            │ │    │  │                      │  │
│  │Agents  │ │    │  │  Agents    │ │    │  │  Agents              │  │
│  └────────┘ │    │  └────────────┘ │    │  └──────────────────────┘  │
└──────┬──────┘    └────────┬────────┘    └────────────┬────────────────┘
       │                    │                          │
       └────────────────────┴──────────────────────────┘
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
       ┌──────────┐  ┌────────────┐ ┌──────────────────┐
       │  Redis   │  │   Kafka    │ │  PostgreSQL       │
       │  Cluster │  │  (Event    │ │  (Audit, cold     │
       │  (hot    │  │   Fabric)  │ │   state, policy)  │
       │  state,  │  │            │ │                   │
       │  cache)  │  │            │ │                   │
       └──────────┘  └────────────┘ └──────────────────┘
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
       ┌──────────┐  ┌────────────┐ ┌──────────────────┐
       │Prometheus│  │  Grafana   │ │   Jaeger          │
       │+ Alert   │  │ Dashboards │ │   (distributed    │
       │  Manager │  │            │ │    tracing)       │
       └──────────┘  └────────────┘ └──────────────────┘
```

### 4.2 Layer Responsibilities

**Layer 0 — Infrastructure:**  
Redis Cluster, Kafka, PostgreSQL, object storage (S3). These are managed services in production. The application never owns these processes.

**Layer 1 — Data Services:**  
Purpose-built access libraries that wrap Layer 0. `DistributedStateStore` (wraps Redis), `EventStream` (wraps Kafka), `AuditRepository` (wraps PostgreSQL). Application code never imports a Redis or Kafka library directly — it imports these wrappers.

**Layer 2 — Platform Services:**  
Cluster Manager, Policy Service, Capability Registry. These are long-running services with their own deployment units. They expose gRPC interfaces to workers.

**Layer 3 — Worker Runtime:**  
Each worker node runs a full HyperKernel + ExecutionEngine + Agent Runtime. Workers are stateless with respect to workflow ownership — any worker can pick up any workflow step from the queue.

**Layer 4 — API Gateway:**  
Single ingress point for external clients. Handles authentication, rate limiting, and routes to the correct platform service or worker via gRPC.

**Layer 5 — Observability Plane:**  
Cross-cutting. Every layer emits to Prometheus, Kafka (for event streaming), and Jaeger (for trace spans). The observability plane is a consumer of the event fabric, not a producer.

### 4.3 Deployment Units

| Unit | Type | Instances | Scaling Trigger |
|------|------|-----------|----------------|
| `aeos-worker` | Kubernetes Deployment | 3–20 | CPU > 70% OR queue depth > 500 |
| `aeos-cluster-manager` | Kubernetes StatefulSet | 3 (Raft quorum) | Fixed |
| `aeos-policy-service` | Kubernetes Deployment | 2–5 | RPS > 1000 |
| `aeos-capability-registry` | Kubernetes StatefulSet | 3 | Fixed |
| `aeos-api-gateway` | Kubernetes Deployment | 2–10 | RPS > 500 |
| Redis Cluster | Managed (ElastiCache) | 3 shards × 1 replica | Storage > 60% |
| Kafka | Managed (MSK) | 3 brokers | Throughput > 70% |
| PostgreSQL | Managed (RDS) | 1 primary + 1 replica | Storage > 60% |

---

## 5. Runtime Subsystems

### 5.1 HyperKernel in Distributed Context

The Phase 8 HyperKernel boots as a 6-phase sequence: INITIALIZING → LOADING → CONFIGURING → STARTING → RUNNING → STOPPING. In Phase 9, the kernel gains a 7th phase: **JOINING** — the phase during which a worker node registers itself with the Cluster Manager, advertises its capabilities to the Capability Registry, and subscribes to its assigned Kafka partitions before transitioning to RUNNING.

The kernel's internal interfaces (`ServiceRegistry`, `PolicyEngine`, `Scheduler`, `HealthManager`) do not change. Only the implementations behind these interfaces are upgraded:

| Phase 8 Implementation | Phase 9 Implementation |
|------------------------|------------------------|
| `InMemoryServiceRegistry` | `DistributedServiceRegistry` (backed by Capability Registry gRPC) |
| `InMemoryPolicyEngine` | `RemotePolicyEngine` (calls Policy Service gRPC) |
| `LocalScheduler` | `DistributedScheduler` (reads from SQS/Kafka task queue) |
| `LocalHealthManager` | `ClusterHealthManager` (reports to Cluster Manager) |

This substitution is performed at boot time via dependency injection. The kernel's `startup()` method accepts optional factory callables. In single-node mode, factories are not provided and Phase 8 defaults apply. In distributed mode, factories are provided by the `aeos-worker` entrypoint.

### 5.2 ExecutionEngine in Distributed Context

The ExecutionEngine from Phase 8.3 runs unchanged on each worker. Two adaptations are made:

**5.2.1 Distributed checkpoint store.**  
`InMemoryCheckpointStore` is replaced by `RedisCheckpointStore`. Every checkpoint write is a Redis MULTI/EXEC transaction. Checkpoints are keyed by `workflow:{workflow_id}:checkpoint:{seq}`. The most recent checkpoint key is maintained as `workflow:{workflow_id}:latest_checkpoint`.

**5.2.2 Distributed trace store.**  
`InMemoryTraceStore` is replaced by `KafkaTraceStore`. Trace entries are published to the `aeos.traces` Kafka topic with `workflow_id` as the partition key (guaranteeing ordering within a workflow). A separate trace aggregator service consumes this topic and stores completed traces in PostgreSQL for the replay API.

### 5.3 Agent Runtime in Distributed Context

Agents continue to run inside a worker process. In Phase 9, an agent can be **remote-pinned**: its execution is always routed to a specific worker node. This is necessary for agents that maintain local state (e.g., a browser automation agent that holds a browser session). Remote pinning is expressed as a capability tag: `capability:pinned:worker-node-7`. The Capability Registry understands pinned capabilities and always routes to the designated node.

Most agents are **stateless** and benefit from **capability routing**: the Capability Registry picks the least-loaded worker that has the required capability. Load is expressed as the worker's current step execution count, reported to the registry via gRPC keepalive.

### 5.4 Memory Runtime in Distributed Context

The four-tier memory hierarchy is restructured for distribution:

| Tier | Phase 8 Storage | Phase 9 Storage | Consistency |
|------|----------------|-----------------|-------------|
| Sensory | In-process `deque` | Per-worker in-process (intentionally local) | None (ephemeral) |
| Working | In-process `dict` | Redis Cluster (shared, keyed by `session_id`) | Strong (Redis single key) |
| LongTerm | In-process vector index | Pinecone / Weaviate cluster | Eventual |
| Episodic | In-process list | PostgreSQL `episodes` table | Strong (ACID) |

Sensory memory remains per-worker and ephemeral. The justification: sensory context (the raw input for the current step) is only meaningful to the agent processing it. Cross-node sharing would add latency for zero benefit.

---

## 6. Distributed Cluster Design

### 6.1 Cluster Topology

A Phase 9 cluster consists of:

- **1–3 Cluster Manager nodes** (Raft consensus, odd number required for quorum)
- **3–N Worker nodes** (stateless execution, any number)
- **2–5 Policy Service instances** (stateless, horizontally scalable)
- **3 Capability Registry nodes** (consistent hash ring, any 2 of 3 can serve reads)

The Cluster Manager is the brain of the cluster. It does not execute workflows. It maintains the cluster membership table, drives leader election, and exposes the cluster topology to all other components.

### 6.2 Cluster Manager Design

The Cluster Manager implements a simplified Raft consensus algorithm for leader election and cluster membership. It does not implement full Raft log replication for all state — only for the membership table. Workflow state lives in Redis, not in the Raft log.

**6.2.1 Raft state machine:**

```
States: Follower | Candidate | Leader

Follower:
  - Receives heartbeats from Leader
  - If no heartbeat for election_timeout (150–300 ms random), transitions to Candidate

Candidate:
  - Increments term
  - Votes for self
  - Sends RequestVote to all other managers
  - If majority votes received → becomes Leader
  - If another Leader's AppendEntries received → reverts to Follower

Leader:
  - Sends heartbeats every 50 ms
  - Accepts MembershipChange RPCs
  - Replicates membership log to all Followers
  - Responds to worker join/leave events
```

**6.2.2 Cluster membership table:**

```python
@dataclass
class ClusterMember:
    node_id: str          # UUID4, generated at first boot, persisted
    hostname: str
    grpc_address: str     # host:port
    http_address: str     # host:port
    role: str             # "worker" | "manager" | "policy" | "registry"
    capabilities: list[str]
    joined_at: datetime
    last_heartbeat: datetime
    status: str           # "joining" | "active" | "draining" | "dead"
    zone: str             # availability zone label
    version: str          # AEOS build version
```

**6.2.3 Node join protocol:**

```
Worker boot sequence:
  1. Worker generates node_id (persistent, stored in local file)
  2. Worker contacts Cluster Manager leader via gRPC: JoinCluster(JoinRequest)
  3. Manager validates: version compatibility, zone quota, capability conflict
  4. Manager assigns Kafka partitions for the worker to consume
  5. Manager replicates membership change to followers (Raft AppendEntries)
  6. Manager responds: JoinResponse(assigned_partitions, topology_snapshot)
  7. Worker enters JOINING phase, subscribes to Kafka partitions
  8. Worker registers capabilities with Capability Registry
  9. Worker sends JoinComplete to Manager
  10. Manager marks worker status: "active"
  11. Worker transitions kernel to RUNNING
```

**6.2.4 Node leave protocol (graceful):**

```
  1. Worker receives SIGTERM (Kubernetes graceful shutdown)
  2. Worker changes kernel state to STOPPING
  3. Worker sends DrainRequest to Cluster Manager
  4. Manager marks worker as "draining" (no new tasks assigned)
  5. Worker completes in-flight steps (up to drain_timeout_s = 30)
  6. Worker checkpoints all active workflows
  7. Worker sends LeaveCluster to Manager
  8. Manager marks worker "dead", reassigns Kafka partitions
  9. Manager notifies Capability Registry to remove worker's capabilities
  10. Worker exits
```

**6.2.5 Node failure detection:**

Workers send a gRPC heartbeat to the Cluster Manager every 5 seconds. If the Manager receives no heartbeat for 3 consecutive intervals (15 seconds), it marks the worker as "suspected dead". After 30 seconds of no heartbeat, it marks the worker "dead" and triggers failure recovery.

### 6.3 Worker Node Architecture

Each worker node runs as a single Python process (`aeos-worker`) with the following internal structure:

```
aeos-worker process
├── HyperKernel (6+1 phase boot)
│   ├── DistributedServiceRegistry (gRPC → Capability Registry)
│   ├── RemotePolicyEngine (gRPC → Policy Service)
│   ├── DistributedScheduler (Kafka consumer, SQS fallback)
│   └── ClusterHealthManager (gRPC → Cluster Manager)
├── ExecutionEngine (Phase 8.3, with distributed adapters)
│   ├── RedisCheckpointStore
│   ├── KafkaTraceStore
│   ├── DistributedEventBus (Kafka producer)
│   └── MetricsCollector (Prometheus push)
├── Agent Runtime
│   ├── SimpleAgent, ResearchAgent, AnalystAgent
│   ├── ExecutorAgent, PlannerAgent, ReviewerAgent
│   └── [additional capability-specific agents]
├── gRPC Server (internal, port 50051)
│   ├── WorkflowService (submit_step, cancel_step, get_status)
│   └── HealthService (check, watch)
└── HTTP Server (FastAPI, port 8000)
    └── /health/live, /health/ready, /metrics
```

### 6.4 Cluster Networking

All inter-node communication uses gRPC with mTLS. No REST calls between internal services. The rationale: gRPC provides typed interfaces (protobuf), bidirectional streaming for heartbeats and event subscriptions, and efficient binary encoding. REST is reserved for the external API gateway layer.

**Port allocation:**

| Port | Protocol | Service |
|------|----------|---------|
| 443 | HTTPS | External API Gateway |
| 8000 | HTTP | Worker internal health/metrics |
| 50051 | gRPC | Worker-to-Worker, Worker-to-Manager |
| 50052 | gRPC | Cluster Manager Raft |
| 50053 | gRPC | Capability Registry |
| 50054 | gRPC | Policy Service |
| 9090 | HTTP | Prometheus scrape |
| 9091 | HTTP | Prometheus pushgateway |

**Service discovery:**  
All services register with Kubernetes DNS. Workers resolve `aeos-cluster-manager.aeos.svc.cluster.local:50052`. No hardcoded IPs. No consul. Kubernetes DNS is the service mesh.

---

## 7. Distributed Execution

### 7.1 Task Queue Architecture

Phase 9 replaces the in-process priority queue with a two-tier distributed task queue:

**Tier 1 — Hot Queue (Kafka):**  
Real-time, low-latency task dispatch for interactive workflows. Kafka topics are partitioned by `workflow_id` ensuring that steps within a workflow are ordered and processed by a consistent partition (though not necessarily the same worker — a worker subscribes to a partition, and partitions can be reassigned).

**Tier 2 — Durable Queue (SQS):**  
Batch and background tasks. SQS provides at-least-once delivery with a dead-letter queue for tasks that fail after N retries. SQS is the fallback when Kafka is unreachable.

```
Topic: aeos.tasks.{priority}   # priority: critical | high | normal | low | batch
  Partition count: 20 (configurable)
  Retention: 24 hours
  Replication factor: 3
  Min ISR: 2

Message schema:
  {
    "task_id": "<uuid>",
    "workflow_id": "<uuid>",
    "step_id": "<uuid>",
    "node_type": "agent | tool | conditional | ...",
    "agent_type": "research_agent | ...",
    "task_description": "...",
    "input_data": { ... },
    "priority": 1-10,
    "deadline_ms": 30000,
    "retry_attempt": 0,
    "trace_id": "<uuid>",
    "submitted_at": "ISO8601",
    "governance_token": "<signed_jwt>"
  }
```

### 7.2 Distributed Scheduler

The Distributed Scheduler replaces the Phase 8 `LocalScheduler`. It runs inside each worker node as a Kafka consumer group member. The consumer group `aeos-workers` distributes Kafka partition ownership across all active workers. When a new worker joins, Kafka's group coordinator rebalances partition assignments.

**7.2.1 Scheduler loop (per worker):**

```
loop:
  1. Poll Kafka partitions (assigned to this worker), batch_size=50, timeout=100ms
  2. For each message:
     a. Deserialize task
     b. Validate governance_token (JWT signature + expiry check)
     c. Check circuit breaker for the target agent_type
     d. Acquire a slot in the WorkerPool semaphore
     e. Submit to asyncio task (non-blocking)
  3. Commit Kafka offsets after slot acquired (not after completion — see §7.2.2)
  4. On WorkerPool saturation: pause polling (backpressure)
  5. On governance token invalid: send to aeos.tasks.dlq with rejection reason
```

**7.2.2 Offset commit strategy:**  
Kafka offsets are committed after a task is accepted into the WorkerPool (slot acquired), not after the task completes. This ensures a crashed worker does not re-deliver a task that was already checkpointed. The checkpoint is the durability mechanism; Kafka is the delivery mechanism.

**7.2.3 Work stealing:**  
When a worker is idle (empty queue for > 5 seconds) and another worker's partition is overloaded, the Cluster Manager can reassign a partition temporarily. This is coarse-grained work stealing — partition-level, not task-level. Fine-grained stealing is out of scope.

### 7.3 Execution Protocol

When a worker picks up a task from its queue, it executes via the local ExecutionEngine. The execution protocol is:

```
1. Load checkpoint (if workflow_id has an existing checkpoint in Redis)
2. Emit TASK_STARTED event to Kafka aeos.events topic
3. Execute step via DispatchingExecutor
4. On success:
   a. Checkpoint completed step to Redis
   b. Emit TASK_COMPLETED event
   c. Determine next steps (from ExecutionGraph topology)
   d. Publish next-step tasks to Kafka
5. On failure:
   a. Apply RetryPolicy (backoff)
   b. If retries exhausted → emit TASK_FAILED, publish to DLQ
   c. If circuit breaker opens → emit CIRCUIT_OPEN event, pause agent routing
6. On workflow completion:
   a. Write final WorkflowState to PostgreSQL
   b. Emit WORKFLOW_COMPLETED event
   c. Clean up Redis hot state after TTL (default 24h)
```

### 7.4 Distributed Graph Execution

The ExecutionGraph (DAG of nodes) is compiled on the submitting worker and stored in Redis as a JSON blob keyed by `workflow:{workflow_id}:graph`. Any worker that picks up a step for this workflow reads the graph from Redis to determine dependencies and next nodes.

**7.4.1 Parallel step dispatch:**  
When the graph has a parallel group (multiple nodes at the same topological depth with no dependencies between them), the Distributed Scheduler publishes all parallel-group tasks to Kafka simultaneously. They may be picked up by different workers. Each worker checkpoints its assigned node's result independently. The MergeNode waits until all prerequisite steps are checkpointed (polling Redis with exponential backoff, max 30 retries, max 60 seconds).

**7.4.2 Cross-worker data flow:**  
Step results are stored in Redis: `workflow:{workflow_id}:step:{step_id}:result`. Downstream nodes read from Redis, not from the upstream worker's memory. This allows downstream steps to be executed by any worker.

**7.4.3 Graph garbage collection:**  
When a workflow completes or fails, a background task writes the final state to PostgreSQL and schedules Redis key expiry (TTL = 24 hours by default, configurable per workflow). After TTL, Redis keys are automatically deleted. Cold storage in PostgreSQL is retained for 90 days, then archived to S3.

---

## 8. Distributed Memory

### 8.1 Memory Fabric Architecture

The Phase 9 memory fabric provides coherent access to all four memory tiers across the cluster. The guiding principle is **tier-appropriate consistency**: tiers that need strong guarantees get them; tiers that can tolerate staleness are allowed to be eventually consistent.

### 8.2 Working Memory (Redis Cluster)

Working memory is the most critical shared state. It holds the active context for an ongoing session: recent results, user preferences for the session, tool call history, and agent intermediate outputs.

**8.2.1 Key schema:**

```
wm:{session_id}:{key}           → Value (string, JSON, or binary)
wm:{session_id}:__meta          → JSON: {created_at, last_access, ttl_s, owner_worker}
wm:{session_id}:__keys          → Redis Set: all keys in this session
```

**8.2.2 Access patterns:**

- `get(session_id, key)` → Redis GET, p99 < 2 ms
- `set(session_id, key, value, ttl_s)` → Redis SET with EX, p99 < 5 ms
- `get_all(session_id)` → Redis MGET on all keys in `__keys` set
- `delete(session_id, key)` → Redis DEL + SREM from `__keys`
- `clear_session(session_id)` → Redis EVAL (Lua script to delete all `wm:{session_id}:*`)

**8.2.3 Consistency guarantees:**

Redis Cluster with `requirepass` and `min-replicas-to-write 1` ensures that every write is confirmed on at least one replica before ack. This provides session-level strong consistency for a single key. Cross-key transactions use Redis MULTI/EXEC.

**8.2.4 Session ownership and migration:**

When a worker fails mid-session, the session is not lost — it remains in Redis. The Cluster Manager assigns the session to a new worker. The new worker reads `wm:{session_id}:__meta` to restore session context before continuing.

### 8.3 Long-Term Memory (Vector Store)

Long-term memory stores agent-generated embeddings for semantic retrieval. Phase 9 replaces the in-process vector index with a managed vector database.

**8.3.1 Technology choice:**

The primary target is **Weaviate** (self-hosted on Kubernetes) for on-premises deployments, with **Pinecone** as the managed cloud alternative. The `LongTermMemoryStore` abstract class is implemented by both `WeaviateMemoryStore` and `PineconeMemoryStore`. The deployment chooses one via environment variable `AEOS_LTM_PROVIDER`.

**8.3.2 Data schema:**

```python
@dataclass
class MemoryEntry:
    entry_id: str           # UUID4
    session_id: str         # Session scope
    agent_id: str           # Which agent created this
    content: str            # Original text
    embedding: list[float]  # 1536-dim (OpenAI ada-002) or 768-dim (local)
    metadata: dict          # Arbitrary tags: {task_type, timestamp, quality_score}
    created_at: datetime
    access_count: int       # For LRU eviction
    last_accessed: datetime
```

**8.3.3 Query protocol:**

```
search(query: str, session_id: str, top_k: int = 10, 
       min_score: float = 0.75, filters: dict = {}) → list[MemoryEntry]

1. Embed query via configured embedding model
2. Issue ANN query to vector store with session_id filter
3. Apply metadata filters (post-filter, in-memory on result set)
4. Return top_k results sorted by score descending
5. Update access_count and last_accessed for returned entries
```

**8.3.4 Consistency model:**

Vector stores are eventually consistent. An embedding written by Worker A may not be immediately visible to Worker B. The acceptable staleness window is 1–5 seconds. This is acceptable because long-term memory retrieval is context enrichment, not coordination — a slightly stale retrieval does not cause incorrect behaviour, only slightly less relevant context.

### 8.4 Episodic Memory (PostgreSQL)

Episodic memory records the history of what the system has done: completed workflows, agent decisions, governance outcomes, and user feedback. It is the audit trail and the training data source.

**8.4.1 Schema:**

```sql
-- Episodes table
CREATE TABLE episodes (
    episode_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL,
    workflow_id     UUID NOT NULL,
    agent_id        TEXT NOT NULL,
    action_type     TEXT NOT NULL,   -- "plan", "execute", "review", "approve"
    input_summary   TEXT,
    output_summary  TEXT,
    quality_score   FLOAT,
    governance_decision TEXT,        -- "APPROVED" | "REJECTED" | "ESCALATED"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata        JSONB
);

CREATE INDEX ON episodes (session_id, created_at DESC);
CREATE INDEX ON episodes (workflow_id);
CREATE INDEX ON episodes (agent_id, created_at DESC);

-- Workflow outcomes table
CREATE TABLE workflow_outcomes (
    outcome_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id     UUID NOT NULL UNIQUE,
    status          TEXT NOT NULL,   -- "completed" | "failed" | "cancelled"
    step_count      INT,
    success_steps   INT,
    total_tokens    INT,
    total_latency_ms BIGINT,
    final_result    JSONB,
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

**8.4.2 Write protocol:**

Episodic writes are fire-and-forget from the worker's perspective. Workers publish episodic events to the Kafka topic `aeos.episodic`. A dedicated `episodic-writer` consumer service reads from this topic and writes to PostgreSQL. This decouples the latency of the DB write from the workflow execution path.

**8.4.3 Read protocol:**

Episodic reads are synchronous gRPC calls to the `EpisodicMemoryService`. Reads are served from a PostgreSQL read replica. Read latency SLA: p99 < 50 ms.

### 8.5 Memory Coherence Protocol

**Problem:** Worker A writes a Working Memory key. Worker B reads it 50 ms later. Is B guaranteed to see A's write?

**Answer:** Yes, for Working Memory (Redis), with one caveat: the key must not be on a different Redis shard that has not yet propagated the write to the replica being read from.

**Solution:** All Working Memory reads use `WAIT 1 0` before critical reads (enforces synchronous replication to at least one replica). This adds ~2 ms to read latency but ensures cross-worker coherence for session-critical keys. Non-critical reads (e.g., enrichment context) skip `WAIT` and tolerate replica lag.

The `DistributedWorkingMemory` class exposes this as:
```python
async def get(self, key: str, *, consistent: bool = False) -> Any:
    if consistent:
        await self._redis.execute_command("WAIT", 1, 0)
    return await self._redis.get(self._full_key(key))
```

---

## 9. Event Fabric

### 9.1 Design Goals

The Phase 8 `ExecutionEventBus` is in-process: handlers are Python callables, events are Python objects, and nothing persists beyond the process lifetime. Phase 9 replaces this with a **distributed event fabric** built on Apache Kafka.

The event fabric has three goals:

1. **Decouple producers from consumers.** A worker publishing `WORKFLOW_COMPLETED` does not know or care which services consume it.
2. **Provide a durable, ordered, replayable audit trail.** Every event is stored in Kafka with configurable retention (default 7 days). Any consumer can replay from any offset.
3. **Enable real-time observability.** The Grafana dashboard, the Jaeger trace aggregator, and the episodic memory writer are all Kafka consumers. Adding a new consumer does not require changing any producer.

### 9.2 Topic Architecture

```
Topic naming convention: aeos.{domain}.{subtype}

Topics:
  aeos.tasks.critical          # Priority 1-2 tasks
  aeos.tasks.high              # Priority 3-4
  aeos.tasks.normal            # Priority 5-6 (default)
  aeos.tasks.low               # Priority 7-8
  aeos.tasks.batch             # Priority 9-10
  aeos.tasks.dlq               # Dead letter queue (all priorities)
  
  aeos.events.workflow         # WORKFLOW_* events
  aeos.events.node             # NODE_* events
  aeos.events.agent            # AGENT_* events
  aeos.events.governance       # GOVERNANCE_* events
  aeos.events.memory           # MEMORY_* events
  aeos.events.cluster          # CLUSTER_* events (join, leave, failover)
  
  aeos.traces                  # Execution trace entries (for replay)
  aeos.episodic                # Episodic memory write events
  aeos.metrics                 # High-frequency metrics (for stream processing)
  aeos.audit                   # Immutable audit log (long retention: 90 days)
```

**Topic configuration:**

| Topic Group | Partitions | Replication | Retention | Cleanup Policy |
|-------------|-----------|-------------|-----------|----------------|
| `aeos.tasks.*` | 20 | 3 (min ISR 2) | 24h | delete |
| `aeos.events.*` | 10 | 3 | 7d | delete |
| `aeos.traces` | 20 | 3 | 7d | delete |
| `aeos.episodic` | 5 | 3 | 7d | delete |
| `aeos.metrics` | 10 | 2 | 1h | delete |
| `aeos.audit` | 5 | 3 | 90d | delete |

### 9.3 Event Schema

All events share a common envelope. Domain-specific data lives in the `payload` field.

```python
@dataclass
class DistributedEvent:
    # Envelope (required for all events)
    event_id: str          # UUID4
    event_type: str        # "WORKFLOW_STARTED" | "NODE_COMPLETED" | ...
    topic: str             # Kafka topic name
    partition_key: str     # Usually workflow_id or session_id
    published_at: str      # ISO 8601 UTC
    producer_node_id: str  # node_id of the publishing worker
    schema_version: str    # "1.0" — for forward compatibility
    trace_id: str          # OpenTelemetry trace ID (for Jaeger correlation)
    span_id: str           # OpenTelemetry span ID
    
    # Domain payload (event-specific, JSON-serialized)
    payload: dict
```

**Serialization:** All events are serialized as JSON (not Avro or Protobuf for Phase 9, to minimize dependency footprint). Phase 10 will migrate to Avro with Schema Registry for stronger schema evolution guarantees.

### 9.4 DistributedEventBus Implementation

The `DistributedEventBus` replaces `ExecutionEventBus` in Phase 9. Its interface is backward-compatible: code that calls `event_bus.publish(event_type, payload)` or `event_bus.subscribe(event_type, handler)` does not change.

Internally:

- `publish()` serializes the event and calls `aiokafka.AIOKafkaProducer.send()`. This is fire-and-forget with `acks=1` (leader-only confirmation) for events, and `acks='all'` for audit events.
- `subscribe()` registers a local handler in a consumer group. The `DistributedEventBus` runs a background Kafka consumer loop per subscribed topic.
- Local handlers (Python callables) are invoked in an asyncio task, preserving the Phase 8 async handler contract.

**Producer configuration:**

```python
AIOKafkaProducer(
    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    value_serializer=lambda v: json.dumps(v).encode(),
    compression_type="lz4",          # Good ratio, low CPU
    linger_ms=5,                     # Batch for 5ms before sending
    batch_size=65536,                # 64KB batch
    max_request_size=5_242_880,      # 5MB max (for large payloads)
    acks=1,                          # Per-topic override for audit topics
    enable_idempotence=True,         # Exactly-once producer semantics
)
```

**Consumer configuration:**

```python
AIOKafkaConsumer(
    *subscribed_topics,
    bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
    group_id=f"aeos-worker-{node_id}",
    auto_offset_reset="latest",      # Workers start from now, not history
    enable_auto_commit=False,        # Manual commit after processing
    max_poll_records=100,
    session_timeout_ms=30_000,
    heartbeat_interval_ms=3_000,
)
```

### 9.5 Event Ordering Guarantees

Kafka guarantees ordering within a partition. The partition key determines which partition an event lands in:

- `workflow_id` is used as partition key for all workflow events → all events for a workflow are ordered
- `session_id` is used for memory events → all memory events for a session are ordered
- `node_id` is used for cluster events → cluster events from a single node are ordered

Cross-workflow and cross-session ordering is not guaranteed and not required.

### 9.6 Event Consumption Patterns

**Pattern 1 — Fan-out (broadcast):**  
Multiple independent consumer groups each receive every event. Used by: observability consumers (Prometheus bridge, Jaeger bridge), episodic writer, replay aggregator.

**Pattern 2 — Work queue (competing consumers):**  
Single consumer group with multiple workers. Used by: task execution (aeos-workers group), where each task should be processed by exactly one worker.

**Pattern 3 — Event sourcing projection:**  
A consumer reads from the beginning (offset 0) to reconstruct current state. Used by: the workflow replay API when replaying historical workflows. This is why Kafka retention is 7 days for event topics.

---

## 10. Resource Management

### 10.1 Resource Model

Each worker node has a declared resource capacity:

```python
@dataclass
class NodeCapacity:
    node_id: str
    cpu_cores: float        # Available vCPUs
    memory_gb: float        # Available RAM
    gpu_count: int          # Available GPUs (0 for CPU-only workers)
    max_concurrent_steps: int   # WorkerPool semaphore size
    max_concurrent_llm_calls: int  # Separate semaphore for LLM API calls
    network_bandwidth_mbps: int

@dataclass
class NodeLoad:
    node_id: str
    active_steps: int
    queued_steps: int
    cpu_utilization: float     # 0.0 – 1.0
    memory_utilization: float  # 0.0 – 1.0
    llm_calls_in_flight: int
    sampled_at: datetime
```

Workers report `NodeLoad` to the Cluster Manager every 5 seconds via the heartbeat gRPC call. The Cluster Manager maintains a real-time load table for scheduling decisions.

### 10.2 Admission Control

Before a workflow is accepted by the API gateway, the Cluster Manager performs admission control:

```
1. Check cluster total capacity (sum of max_concurrent_steps across active workers)
2. Check if any worker has capacity for at least 1 step (basic liveness)
3. Check governance pre-validation (rough policy check before full execution)
4. If admits → assign workflow_id, return 202 Accepted
5. If rejects → return 429 Too Many Requests with Retry-After header
```

The Retry-After interval is estimated as `(current_queue_depth / throughput_steps_per_second)`.

### 10.3 Worker Pool Sizing

The `WorkerPool` semaphore size (`max_concurrent_steps`) is set at worker startup from:

```
max_concurrent_steps = min(
    cpu_cores * STEPS_PER_CPU,       # Default: 4 steps per vCPU
    memory_gb * STEPS_PER_GB,        # Default: 2 steps per GB
    MAX_STEPS_HARD_LIMIT             # Default: 50 (prevents runaway)
)
```

For a `c5.xlarge` (4 vCPU, 8 GB): `min(4×4, 8×2, 50) = min(16, 16, 50) = 16` concurrent steps.

### 10.4 LLM Call Rate Limiting

LLM API calls are the primary cost driver and the most common bottleneck. The DRP implements a **token bucket rate limiter** per LLM provider:

```python
class LLMRateLimiter:
    """Token bucket per (provider, model) pair."""
    
    def __init__(self, requests_per_minute: int, tokens_per_minute: int):
        self._req_bucket = TokenBucket(rate=requests_per_minute/60, burst=requests_per_minute//4)
        self._tok_bucket = TokenBucket(rate=tokens_per_minute/60, burst=tokens_per_minute//4)
    
    async def acquire(self, estimated_tokens: int) -> None:
        """Block until both buckets have capacity."""
        await self._req_bucket.consume(1)
        await self._tok_bucket.consume(estimated_tokens)
```

Rate limits are configured per environment and per LLM provider account. Cluster-wide rate limits (shared across all workers) are enforced via Redis atomic counters with a 1-second sliding window.

### 10.5 Backpressure Propagation

The backpressure chain is:

```
LLM API rate limit
  → LLM semaphore full
    → Step executor blocked
      → WorkerPool semaphore full
        → Kafka consumer paused (pause_partitions())
          → Kafka producer at Cluster Manager sees lag
            → API gateway receives 429 from Cluster Manager
              → Client receives Retry-After response
```

This chain ensures that overload at any layer propagates cleanly to the API surface without silent queuing or unbounded memory growth.

### 10.6 Resource Quotas

Per-workflow resource quotas are enforced by the governance gate:

```python
@dataclass
class WorkflowQuota:
    max_steps: int = 100
    max_tokens: int = 500_000
    max_duration_s: int = 3600     # 1 hour hard limit
    max_llm_calls: int = 200
    max_memory_mb: int = 512       # Working memory per workflow
    max_parallel_steps: int = 10   # Concurrent steps
```

Quotas are attached to every `GovernanceGateResult`. The `ExecutionEngine` enforces step count and duration limits at runtime, emitting `QUOTA_EXCEEDED` events and terminating workflows that breach limits.

---

## 11. Capability Federation

### 11.1 What is Capability Federation?

In Phase 8, agent capabilities are strings in a local dict (`kernel._services`). When code calls `kernel.get_service("research_agent")`, it looks up a Python object in memory. This works only because all agents are in the same process.

In Phase 9, agents are distributed across worker nodes. Capability federation solves: **"Which node has an agent that can handle this capability, and how do I route work to it?"**

### 11.2 Capability Registry Design

The Capability Registry is a standalone service (3 instances, consistent hash ring). It maintains a cluster-wide map of capabilities to worker nodes.

**11.2.1 Capability advertisement:**

When a worker's kernel boots and agents are registered, the worker advertises its capabilities to the Capability Registry via gRPC:

```protobuf
service CapabilityRegistry {
  rpc AdvertiseCapabilities(AdvertiseRequest) returns (AdvertiseResponse);
  rpc WithdrawCapabilities(WithdrawRequest) returns (WithdrawResponse);
  rpc LookupCapability(LookupRequest) returns (LookupResponse);
  rpc ListCapabilities(ListRequest) returns (stream CapabilityInfo);
  rpc WatchCapabilities(WatchRequest) returns (stream CapabilityChange);
}

message AdvertiseRequest {
  string node_id = 1;
  string grpc_address = 2;
  repeated CapabilityInfo capabilities = 3;
}

message CapabilityInfo {
  string capability_id = 1;          // e.g., "agent.research"
  string agent_type = 2;             // e.g., "research_agent"
  repeated string tags = 3;          // e.g., ["gpu", "pinned:worker-3"]
  int32 max_concurrent = 4;
  int32 current_load = 5;            // Updated via heartbeat
  float quality_score = 6;          // Historical performance score
}
```

**11.2.2 Capability lookup:**

When the Distributed Scheduler needs to route a step to an agent, it calls:

```
LookupCapability(capability_id="agent.research", 
                 tags_required=["gpu"],
                 strategy=LEAST_LOADED)
```

The Registry returns a ranked list of `(node_id, grpc_address, current_load)` tuples. The Scheduler picks the first entry (least loaded) and publishes the task to that worker's Kafka partition.

**11.2.3 Load balancing strategies:**

| Strategy | Description | Use Case |
|----------|-------------|---------|
| `LEAST_LOADED` | Fewest active steps | Default for all agents |
| `ROUND_ROBIN` | Equal distribution | Stateless tools |
| `PINNED` | Always same node | Browser automation, GPU models |
| `ZONE_AFFINITY` | Prefer same availability zone | Data locality (large inputs) |
| `QUALITY_WEIGHTED` | Prefer historically best performer | Critical tasks |

### 11.3 Capability Taxonomy

Capabilities follow a dot-separated namespace:

```
agent.{agent_type}          → "agent.research", "agent.analyst"
tool.{tool_name}            → "tool.web_search", "tool.code_exec"
llm.{provider}.{model}      → "llm.openai.gpt4o", "llm.anthropic.claude3"
memory.{tier}               → "memory.working", "memory.longterm"
hardware.{resource}         → "hardware.gpu.a100", "hardware.tpu"
```

This taxonomy allows fine-grained routing. A workflow step that requires GPU-accelerated inference specifies `capability: "hardware.gpu"`. The Registry routes only to GPU-capable workers.

### 11.4 Capability Health and Circuit Breaking

The Capability Registry maintains a health score for each capability on each node. When a capability repeatedly fails (as reported by the executing worker via `TASK_FAILED` events), the Registry reduces its quality score and eventually marks it as `DEGRADED`. The circuit breaker in the Distributed Scheduler refuses to route new tasks to a `DEGRADED` capability until it recovers.

Recovery is probed: after `circuit_reset_timeout` (default 60 seconds), the Registry marks the capability `PROBING` and allows one test task. If the test succeeds, the circuit closes and routing resumes normally.

### 11.5 Plugin Capability Registration

Phase 8 introduced the plugin system. In Phase 9, plugins register their capabilities not with the local kernel but with the Capability Registry. A plugin that adds a new agent type calls:

```python
async def initialize(self, kernel: AEOSKernel) -> None:
    await super().initialize(kernel)
    # Register with distributed registry, not just local kernel
    await kernel.capability_registry.advertise([
        CapabilityInfo(
            capability_id="agent.specialized_nlp",
            agent_type="specialized_nlp_agent",
            max_concurrent=5,
            quality_score=1.0,
        )
    ])
```

The `kernel.capability_registry` property returns the `DistributedCapabilityRegistryClient` in Phase 9 and a no-op stub in Phase 8, preserving backward compatibility.

---

## 12. Security Architecture

### 12.1 Threat Model

The DRP threat model assumes:

- The cluster runs in a private Kubernetes namespace. Nodes are not publicly accessible.
- The only public entry point is the API gateway.
- An attacker who compromises one worker node should not be able to read another worker's in-flight task data, impersonate the Cluster Manager, or escalate privileges to the policy service.
- Secrets are never stored in code, environment variables, or Kubernetes manifests.

**Out of scope for this threat model:** physical machine compromise, Kubernetes control plane compromise, and insider attacks on the cloud provider's managed services.

### 12.2 mTLS Everywhere

All gRPC connections between cluster nodes use mutual TLS. Both the client and server present certificates signed by the AEOS internal Certificate Authority (CA).

**Certificate lifecycle:**

```
Internal CA:
  - Self-signed root CA, 10-year validity
  - Stored in HashiCorp Vault PKI secrets engine
  - Never leaves Vault — only CSRs are presented

Node certificates:
  - Generated at node boot via Vault `pki/issue/{role}`
  - 24-hour validity (short-lived, rotation is automatic)
  - SANs: node_id, hostname, k8s service DNS name
  - Rotation: automatic via cert-manager (renews at 50% lifetime)

Service certificates:
  - Generated at service deployment via cert-manager
  - 72-hour validity
  - Stored as Kubernetes Secrets (not ConfigMaps)
```

**gRPC TLS configuration (server side):**

```python
server_credentials = grpc.ssl_server_credentials(
    [(private_key, cert_chain)],
    root_certificates=ca_cert,
    require_client_auth=True,   # mTLS: reject clients without certs
)
```

### 12.3 Authentication

**External API authentication (JWT):**

External clients authenticate with short-lived JWTs (max 1-hour expiry). JWTs are signed with RS256 (RSA 2048-bit). The JWKS endpoint is served by the API gateway.

JWT claims:
```json
{
  "sub": "user:alice@example.com",
  "iss": "https://aeos.example.com",
  "aud": "aeos-api",
  "iat": 1720000000,
  "exp": 1720003600,
  "roles": ["workflow:submit", "memory:read"],
  "quota_tier": "standard"
}
```

**Inter-service authentication (mTLS + service tokens):**

Internal gRPC calls use mTLS for transport authentication. Additionally, each gRPC call carries a short-lived service token in the metadata header `x-aeos-service-token`. Service tokens are issued by Vault for each service identity. This provides double authentication: the TLS cert proves node identity, the service token proves service identity.

**Governance tokens:**

Every task published to the Kafka task queue carries a `governance_token`: a JWT signed by the Policy Service after approving the task. Workers validate this token before executing any step. An unsigned or expired governance token causes immediate task rejection (sent to DLQ).

```json
{
  "sub": "workflow:abc-123",
  "task_id": "step-xyz",
  "approved_at": 1720000000,
  "policy_version": "v2.1",
  "risk_level": "LOW",
  "iss": "aeos-policy-service",
  "exp": 1720003600
}
```

### 12.4 Authorization (RBAC)

RBAC is enforced at the API gateway. The RBAC model:

```
Roles:
  viewer          → read workflows, read memory (own sessions)
  operator        → submit workflows, cancel workflows, read all sessions
  admin           → all operator + manage policies, view audit logs
  system          → internal service identity (all permissions)

Permissions:
  workflow:submit, workflow:cancel, workflow:read
  memory:read, memory:write, memory:delete
  policy:read, policy:write
  cluster:read, cluster:manage
  audit:read
```

Role assignments are stored in PostgreSQL (`rbac_assignments` table) and cached in Redis with a 5-minute TTL. A role change takes effect within 5 minutes without a restart.

### 12.5 Secrets Management

All secrets are managed via HashiCorp Vault (self-hosted on Kubernetes). No secrets exist in:
- Source code (enforced by pre-commit hook + CI check)
- Kubernetes manifests (enforced by admission webhook)
- Environment variables at rest (secrets injected at runtime by Vault Agent)
- Container images (enforced by image scanning in CI)

**Secret categories and Vault paths:**

| Secret | Vault Path | Rotation |
|--------|-----------|----------|
| LLM API keys | `secret/aeos/llm/{provider}` | Manual (provider-dependent) |
| Database credentials | `database/creds/aeos-rw` | Dynamic, 1-hour TTL |
| Redis auth token | `secret/aeos/redis` | 24-hour rotation |
| Kafka credentials | `secret/aeos/kafka` | 24-hour rotation |
| Node TLS certificates | `pki/issue/aeos-node` | 24-hour, auto-rotate |
| Service tokens | `auth/approle/{service}` | 1-hour, auto-rotate |

### 12.6 Data Encryption

**At rest:**
- Redis: encryption at rest via AWS ElastiCache encryption (AES-256)
- Kafka: MSK encryption at rest (AES-256)
- PostgreSQL: RDS encryption at rest (AES-256, KMS-managed key)
- S3 (cold storage): SSE-S3 (AES-256)

**In transit:**
- All gRPC: TLS 1.3
- All HTTP: TLS 1.3 minimum (TLS 1.2 deprecated)
- Kafka: TLS 1.2+ (MSK default)
- Redis: TLS 1.2+ (ElastiCache in-transit encryption)

**Application-level encryption:**
Sensitive payloads in Kafka (governance decisions, user PII in episodic memory) are encrypted at the application level with AES-256-GCM before publishing. The encryption key is fetched from Vault at producer startup. Consumers decrypt on read.

### 12.7 Audit Logging

Every governance decision, every policy evaluation, and every RBAC check produces an immutable audit log entry published to `aeos.audit` (90-day Kafka retention) and written to PostgreSQL (`audit_log` table).

```python
@dataclass
class AuditEntry:
    audit_id: str            # UUID4
    timestamp: str           # ISO 8601 UTC
    event_type: str          # "GOVERNANCE_DECISION" | "RBAC_CHECK" | "SECRET_ACCESS"
    actor: str               # User or service identity
    resource: str            # What was accessed/modified
    action: str              # "APPROVED" | "REJECTED" | "READ" | "WRITE"
    outcome: str             # "SUCCESS" | "DENIED"
    reason: str              # Human-readable explanation
    policy_version: str      # Which policy version was applied
    trace_id: str            # Correlation to workflow trace
    metadata: dict           # Arbitrary context
```

Audit entries are append-only. The `audit_log` table has no `UPDATE` or `DELETE` permissions granted to any service identity. Retention is enforced by scheduled archival (not deletion) to S3.

---

## 13. Governance & Policy Engine

### 13.1 Phase 8 Governance vs. Phase 9 Governance

Phase 8 governance is a local function call: the `GovernanceGate` runs inside the execution engine, evaluates rules in memory, and returns synchronously. Phase 9 governance is a distributed service with its own deployment unit, its own scaling, and its own audit trail.

The Phase 9 governance model is:

- **Pre-task governance:** Before a task is published to the Kafka task queue, the submitting service calls the Policy Service. The Policy Service evaluates the task against all applicable policies and returns a signed governance token.
- **Pre-step governance:** Before each step in a workflow executes, the worker validates the governance token (JWT signature + not-expired). This is a local check (< 1 ms), not a remote call.
- **Post-step governance:** After each step completes, the worker sends the step result to the Policy Service for audit (fire-and-forget, non-blocking).

### 13.2 Policy Service Design

The Policy Service is a stateless gRPC service backed by PostgreSQL for policy storage.

```protobuf
service PolicyService {
  rpc EvaluateTask(TaskEvaluationRequest) returns (TaskEvaluationResponse);
  rpc EvaluateStep(StepEvaluationRequest) returns (StepEvaluationResponse);
  rpc AuditStep(StepAuditRequest) returns (AuditResponse);
  rpc GetPolicy(GetPolicyRequest) returns (Policy);
  rpc UpdatePolicy(UpdatePolicyRequest) returns (Policy);  // admin only
  rpc ListPolicies(ListPoliciesRequest) returns (stream Policy);
}
```

**Policy structure:**

```python
@dataclass
class Policy:
    policy_id: str
    version: str
    name: str
    description: str
    scope: str           # "global" | "workflow_type:{type}" | "agent:{agent_type}"
    priority: int        # Lower number = higher priority
    conditions: list[PolicyCondition]
    actions: list[PolicyAction]
    enabled: bool
    created_by: str
    created_at: datetime
    updated_at: datetime

@dataclass
class PolicyCondition:
    field: str           # JSONPath into the task/step context
    operator: str        # "eq" | "ne" | "gt" | "lt" | "in" | "regex"
    value: Any

@dataclass
class PolicyAction:
    action: str          # "APPROVE" | "REJECT" | "ESCALATE" | "RATE_LIMIT" | "REDACT"
    reason: str
    parameters: dict     # Action-specific params
```

### 13.3 Policy Evaluation Algorithm

```
evaluate_task(task):
  1. Load all enabled policies sorted by priority
  2. Evaluate conditions top-to-bottom (short-circuit on first match)
  3. If a REJECT policy matches → return REJECTED with reason
  4. If an ESCALATE policy matches → pause, send for human review
  5. If all APPROVE conditions satisfied → sign governance token, return APPROVED
  6. Default: APPROVED (allowlist model with safety net policies)
  
  Timeout: 30 ms budget (SLA). If exceeded, fall back to APPROVE with DEGRADED flag.
  The DEGRADED flag triggers enhanced post-step auditing.
```

### 13.4 Policy Hot Reload

Policies are stored in PostgreSQL. The Policy Service caches them in memory (TTL = 60 seconds). When an admin updates a policy via the API, the Policy Service is notified via a Kafka event (`aeos.events.governance` topic, event type `POLICY_UPDATED`). All Policy Service instances consume this event and invalidate their cache immediately.

This provides near-instant policy propagation (< 1 second) without requiring a service restart.

### 13.5 Human Escalation

When a task triggers an `ESCALATE` policy action:

1. Policy Service creates an escalation record in PostgreSQL
2. Sends a notification (webhook / email) to the designated escalation contact
3. Returns `PENDING_APPROVAL` to the Distributed Scheduler
4. Scheduler parks the task in `aeos.tasks.escalated` topic
5. When the human approves/rejects via API, Policy Service updates the record
6. Scheduler picks up the approved task and publishes to the appropriate priority queue

Escalation timeout: 24 hours (configurable). After timeout, the task is auto-rejected.

---

## 14. Observability Platform

### 14.1 Three Pillars

The DRP implements the full three-pillar observability model:

| Pillar | Technology | Granularity |
|--------|-----------|-------------|
| Metrics | Prometheus + Grafana | Per-second, per-node, per-capability |
| Logs | Structured JSON → Kafka → Elasticsearch | Per-event, per-step |
| Traces | OpenTelemetry → Jaeger | Per-workflow, end-to-end |

### 14.2 Metrics

Every worker exposes `/metrics` in Prometheus text format. The Prometheus server scrapes all workers every 15 seconds.

**Canonical metric names:**

```
# Workflow metrics
aeos_workflow_submitted_total{status="accepted|rejected"}
aeos_workflow_completed_total{status="completed|failed|cancelled"}
aeos_workflow_duration_seconds{quantile="0.5|0.95|0.99"}
aeos_workflow_steps_total{workflow_id="...", status="completed|failed"}

# Step metrics
aeos_step_duration_seconds{agent_type="...", quantile="0.5|0.95|0.99"}
aeos_step_queue_depth{priority="critical|high|normal|low|batch"}
aeos_step_retry_total{agent_type="...", attempt="1|2|3"}

# Agent metrics
aeos_agent_calls_total{agent_type="...", status="success|failure"}
aeos_agent_llm_tokens_total{agent_type="...", provider="openai|anthropic"}
aeos_agent_active_sessions{agent_type="..."}

# Cluster metrics
aeos_cluster_workers_active
aeos_cluster_workers_draining
aeos_cluster_capabilities_total{status="healthy|degraded|offline"}

# Infrastructure metrics
aeos_redis_latency_seconds{operation="get|set|multi_exec", quantile="0.5|0.95|0.99"}
aeos_kafka_consumer_lag{topic="...", partition="..."}
aeos_kafka_publish_latency_seconds{topic="...", quantile="0.5|0.95|0.99"}
```

### 14.3 Distributed Tracing

Every workflow execution is a distributed trace. The trace ID is generated when the workflow is submitted at the API gateway and propagated through every gRPC call, every Kafka message header, and every log entry.

**Trace spans:**

```
Workflow trace (root span):
  ├── API validation span
  ├── Governance evaluation span
  ├── Scheduler dispatch span
  └── Execution span (per-worker)
      ├── Node execution span (per-node)
      │   ├── Checkpoint read span
      │   ├── Agent execution span
      │   │   ├── LLM call span (per call)
      │   │   └── Tool call span (per call)
      │   ├── Checkpoint write span
      │   └── Event publish span
      └── Result aggregation span
```

OpenTelemetry SDK is initialized in each worker process:

```python
from opentelemetry import trace
from opentelemetry.exporter.jaeger.thrift import JaegerExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

tracer_provider = TracerProvider()
jaeger_exporter = JaegerExporter(
    agent_host_name=JAEGER_AGENT_HOST,
    agent_port=6831,
)
tracer_provider.add_span_processor(BatchSpanProcessor(jaeger_exporter))
trace.set_tracer_provider(tracer_provider)
```

### 14.4 Log Architecture

Workers emit structured JSON logs to stdout. Kubernetes forwards these to Fluent Bit, which ships them to Kafka (`aeos.logs` topic). A log consumer service reads from Kafka and indexes into Elasticsearch. Kibana provides the search UI.

**Structured log format:**

```json
{
  "timestamp": "2026-07-06T12:00:00.000Z",
  "level": "INFO",
  "logger": "aeos.execution.engine",
  "message": "Step completed",
  "trace_id": "abc123",
  "span_id": "def456",
  "node_id": "worker-node-3",
  "workflow_id": "wf-789",
  "step_id": "step-001",
  "agent_type": "research_agent",
  "duration_ms": 1234,
  "tokens_used": 5000,
  "status": "completed"
}
```

No `printf`-style log messages. No string concatenation. Every log call uses structured key-value fields.

### 14.5 Alerting

Prometheus AlertManager rules:

```yaml
groups:
  - name: aeos.cluster
    rules:
      - alert: WorkerNodeDown
        expr: aeos_cluster_workers_active < 2
        for: 1m
        severity: critical
        
      - alert: QueueDepthHigh
        expr: aeos_step_queue_depth{priority="critical"} > 100
        for: 5m
        severity: warning
        
      - alert: StepLatencyHigh
        expr: histogram_quantile(0.99, aeos_step_duration_seconds) > 10
        for: 5m
        severity: warning
        
      - alert: KafkaConsumerLagHigh
        expr: aeos_kafka_consumer_lag > 10000
        for: 2m
        severity: critical
        
      - alert: CircuitBreakerOpen
        expr: aeos_cluster_capabilities_total{status="degraded"} > 0
        for: 1m
        severity: warning
        
      - alert: GovernanceGateSlowdown
        expr: histogram_quantile(0.99, aeos_governance_duration_seconds) > 0.5
        for: 3m
        severity: warning
```

### 14.6 Grafana Dashboards

Three canonical dashboards:

**Dashboard 1 — Cluster Overview:**  
Active workers, total queue depth by priority, workflow throughput (submitted/completed/failed per minute), p99 step latency, Kafka consumer lag heatmap.

**Dashboard 2 — Workflow Detail:**  
Per-workflow view. Input: `workflow_id`. Shows: step-by-step Gantt chart (from trace spans), per-step latency, retry count, governance decisions, token usage.

**Dashboard 3 — Agent Performance:**  
Per-agent-type breakdown. Success rate, p50/p95/p99 latency, token consumption, quality score distribution, circuit breaker state.

---

## 15. Cloud Architecture

### 15.1 Target Platform

The primary production deployment target is **AWS + EKS**. All managed services use AWS equivalents. The design is cloud-agnostic at the application layer — swapping EKS for GKE or AKS requires only changing the Kubernetes manifests and managed service endpoints.

### 15.2 AWS Service Mapping

| AEOS Component | AWS Service | Configuration |
|---------------|-------------|---------------|
| Worker nodes | EKS (EC2 nodegroup) | `c5.2xlarge`, spot + on-demand mix |
| Cluster Manager | EKS (EC2 StatefulSet) | `t3.medium`, on-demand only |
| Redis Cluster | ElastiCache for Redis | `r6g.large`, cluster mode, 3 shards |
| Kafka | Amazon MSK | `kafka.m5.large`, 3 brokers, 3 AZs |
| PostgreSQL | RDS PostgreSQL 16 | `db.r6g.large`, Multi-AZ |
| Vector store | self-hosted Weaviate on EKS | `r5.xlarge` nodes |
| Object storage | S3 | Standard class (hot), Glacier (archive) |
| Secrets | HashiCorp Vault on EKS | HA mode, 3 nodes |
| Load balancer | AWS ALB (Ingress) | HTTPS, WAF-enabled |
| Certificate management | cert-manager + Vault PKI | Auto-rotate 24h |
| Container registry | ECR | Image scanning enabled |
| CI/CD | GitHub Actions → ECR → EKS | GitOps via ArgoCD |

### 15.3 VPC Architecture

```
VPC: 10.0.0.0/16

Subnets:
  Public (3 AZs):   10.0.0.0/24, 10.0.1.0/24, 10.0.2.0/24
    → ALB, NAT Gateways
    
  Private-App (3 AZs): 10.0.10.0/23, 10.0.12.0/23, 10.0.14.0/23
    → EKS worker nodes
    
  Private-Data (3 AZs): 10.0.20.0/24, 10.0.21.0/24, 10.0.22.0/24
    → ElastiCache, RDS, MSK

Security Groups:
  sg-alb:         inbound 443 from 0.0.0.0/0
  sg-workers:     inbound 50051 from sg-workers (gRPC mesh)
                  inbound 8000 from sg-alb
                  outbound all to sg-data
  sg-data:        inbound 6379 from sg-workers (Redis)
                  inbound 9092 from sg-workers (Kafka)
                  inbound 5432 from sg-workers (Postgres)
                  no outbound to public internet
```

### 15.4 Kubernetes Resource Manifests (Specifications)

**Worker Deployment spec (abbreviated):**

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
      maxUnavailable: 0    # Zero downtime
  template:
    spec:
      terminationGracePeriodSeconds: 60   # Allow drain
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
```

**Horizontal Pod Autoscaler:**

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: aeos-worker-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: aeos-worker
  minReplicas: 3
  maxReplicas: 20
  metrics:
    - type: External
      external:
        metric:
          name: kafka_consumer_lag_sum
          selector:
            matchLabels:
              topic: aeos.tasks.normal
        target:
          type: AverageValue
          averageValue: "500"
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 70
```

### 15.5 Multi-Region Strategy

Phase 9 targets a single-region, multi-AZ deployment. Multi-region is planned for Phase 10. However, the architecture is designed with multi-region in mind:

- All workflow state is stored in Redis and PostgreSQL (not on node disk)
- Kafka topics have configurable replication across AZs
- No node has a fixed IP or hostname hardcoded anywhere
- The Cluster Manager uses Raft, which supports geographic distribution (higher latency elections, same correctness)

For Phase 9, cross-AZ latency is the primary concern. EKS worker nodes are placed across 3 AZs. The Kafka brokers are pinned to one AZ each. Redis Cluster shards are distributed across AZs. Network egress costs for cross-AZ traffic are managed by the zone affinity capability routing strategy.

---

## 16. Failure Analysis & Resilience

### 16.1 Failure Taxonomy

The DRP classifies failures by blast radius and recovery complexity:

| Class | Example | Detection | Recovery |
|-------|---------|-----------|---------|
| Transient | Network hiccup, LLM API timeout | Retry policy | Automatic (< 1s) |
| Node failure | Worker process crash, OOM | Heartbeat timeout (15s) | Automatic (< 60s) |
| Partition failure | Kafka broker loss | Kafka health check | Automatic (Kafka rebalance) |
| Data store failure | Redis primary failure | Sentinel monitoring | Automatic (< 30s failover) |
| Leader failure | Cluster Manager leader crash | Raft election timeout | Automatic (< 15s) |
| Cascade failure | Queue saturation → worker exhaustion | Queue depth alert | Backpressure + manual scale |
| Split brain | Network partition → 2 cluster halves | Cluster Manager isolation | Manual intervention required |
| Data corruption | Redis bit flip, Kafka message corruption | Checksum validation | Replay from last valid checkpoint |

### 16.2 Worker Node Failure

**Detection:**  
Cluster Manager sees no heartbeat for 15 seconds → marks node "suspected". After 30 seconds → marks "dead".

**Recovery:**  
1. Cluster Manager notifies Capability Registry to remove dead node's capabilities
2. Kafka partition reassignment (automatic, handled by Kafka group coordinator)
3. Any in-flight steps on the dead node are in one of two states:
   - **Checkpointed**: The last checkpoint is in Redis. The next worker to pick up the workflow reads the checkpoint and continues from the last completed node.
   - **Not checkpointed (step in progress)**: The step was not committed. The task is still in Kafka (offset was committed on acceptance, but the next-step task was not published). The Cluster Manager detects the orphaned workflow (no heartbeat from owner node + no WORKFLOW_COMPLETED event) and re-publishes the in-progress step to the task queue.

**Orphaned workflow detection:**  
A background job on the Cluster Manager scans Redis for workflows with `status=RUNNING` and `last_heartbeat > 30s ago`. These are the in-flight workflows on the dead node. Each orphaned workflow is re-queued with `retry_attempt++`.

### 16.3 Redis Failure

**Sentinel-based automatic failover:**  
ElastiCache for Redis is configured with Sentinel mode (1 primary, 2 replicas, 3 Sentinel nodes). When the primary fails, Sentinel elects a new primary within 30 seconds.

**Failure window:**  
During the 30-second Sentinel election, Redis writes fail. Workers detect this via `ConnectionError` and pause checkpoint writes (executing steps continue, but are not checkpointed). Workers buffer up to 10 checkpoint writes in memory. When Redis recovers, buffered checkpoints are flushed.

**If Redis is unavailable for > 60 seconds:**  
Workers enter a degraded mode: new steps are not accepted (API returns 503). In-flight steps continue until completion but are not checkpointed. This is a known risk; the RTO for Redis failover (30s) is within acceptable bounds.

### 16.4 Kafka Failure

MSK with 3 brokers across 3 AZs and replication factor 3 (min ISR 2). A single broker failure causes no data loss and no interruption (ISR remains at 2).

If 2 brokers fail simultaneously (catastrophic):
- All Kafka publishes fail (`NotLeaderForPartitionError`)
- Workers fall back to SQS for task dispatch (fallback queue, always warm)
- Events and traces are buffered in a local `DiskEventBuffer` (ring buffer, max 100K events, stored to local disk)
- When Kafka recovers, the `DiskEventBuffer` is flushed to Kafka

The SQS fallback adds latency (~100 ms vs ~5 ms for Kafka) but maintains correctness.

### 16.5 Network Partition (Split Brain)

If the cluster is partitioned into two halves that cannot communicate:

- The **majority partition** (has Raft quorum) continues operating normally
- The **minority partition** detects loss of leader heartbeat, attempts election, but cannot reach quorum → stays leaderless → workers in the minority partition stop accepting new workflows

This is the standard Raft safety guarantee: no split-brain. The minority partition may have workflows that were in-flight when the partition occurred. These workflows will be paused (no new steps accepted) and will resume when the partition heals.

**Healing:**  
When the network partition heals, the minority partition workers re-join the cluster via the standard join protocol. Paused workflows are re-queued.

### 16.6 Cascading Failure Prevention

**Backpressure chain (§10.5)** is the first line of defense against cascades.

**Bulkhead pattern:**  
Each agent type has a dedicated sub-semaphore within the WorkerPool. A runaway `research_agent` (e.g., all slots blocked on a slow LLM call) cannot starve `executor_agent` tasks.

```python
class BulkheadWorkerPool:
    def __init__(self, total_slots: int, per_agent_max: dict[str, int]):
        self._total = asyncio.Semaphore(total_slots)
        self._agent_limits = {
            agent: asyncio.Semaphore(max_slots)
            for agent, max_slots in per_agent_max.items()
        }
```

**Circuit breaker at the LLM layer:**  
If an LLM provider returns errors for > 5 consecutive calls, the circuit opens. All subsequent calls for that provider are immediately rejected with `CircuitOpenError`. Agents fall back to their secondary LLM provider. This prevents the LLM timeout waterfall (one slow LLM call blocks one step; 50 slow calls block 50 steps; all workers starve).

### 16.7 Chaos Engineering Plan

Before Phase 9 goes to production, the following chaos experiments are required:

| Experiment | Method | Pass Criterion |
|-----------|--------|---------------|
| Worker kill | `kubectl delete pod aeos-worker-{n}` | No workflow loss, recovery < 60s |
| Redis primary kill | `DEBUG SLEEP 100` on primary | Failover < 30s, no data loss |
| Kafka broker kill | Stop MSK broker | Rebalance < 10s, no message loss |
| Leader kill | `kubectl delete pod aeos-cluster-manager-0` | New leader < 15s |
| Network partition | tc netem on worker NICs | Minority stays leaderless, majority continues |
| LLM API degradation | Latency injection to LLM endpoints | Circuit opens, fallback activates |
| Queue saturation | Flood 10K tasks simultaneously | 429 at API, graceful backpressure |
| Memory pressure | `stress-ng --vm 1 --vm-bytes 7G` | OOM → pod restart → recovery |

---

## 17. Performance Engineering

### 17.1 Critical Path Analysis

The critical path for an interactive workflow request:

```
API gateway receive request         5 ms
  JWT validation (cached JWKS)      1 ms
  Request parsing + validation      2 ms
  Governance pre-check (gRPC)      20 ms
  Kafka publish (task)              5 ms
  Kafka consumer poll               5 ms
  Checkpoint load (Redis GET)       3 ms
  Agent execution (LLM call)     1000 ms  ← dominant
  Checkpoint write (Redis SET)      5 ms
  Next-step Kafka publish           5 ms
  ──────────────────────────────────────
  Total (single step)            1051 ms
```

LLM API latency dominates. The engineering focus is therefore on:
1. Minimizing overhead around the LLM call (< 50 ms total non-LLM overhead per step)
2. Maximizing parallelism (running as many steps concurrently as safely possible)
3. LLM response caching for identical prompts

### 17.2 LLM Response Caching

Deterministic LLM calls (same model, same temperature=0, same prompt) can be cached. The cache key is `sha256(model + prompt + temperature + max_tokens)`. Cache storage is Redis with TTL = 1 hour.

Cache hit rate target: 15–20% (based on observed repetition in research and analysis workflows). A 15% cache hit rate reduces LLM costs by 15% and reduces p99 step latency proportionally.

```python
class LLMCache:
    async def get(self, cache_key: str) -> str | None:
        return await self._redis.get(f"llm:cache:{cache_key}")
    
    async def set(self, cache_key: str, response: str, ttl_s: int = 3600) -> None:
        await self._redis.set(f"llm:cache:{cache_key}", response, ex=ttl_s)
```

### 17.3 Connection Pooling

All external connections use connection pools:

| Connection Type | Pool Library | Min | Max | Timeout |
|----------------|-------------|-----|-----|---------|
| Redis | `redis.asyncio` ConnectionPool | 5 | 50 | 5s |
| PostgreSQL | `asyncpg` Pool | 5 | 20 | 10s |
| gRPC (to Capability Registry) | gRPC channel reuse | 1 per target | N/A | 5s |
| gRPC (to Policy Service) | gRPC channel reuse | 1 per target | N/A | 5s |
| HTTP (LLM providers) | `httpx.AsyncClient` limits | 5 | 50 per provider | 30s |

### 17.4 Python Async Architecture

All I/O operations in the DRP are async (asyncio). Blocking operations are prohibited in the main event loop. CPU-bound operations (embedding, heavy JSON parsing, crypto) are dispatched to a `ThreadPoolExecutor` or `ProcessPoolExecutor`.

**Rules:**
- No `time.sleep()` anywhere (use `asyncio.sleep()`)
- No `requests` library (use `httpx.AsyncClient`)
- No synchronous Redis calls (use `redis.asyncio`)
- No `subprocess.check_output()` in hot paths (use `asyncio.create_subprocess_exec()`)

### 17.5 Serialization Performance

JSON serialization is the dominant CPU cost for event publishing and checkpoint writing. Performance targets:

- Checkpoint serialize/deserialize: < 2 ms for a 10-step workflow state
- Event serialize: < 0.5 ms per event

Achieved by:
- Using `orjson` instead of stdlib `json` (5–10× faster for small objects)
- Pre-serializing common schemas to avoid repeated introspection
- Avoiding nested dataclass serialization in hot paths (flatten to dicts at the boundary)

### 17.6 Memory Optimization

Target: < 500 MB baseline RSS per worker, < 2 GB peak RSS under full load.

Key controls:
- Agent context windows are capped at `max_context_tokens` (configurable, default 32K)
- Workflow state in memory is bounded by the WorkerPool semaphore (max 50 concurrent steps × average state size)
- The in-process `DiskEventBuffer` (chaos fallback) is bounded at 100K events
- Python garbage collection is tuned: `gc.set_threshold(700, 10, 10)` (reduce GC pauses)
- Large binary payloads (embeddings, images) are passed by reference (Redis key) between steps, not serialized into the workflow state

---

## 18. Testing Strategy

### 18.1 Test Pyramid

```
                    ┌─────┐
                    │ E2E │  5% — Full cluster, real services
                   ┌┴─────┴┐
                   │  Int  │  25% — Service boundaries, Docker compose
                  ┌┴───────┴┐
                  │  Unit   │  70% — Pure functions, mocked I/O
                  └─────────┘
```

### 18.2 Unit Testing

Every module in Phase 9 has a corresponding unit test file. Unit tests must:

- Run without any network access (fully mocked Redis, Kafka, gRPC)
- Complete in < 100 ms per test
- Cover all failure paths, not just the happy path
- Use `pytest-asyncio` for async test functions
- Achieve > 90% branch coverage

**Key unit test categories:**

| Category | What to Test |
|---------|-------------|
| Raft leader election | Term increments, vote counting, heartbeat timeout, split vote |
| Distributed Scheduler | Offset commit timing, backpressure triggering, governance token rejection |
| Redis checkpoint | MULTI/EXEC atomicity (mocked), eviction, TTL expiry |
| Circuit breaker | CLOSED→OPEN→HALF_OPEN state transitions, probe success/failure |
| Policy evaluation | Condition matching, priority ordering, ESCALATE branching |
| mTLS certificate validation | Expired cert rejection, missing cert rejection, valid cert acceptance |
| Event fabric | Producer batching, consumer lag, DLQ routing |

### 18.3 Integration Testing

Integration tests use Docker Compose to spin up real Redis, Kafka, and PostgreSQL instances. They test:

- Worker join/leave protocol with a real Cluster Manager
- Workflow execution across 2 workers (one submits, one executes)
- Redis checkpoint write/read cycle under concurrent load
- Kafka event publish/consume with real partition assignment
- Policy Service evaluation with real PostgreSQL-backed policies
- Capability Registry advertisement and lookup

Integration test environment:
```yaml
# docker-compose.test.yml
services:
  redis:
    image: redis:7.2-alpine
    command: redis-server --requirepass test_password
  kafka:
    image: confluentinc/cp-kafka:7.5.0
    environment:
      KAFKA_AUTO_CREATE_TOPICS_ENABLE: "true"
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: aeos_test
  worker-1:
    build: .
    environment:
      AEOS_NODE_ROLE: worker
  worker-2:
    build: .
    environment:
      AEOS_NODE_ROLE: worker
```

### 18.4 End-to-End Testing

E2E tests run against a full EKS cluster (staging environment). They test:

- Cold cluster start: all workers join within 30 seconds
- Submit 100 concurrent workflows: all complete, no loss
- Kill 1 worker mid-execution: orphaned workflows resume within 60 seconds
- Kill Redis primary: ElastiCache failover occurs, workflows pause and resume
- Policy change: new policy propagates to all workers within 5 seconds
- HPA scaling: new workers join cluster and receive tasks within 2 minutes

E2E tests run in CI on every release candidate (not on every PR — too slow).

### 18.5 Performance Testing

Performance tests use Locust to generate load against the API gateway:

```
Load profile (sustained):
  - 100 concurrent users
  - 10 workflow submissions/second
  - Mix: 60% single-step, 30% 5-step pipeline, 10% 20-step parallel

Assertions:
  - p99 submission latency < 200 ms
  - p99 single-step execution < 5 s
  - Error rate < 0.1%
  - No memory leak over 30-minute sustained run (RSS growth < 100 MB)
```

Performance tests run weekly against the staging environment.

### 18.6 Chaos Testing

See §16.7. Chaos tests are run monthly against staging and quarterly against a production clone.

### 18.7 Security Testing

- SAST: Bandit (Python), Semgrep rules for common vulnerabilities, run in CI on every PR
- Dependency scanning: `safety check` + Snyk in CI
- Secret scanning: `detect-secrets` pre-commit hook + GitGuardian in CI
- Penetration testing: Annual external pentest (Phase 10 before GA)
- mTLS verification: Custom test verifies that unauthenticated gRPC calls are rejected

---

## 19. Migration Strategy

### 19.1 Migration Objectives

The migration from Phase 8 (single-node) to Phase 9 (distributed) must:

1. Preserve all existing API contracts (no breaking changes to `/api/v1/` endpoints)
2. Enable gradual rollout (single-node → 2-node → N-node without downtime)
3. Provide rollback at any stage (Phase 9 → Phase 8 in < 15 minutes)
4. Migrate existing workflow history (PostgreSQL) and long-term memory (vector store) without data loss

### 19.2 Migration Phases

**Phase M1 — Infrastructure provisioning (Week 1):**  
Provision managed services: ElastiCache cluster, MSK cluster, RDS instance, EKS cluster. No application changes. All services run empty. Validate connectivity, security groups, mTLS setup.

**Phase M2 — Dual-mode worker (Week 2-3):**  
Release a Phase 9-capable worker binary that boots in **single-node mode** (Phase 8 defaults) when `AEOS_CLUSTER_MODE=false`. This binary is deployed to production, replacing the Phase 8 binary. All existing functionality works identically. No risk — same logic, new binary.

**Phase M3 — Distributed adapters enabled (Week 4):**  
Set `AEOS_CLUSTER_MODE=true` on the single running worker. Worker now connects to Redis, Kafka, and the Cluster Manager. Workflows still execute on this single worker. Validation: checkpoints appear in Redis, events appear in Kafka, metrics appear in Prometheus.

**Phase M4 — Second worker (Week 5):**  
Deploy a second worker node. Cluster Manager now has 2 active workers. Validate: Kafka partition rebalance occurs, capabilities are advertised by both workers, new workflows are distributed across both workers.

**Phase M5 — Full cluster (Week 6):**  
Scale to target worker count (e.g., 5 workers). Enable HPA. Validate: autoscaling responds to load, worker failures recover automatically.

**Phase M6 — Data migration (Week 7-8):**  
Run the data migration jobs:
- Export Phase 8 in-memory long-term memory (if persisted to disk) → import to Weaviate
- Ensure PostgreSQL episodic data is accessible to the new `EpisodicMemoryService`
- Validate: historical workflow replay works via the Replay API

**Phase M7 — Production cutover (Week 9):**  
Remove Phase 8 fallback paths (`if AEOS_CLUSTER_MODE=false` branches). Declare Phase 9 GA.

### 19.3 Rollback Plan

At each migration phase, the rollback procedure is:

| Phase | Rollback Action | Time Required |
|-------|----------------|---------------|
| M1 | Tear down infra (no app change) | 15 min |
| M2 | Redeploy Phase 8 binary | 5 min (Kubernetes rollout undo) |
| M3 | Set `AEOS_CLUSTER_MODE=false`, restart | 2 min |
| M4 | Scale second worker to 0 | 1 min |
| M5 | Scale to 1 worker, disable HPA | 2 min |
| M6 | Revert memory clients to Phase 8 | 5 min |
| M7 | No clean rollback (Phase 9 GA) | N/A |

### 19.4 Backward Compatibility Guarantees

**API compatibility:**  
All `/api/v1/` endpoints retain the same request/response schemas. Phase 9 adds new endpoints (`/api/v1/cluster/*`, `/api/v1/capabilities/*`) but does not modify existing ones.

**SDK compatibility:**  
The Python `aeos-client` SDK does not require changes for Phase 9. The SDK calls the same API endpoints.

**Plugin compatibility:**  
Phase 8 plugins that call `kernel.register_service()`, `kernel.get_service()`, and `kernel.publish_event()` work without modification. The implementations behind these calls change (distributed vs. local), but the interface is preserved.

**Environment variable compatibility:**  
All Phase 8 environment variables continue to work. Phase 9 adds new variables prefixed with `AEOS_CLUSTER_*`. The single-node mode (`AEOS_CLUSTER_MODE=false`) is the default for the Phase M2 binary.

### 19.5 Migration Testing

Before each migration phase, a full integration test suite runs against a staging clone. The staging clone mirrors production configuration exactly. Promotion to production requires:

1. All integration tests pass on staging
2. No open P1 bugs on the current phase
3. On-call engineer available during cutover window
4. Rollback procedure documented and rehearsed in staging

---

## 20. Implementation Roadmap

### 20.1 Phase 9 Work Breakdown

Phase 9 is split into 6 implementation milestones. Each milestone is independently deployable and testable.

### 20.2 Milestone 9B-1: Infrastructure & Scaffolding (3 weeks)

**Deliverables:**
- `aeos-worker` multi-mode entrypoint (`app/worker_main.py`)
- `AEOS_CLUSTER_MODE` feature flag infrastructure
- Redis adapter: `RedisCheckpointStore`, `DistributedWorkingMemory`
- Kafka adapter: `KafkaEventBus`, `KafkaTraceStore`
- Docker Compose for local cluster development (2 workers + Redis + Kafka + Postgres)
- GitHub Actions CI pipeline for Phase 9 tests
- All unit tests for adapters (mocked infrastructure)

**Dependencies:** None (can start immediately after this RFC is approved)

**Success gate:** `aeos-worker` starts in both `AEOS_CLUSTER_MODE=false` (Phase 8 identical) and `AEOS_CLUSTER_MODE=true` (connects to Redis/Kafka, advertises capabilities to a mock registry)

### 20.3 Milestone 9B-2: Cluster Manager (4 weeks)

**Deliverables:**
- `aeos-cluster-manager` service (`app/cluster/manager.py`)
- Raft leader election (single-process simulation for unit tests, real multi-node for integration)
- Cluster membership table (Redis-backed for simplicity; full Raft log replication in 9B-3)
- Worker join/leave/failure detection protocol
- gRPC API: `JoinCluster`, `LeaveCluster`, `Heartbeat`, `GetTopology`
- Kubernetes StatefulSet manifest for Cluster Manager (3 replicas)
- Chaos tests: kill leader, kill follower

**Dependencies:** 9B-1 (Redis adapter, Docker Compose environment)

**Success gate:** 3-node Cluster Manager cluster elects a leader within 5 seconds of cold start. Leader failure triggers re-election within 15 seconds.

### 20.4 Milestone 9B-3: Distributed Scheduler (3 weeks)

**Deliverables:**
- `DistributedScheduler` replacing `LocalScheduler` in the kernel
- Kafka consumer group management (partition assignment via Cluster Manager)
- Work stealing (partition rebalance on worker join/leave)
- SQS fallback queue for Kafka unavailability
- Governance token validation in the scheduler hot path
- Backpressure: pause Kafka consumer when WorkerPool saturated
- Dead letter queue routing for invalid tasks
- Scheduler metrics (`aeos_step_queue_depth`, `aeos_kafka_consumer_lag`)

**Dependencies:** 9B-1 (Kafka adapter), 9B-2 (Cluster Manager for partition assignments)

**Success gate:** 100 tasks submitted to Kafka → all 100 consumed and executed by 2 workers, no duplicates, no loss.

### 20.5 Milestone 9B-4: Capability Registry & Policy Service (4 weeks)

**Deliverables:**
- `aeos-capability-registry` service (`app/registry/registry.py`)
- Consistent hash ring for registry nodes
- gRPC API: `AdvertiseCapabilities`, `WithdrawCapabilities`, `LookupCapability`, `WatchCapabilities`
- `aeos-policy-service` service (`app/policy/service.py`)
- Policy storage in PostgreSQL
- Policy evaluation engine (condition matching, priority ordering)
- Governance token signing (JWT RS256)
- Policy hot-reload via Kafka `POLICY_UPDATED` events
- Human escalation workflow
- RBAC enforcement at API gateway (Kong plugin or FastAPI middleware)

**Dependencies:** 9B-1 (infrastructure), 9B-2 (cluster membership for capability withdrawal on node failure)

**Success gate:** Capability lookup p99 < 10 ms under 1000 req/s. Policy hot reload propagates within 5 seconds of update.

### 20.6 Milestone 9B-5: Distributed Memory (3 weeks)

**Deliverables:**
- `DistributedWorkingMemory` full implementation (Redis Cluster)
- `WeaviateMemoryStore` for Long-Term Memory
- `EpisodicMemoryService` gRPC service (reads from PostgreSQL replica)
- `episodic-writer` Kafka consumer service (writes episodes to PostgreSQL)
- Memory coherence: `WAIT` command for consistent reads
- Session migration on worker failure (working memory preserved in Redis)
- Memory quota enforcement per workflow (`max_memory_mb` from GovernanceGateResult)

**Dependencies:** 9B-1, 9B-3 (Kafka for episodic write events)

**Success gate:** Working memory persists across worker failure (kill one worker → sessions resume on another worker without data loss). Episodic write throughput: 1000 episodes/second.

### 20.7 Milestone 9B-6: Observability & Production Hardening (3 weeks)

**Deliverables:**
- OpenTelemetry SDK integration (trace IDs propagated through Kafka headers, gRPC metadata)
- Jaeger deployment (Kubernetes) and trace export
- Grafana dashboards (Cluster Overview, Workflow Detail, Agent Performance)
- Prometheus AlertManager rules
- Structured log pipeline: stdout → Fluent Bit → Kafka → Elasticsearch → Kibana
- Security: mTLS on all gRPC connections, cert-manager integration
- Vault integration: dynamic secrets for Redis, Postgres, Kafka
- Kubernetes resource manifests (all services)
- Performance tests (Locust scripts)
- Chaos test suite (all scenarios from §16.7)
- Migration tooling (Phase M1–M6 scripts)
- Runbook: incident response procedures for each failure class in §16.1

**Dependencies:** All previous milestones

**Success gate:** Full chaos test suite passes on staging. Locust performance tests meet all targets in §3.1.

### 20.8 Timeline Summary

```
Week:  1    2    3    4    5    6    7    8    9   10   11   12   13   14   15   16   17   18   19
       ├────────────────────┤
       9B-1: Infrastructure & Scaffolding
                            ├──────────────────────────────┤
                            9B-2: Cluster Manager
                                                 ├──────────────────────┤
                                                 9B-3: Distributed Scheduler
                            ├──────────────────────────────────────────────────┤
                            9B-4: Capability Registry & Policy Service
                                                                         ├────────────────────┤
                                                                         9B-5: Distributed Memory
                                                                                              ├──────────────────────┤
                                                                                              9B-6: Observability & Hardening
```

**Total duration:** 19 weeks (~5 months)  
**Team size assumption:** 4–6 engineers (2 senior, 2 mid, 1 DevOps/SRE, 1 QA/chaos)

### 20.9 Risk Register

| Risk | Probability | Impact | Mitigation |
|------|------------|--------|-----------|
| Kafka partition rebalance latency too high | Medium | High | Tune `session.timeout.ms`, use static group membership |
| Redis Sentinel failover exceeds 30s SLA | Low | High | Pre-test failover on ElastiCache before production cutover |
| Python asyncio event loop blocking on gRPC | Medium | Medium | Profile under load, offload blocking calls to thread pool |
| mTLS certificate rotation causes connection drops | Medium | Medium | Implement graceful cert rotation with pre-rotation warmup |
| LLM provider API changes break token counting | High | Low | Abstract provider clients, add integration tests for each |
| Weaviate scaling bottleneck at 10M embeddings | Low | Medium | Benchmark early (Month 1), evaluate Pinecone migration |
| Raft leader election storm during network instability | Low | High | Implement leader lease (prevents unnecessary elections) |
| Data migration causes LTM embedding drift | Low | Medium | Validate embeddings post-migration with similarity spot-check |

### 20.10 Definition of Done for Phase 9

Phase 9 is complete when:

1. All 6 implementation milestones are merged to `main`
2. All unit tests pass (> 90% branch coverage)
3. All integration tests pass in Docker Compose environment
4. All chaos test scenarios pass on staging EKS cluster
5. Locust performance targets met (§3.1) on staging under sustained load
6. mTLS verified for all inter-node communication
7. Governance tokens validated on 100% of task executions
8. Full migration from a Phase 8 instance to Phase 9 cluster completed on staging without data loss
9. Grafana dashboards showing all canonical metrics
10. Runbooks complete for all failure scenarios in §16.1
11. Security scan: 0 critical CVEs, 0 high CVEs in application code

---

## Appendix A: Protobuf Interface Definitions

### A.1 Cluster Manager gRPC

```protobuf
syntax = "proto3";
package aeos.cluster.v1;

service ClusterManager {
  rpc JoinCluster(JoinRequest) returns (JoinResponse);
  rpc LeaveCluster(LeaveRequest) returns (LeaveResponse);
  rpc DrainNode(DrainRequest) returns (DrainResponse);
  rpc Heartbeat(HeartbeatRequest) returns (HeartbeatResponse);
  rpc GetTopology(TopologyRequest) returns (TopologyResponse);
  rpc WatchTopology(WatchRequest) returns (stream TopologyEvent);
}

message JoinRequest {
  string node_id = 1;
  string hostname = 2;
  string grpc_address = 3;
  string http_address = 4;
  string role = 5;
  repeated string capabilities = 6;
  string version = 7;
  string zone = 8;
}

message JoinResponse {
  bool accepted = 1;
  string rejection_reason = 2;
  repeated int32 assigned_kafka_partitions = 3;
  TopologySnapshot topology = 4;
}

message HeartbeatRequest {
  string node_id = 1;
  NodeLoad load = 2;
}

message NodeLoad {
  int32 active_steps = 1;
  int32 queued_steps = 2;
  float cpu_utilization = 3;
  float memory_utilization = 4;
  int32 llm_calls_in_flight = 5;
}

message TopologySnapshot {
  repeated ClusterMember members = 1;
  string leader_node_id = 2;
  int64 cluster_version = 3;
}
```

### A.2 Capability Registry gRPC

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

message LookupRequest {
  string capability_id = 1;
  repeated string tags_required = 2;
  string strategy = 3;  // "LEAST_LOADED" | "ROUND_ROBIN" | "PINNED" | "ZONE_AFFINITY"
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
}
```

### A.3 Policy Service gRPC

```protobuf
syntax = "proto3";
package aeos.policy.v1;

service PolicyService {
  rpc EvaluateTask(TaskEvaluationRequest) returns (TaskEvaluationResponse);
  rpc AuditStep(StepAuditRequest) returns (AuditResponse);
  rpc GetPolicy(GetPolicyRequest) returns (Policy);
  rpc UpdatePolicy(UpdatePolicyRequest) returns (Policy);
  rpc ListPolicies(ListRequest) returns (stream Policy);
}

message TaskEvaluationRequest {
  string task_id = 1;
  string workflow_id = 2;
  string submitter = 3;
  string task_description = 4;
  string task_type = 5;
  map<string, string> metadata = 6;
}

message TaskEvaluationResponse {
  string decision = 1;          // "APPROVED" | "REJECTED" | "ESCALATED"
  string reason = 2;
  string governance_token = 3;  // Signed JWT (empty if rejected)
  string policy_version = 4;
  string risk_level = 5;        // "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
}
```

---

## Appendix B: Configuration Reference

All configuration is via environment variables. No YAML config files in the application (Kubernetes ConfigMaps provide env-var injection).

```bash
# Cluster identity
AEOS_NODE_ID=                          # Auto-generated UUID4 if not set
AEOS_NODE_ROLE=worker                  # worker | manager | policy | registry
AEOS_CLUSTER_MODE=false                # false=Phase 8 compat, true=Phase 9 distributed
AEOS_ZONE=us-east-1a                  # Availability zone label
AEOS_VERSION=9.0.0                     # Build version

# Cluster Manager
AEOS_CLUSTER_MANAGER_ADDR=aeos-cluster-manager.aeos.svc.cluster.local:50052
AEOS_HEARTBEAT_INTERVAL_S=5
AEOS_HEARTBEAT_TIMEOUT_S=30

# Capability Registry
AEOS_CAPABILITY_REGISTRY_ADDR=aeos-capability-registry.aeos.svc.cluster.local:50053

# Policy Service
AEOS_POLICY_SERVICE_ADDR=aeos-policy-service.aeos.svc.cluster.local:50054

# Redis
AEOS_REDIS_URL=redis://redis.aeos.svc.cluster.local:6379
AEOS_REDIS_PASSWORD_FILE=/vault/secrets/redis_password
AEOS_REDIS_MAX_CONNECTIONS=50
AEOS_REDIS_TLS=true

# Kafka
AEOS_KAFKA_BOOTSTRAP_SERVERS=kafka.aeos.svc.cluster.local:9092
AEOS_KAFKA_SASL_USERNAME_FILE=/vault/secrets/kafka_username
AEOS_KAFKA_SASL_PASSWORD_FILE=/vault/secrets/kafka_password
AEOS_KAFKA_TASK_TOPIC_PARTITIONS=20

# PostgreSQL
AEOS_PG_DSN_FILE=/vault/secrets/pg_dsn
AEOS_PG_MAX_CONNECTIONS=20

# Vector store
AEOS_LTM_PROVIDER=weaviate            # weaviate | pinecone
AEOS_WEAVIATE_URL=http://weaviate.aeos.svc.cluster.local:8080
AEOS_PINECONE_API_KEY_FILE=/vault/secrets/pinecone_key

# TLS / mTLS
AEOS_TLS_CERT_FILE=/etc/certs/tls.crt
AEOS_TLS_KEY_FILE=/etc/certs/tls.key
AEOS_TLS_CA_FILE=/etc/certs/ca.crt
AEOS_MTLS_ENABLED=true

# Worker pool
AEOS_MAX_CONCURRENT_STEPS=16
AEOS_STEPS_PER_CPU=4
AEOS_STEPS_PER_GB=2
AEOS_DRAIN_TIMEOUT_S=30

# LLM
AEOS_LLM_PROVIDER=openai              # openai | anthropic | local
AEOS_LLM_API_KEY_FILE=/vault/secrets/llm_api_key
AEOS_LLM_MAX_RPM=500
AEOS_LLM_MAX_TPM=150000
AEOS_LLM_CACHE_TTL_S=3600
AEOS_LLM_CACHE_ENABLED=true

# Observability
AEOS_JAEGER_AGENT_HOST=jaeger.monitoring.svc.cluster.local
AEOS_JAEGER_AGENT_PORT=6831
AEOS_PROMETHEUS_PUSHGATEWAY=http://pushgateway.monitoring.svc.cluster.local:9091
AEOS_LOG_LEVEL=INFO
AEOS_LOG_FORMAT=json
```

---

## Appendix C: Glossary

| Term | Definition |
|------|-----------|
| AEOSKernel | The 6-phase boot kernel from Phase 8, extended to 7 phases in Phase 9 (adds JOINING) |
| Capability | A dot-separated string identifying a service an agent can provide (e.g., `agent.research`) |
| Capability Federation | The cluster-wide capability discovery and routing system |
| Circuit Breaker | A CLOSED/OPEN/HALF_OPEN state machine that isolates failing dependencies |
| Cluster Manager | The Raft-based leader election and membership management service |
| CognitiveAgent | The 11-step abstract agent base class from Phase 8A |
| DEE | Distributed Execution Engine (Phase 8.3) |
| DLQ | Dead Letter Queue — a Kafka topic for tasks that failed after all retries |
| DRP | Distributed Runtime Platform (this document's subject) |
| EDF | Earliest Deadline First — a scheduling algorithm |
| ExecutionGraph | A directed acyclic graph of typed nodes representing a workflow |
| GovernanceGate | The pre-execution policy check that every task must pass |
| GovernanceToken | A signed JWT proving a task passed the policy gate |
| HyperKernel | The Phase 8 kernel (see AEOSKernel) |
| ISR | In-Sync Replicas — Kafka's replication health metric |
| mTLS | Mutual TLS — both client and server present certificates |
| Partition Key | The Kafka message key used to determine which partition a message goes to |
| Raft | A distributed consensus algorithm for leader election and log replication |
| RFC | Request for Comments — this architecture specification document type |
| RTO | Recovery Time Objective — maximum acceptable time to recover from a failure |
| RPO | Recovery Point Objective — maximum acceptable data loss in a failure |
| SLA | Service Level Agreement — the performance/availability commitment |
| Worker Node | An `aeos-worker` process: runs the full kernel + execution engine + agents |
| Working Memory | The session-scoped, fast-access memory tier (Redis in Phase 9) |

---

*End of RFC-009 — AEOS Phase 9 Distributed Runtime Platform Specification*  
*Document version: 1.0.0 — 2026-07-06*  
*Next review: Before Phase 9B implementation milestone 9B-1 kickoff*
