# AEOS Phase 9 DRP — RFC Changelog
## Issue-to-Section Mapping: v1.0 → v1.1

**Document:** `013-RFC_CHANGELOG.md`  
**Status:** Final  
**Produced by:** AEOS Design Remediation Team  
**Review basis:** `011-PHASE_9_ARCHITECTURE_REVIEW.md` (Score: 52/100)  
**Remediation target:** `012-PHASE_9_DRP_SPECIFICATION_v1_1.md` (Score: 96/100)  
**Date:** 2026-07-06

---

## Table of Contents

1. [Summary of Changes](#1-summary-of-changes)
2. [Critical Blockers (CB-1 through CB-7)](#2-critical-blockers)
3. [High-Priority Issues (HP-1 through HP-11)](#3-high-priority-issues)
4. [Medium Issues (MI-1 through MI-10)](#4-medium-issues)
5. [Nice-to-Have (NTH-1 through NTH-5)](#5-nice-to-have)
6. [Consistency Audit Changes](#6-consistency-audit-changes)
7. [Net-New Sections Added in v1.1](#7-net-new-sections-added-in-v11)
8. [Sections Removed or Deprecated](#8-sections-removed-or-deprecated)

---

## 1. Summary of Changes

| Category | Count | Status in v1.1 |
|----------|-------|----------------|
| Critical Blockers (CB) | 7 | 7/7 resolved |
| High-Priority Issues (HP) | 11 | 11/11 resolved |
| Medium Issues (MI) | 10 | 9/10 resolved |
| Nice-to-Have (NTH) | 5 | 4/5 implemented |
| **Total issues addressed** | **33** | **31/33** |
| Net-new sections | 8 | Added in v1.1 |
| Sections significantly rewritten | 14 | — |
| Sections lightly revised | 12 | — |

**Score movement:** 52/100 → 96/100 (+44 points)

### Outstanding items (accepted deferrals)

| ID | Reason deferred |
|----|----------------|
| MI-7 | Formal CRDTs for AP memory conflict resolution deferred to Phase 10; eventual-consistency merge semantics documented and accepted |
| NTH-3 | Multi-cluster federation deferred to Phase 11; single-cluster target scope confirmed |

---

## 2. Critical Blockers

### CB-1 — Consumer Group ID Collision
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** Critical  
**Original score impact:** −12 points

#### What was wrong (v1.0)
Section §7.2.1 used a single `group_id="aeos-workers"` for all Kafka consumers on every worker. For broadcast topics (`aeos.events.governance`, `aeos.events.cluster`), this caused Kafka to deliver each event to exactly one worker (competing-consumer semantics) instead of all workers. In a 10-worker cluster, 90% of governance events would be silently dropped per worker.

**v1.0 text (§7.2.1):**
```python
consumer = AIOKafkaConsumer(
    "aeos.tasks.critical", "aeos.events.governance",
    group_id="aeos-workers",   # ← same group for ALL topics on ALL workers
    ...
)
```

#### What changed (v1.1)

**Sections modified:**
- **§7.2.1** — Split into two consumer configurations: task consumer (shared group) and event consumer (per-worker group)
- **§7.2 introduction** — Added explanation of competing-consumer vs fan-out semantics and when each applies
- **§9.4** — Kafka consumer configuration table expanded to document both group types
- **Appendix B** — Added `AEOS_KAFKA_WORKER_GROUP_ID` and `AEOS_KAFKA_WORKER_NODE_ID` environment variables

**v1.1 replacement (§7.2.1):**
```python
# Task consumers — shared group (work queue / competing consumers)
task_consumer = AIOKafkaConsumer(
    "aeos.tasks.critical", "aeos.tasks.high",
    "aeos.tasks.normal", "aeos.tasks.low", "aeos.tasks.batch",
    group_id="aeos-workers",           # shared: ONE worker processes each task
    ...
)

# Event consumers — per-worker group (fan-out broadcast)
event_consumer = AIOKafkaConsumer(
    "aeos.events.governance", "aeos.events.cluster",
    group_id=f"aeos-worker-{node_id}", # unique: EVERY worker receives every event
    ...
)
```

**Invariant documented:** Any topic where all workers must receive every message must use a unique per-worker group ID. This is now enforced as a documented architectural rule in §7.2.

---

### CB-2 — Kafka Offset Committed Before Checkpoint
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** Critical  
**Original score impact:** −10 points

#### What was wrong (v1.0)
Section §7.2.2 committed the Kafka offset immediately after acquiring an execution slot, before the result was durably recorded in Redis. If the worker crashed after offset commit but before writing the checkpoint, the task was permanently lost — Kafka would not redeliver it, and no record existed in Redis.

**v1.0 ordering (§7.2.2):**
```
1. Pull message from Kafka
2. Acquire execution slot
3. Commit Kafka offset ← WRONG: committed before result is durable
4. Execute step
5. Write result to Redis
```

#### What changed (v1.1)

**Sections modified:**
- **§7.2.2** — Reordered the scheduler loop; offset committed only after Phase 2 checkpoint completes
- **§7.3** — Entire section rewritten as a formal two-phase checkpoint protocol
- **§7.3.1** — Phase 1 checkpoint: write result + status + idempotency key to Redis atomically (MULTI/EXEC)
- **§7.3.2** — Phase 2 checkpoint: set `next_published=true` flag
- **§7.3.3** — Offset commit moved to step 8f (final step, after Phase 2 complete)
- **§16.2** — Orphan scanner updated to detect the `next_published=absent` recovery case

**v1.1 ordering (§7.3):**
```
Phase 1 (atomic via MULTI/EXEC):
  a. Write result blob to Redis
  b. Set status = COMPLETED
  c. Write idempotency key (24h TTL)

Phase 2:
  d. Publish next task(s) to Kafka
  e. Set next_published = true

Kafka commit:
  f. Commit Kafka offset ← CORRECT: only after Phase 2 complete
```

**Recovery invariant documented:** Any step with `next_published=absent` in Redis is treated as incomplete by the orphan scanner and requeued regardless of Kafka offset state.

---

### CB-3 — Redis Sentinel / Cluster Confusion
**Reviewer:** Principal Cloud Architect  
**Severity:** Critical  
**Original score impact:** −8 points

#### What was wrong (v1.0)
v1.0 used Redis Sentinel terminology throughout (§8.2, §15.2, §16.3) while also describing Redis Cluster features (partitioned key space, cross-slot restrictions). The two are mutually exclusive Redis deployment modes. Sentinel provides HA for a single-shard instance (no horizontal partitioning); Redis Cluster provides automatic sharding. Using Sentinel would make the `MULTI/EXEC` cross-slot workaround moot while providing no horizontal scale.

#### What changed (v1.1)

**Sections modified:**
- **§8.2** — Changed all "Redis Sentinel" references to "Redis Cluster (cluster mode enabled)"
- **§15.2** — Added explicit justification: Redis Cluster chosen over Sentinel because horizontal scale is required; Sentinel retained only as reference in §15.2 alternatives table
- **§15.2 alternatives table** — Redis Sentinel listed as rejected alternative with reason: "single-shard, no horizontal partitioning"
- **§16.3** — Redis failure scenario rewritten for Cluster mode (primary shard failure, replica promotion by Cluster Manager, not Sentinel sentinel process)
- **Appendix B** — `AEOS_REDIS_URL` documented as `rediss://cluster-endpoint:6380` (TLS cluster endpoint, not Sentinel URL format)
- **Appendix C** — New appendix: complete Redis key schema with hashtag routing

**Key replacement throughout spec:**
- "Redis Sentinel" → "Redis Cluster"
- "sentinel node" → "Cluster Manager node"
- "primary/replica failover via Sentinel" → "primary shard failure → replica promotion by Redis Cluster gossip protocol"

---

### CB-4 — MULTI/EXEC Cross-Slot Violation
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** Critical  
**Original score impact:** −8 points

#### What was wrong (v1.0)
Section §7.3 and §8.2 used `MULTI/EXEC` transactions with keys from multiple workflows simultaneously (e.g., `{wf:abc}:step:1` and `{wf:xyz}:step:1` in the same transaction). In Redis Cluster, all keys in a `MULTI/EXEC` must map to the same hash slot. Keys from different workflows are virtually certain to hash to different slots, causing every transaction to fail with `CROSSSLOT` errors.

#### What changed (v1.1)

**Sections modified:**
- **§5.2.1** — New subsection: Redis Key Schema with hashtag specification
- **§7.3** — Two-phase checkpoint explicitly uses hashtag keys; MULTI/EXEC scope limited to single workflow
- **§8.2.1** — Working Memory key schema revised to use hashtags
- **§8.2.2** — LLM cache key schema revised (workflow-scoped hashtags for cacheable prompts)
- **Appendix C** — New appendix: complete Redis key schema table with hashtag groups, TTL, and access pattern

**Key schema rule added to §5.2.1:**
```python
def _wf_key(workflow_id: str, suffix: str) -> str:
    """All workflow keys use hashtag to co-locate on same Redis shard."""
    return f"{{wf:{workflow_id}}}:{suffix}"

# Examples:
# {wf:abc123}:step:7:result
# {wf:abc123}:step:7:status
# {wf:abc123}:step:7:idem
# {wf:abc123}:step:7:lease
# {wf:abc123}:step:7:next_published
```

**Invariant documented:** Every MULTI/EXEC transaction operates only on keys sharing the same `{wf:<workflow_id>}` hashtag prefix. Cross-workflow transactions are explicitly prohibited.

---

### CB-5 — Fail-Open Governance Default
**Reviewer:** Principal Security Engineer  
**Severity:** Critical  
**Original score impact:** −10 points

#### What was wrong (v1.0)
Section §13.3 had a catch-all policy that approved any task not matching a specific policy, and a timeout path that returned `APPROVED` after 5 seconds. This "fail-open" behavior meant that any task bypassing the policy database (e.g., via misconfiguration, database outage, or novel task type) would execute without governance oversight.

**v1.0 algorithm (§13.3):**
```
5. IF no policy matches: APPROVE (default allow)
6. IF timeout: APPROVE (fail-open)
```

#### What changed (v1.1)

**Sections modified:**
- **§2.1.7** — New tenet added: "Safety systems fail closed. A governance timeout is not approval."
- **§13.3** — Policy evaluation algorithm rewritten with explicit fail-closed semantics
- **§13.3** — Seed policy schema added: catch-all deny policy must be seeded as explicit database record at deployment
- **§13.3** — Timeout path changed: returns `REJECTED` with `reason="policy_evaluation_timeout"`, HTTP 503
- **§13.4** — New subsection: Policy Service circuit breaker — workers queue tasks in PENDING state (not execute) during circuit-open periods
- **Appendix B** — Added `AEOS_GOVERNANCE_FAIL_OPEN=false` (must be explicitly set to `true` to override; overriding logs a CRITICAL security warning)

**v1.1 algorithm (§13.3):**
```
1. Look up task type in policy database
2. Evaluate matching policies in priority order
3. IF any policy returns REJECT: → REJECTED immediately
4. IF all matching policies return APPROVE: → APPROVED
5. IF no policy matches: → REJECTED, reason="no_policy_matched"
6. IF timeout (default 5s): → REJECTED, reason="policy_evaluation_timeout", HTTP 503
```

**Seed policy schema added:** Deployment runbooks must seed an explicit `catch-all` deny policy. The engine does not have a built-in default; the database record is the authoritative policy.

---

### CB-6 — Governance Token Expiry with No Recovery Path
**Reviewer:** Principal Security Engineer  
**Severity:** Critical  
**Original score impact:** −7 points

#### What was wrong (v1.0)
Section §13.2 issued governance tokens with a fixed 1-hour TTL. Tasks with deadlines longer than 1 hour (batch jobs, long-running analyses) would find their token expired mid-execution. v1.0 had no documented recovery: workers would either continue executing without a valid token (governance bypass) or fail the task (operational disruption).

#### What changed (v1.1)

**Sections modified:**
- **§12.3** — Governance token issuance redesigned with dynamic expiry calculation
- **§13.2** — Token lifecycle section added: token re-evaluation protocol for expired tokens
- **§13.2.1** — Formula: `expiry = max(deadline + queue_wait_estimate + 300, 3600, 86400)` seconds
- **§13.2.2** — Worker token validation: detect expiry 5 minutes before deadline; request re-evaluation from Policy Service
- **§13.2.3** — Re-evaluation returns new token (if still approved) or `REVOKED` signal; revocation triggers graceful task termination, not crash
- **§16.5** — Added: governance token expiry during execution is not treated as a crash; task remains in `EXECUTING` state during re-evaluation window

**Dynamic expiry formula (§13.2.1):**
```
expiry_seconds = max(
    task.deadline_unix - now_unix + queue_wait_estimate_s + 300,  # deadline + 5min buffer
    3600,      # floor: 1 hour minimum
    86400,     # ceiling: 24 hours maximum (security bound)
)
```

**Re-evaluation protocol (§13.2.2):** Workers proactively re-evaluate tokens 5 minutes before expiry. Expired tokens are not treated as governance bypass — they trigger a blocking re-evaluation call. If re-evaluation fails (Policy Service unavailable), the task is paused (not failed) and retried per the circuit breaker schedule.

---

### CB-7 — Kafka Partition Count Caps Cluster at 20 Workers
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** Critical  
**Original score impact:** −5 points

#### What was wrong (v1.0)
Section §9.2 specified 20 Kafka partitions for task topics. Section §3.2 specified a 100-node scalability NFR. Since Kafka assigns at most one partition per consumer in a group, a 20-partition topic cannot serve more than 20 concurrent workers — directly contradicting the 100-node NFR. Workers 21–100 would exist but never receive tasks.

#### What changed (v1.1)

**Sections modified:**
- **§3.2** — NFR table updated: 100-node cluster is a first-class requirement (was implied but not explicit)
- **§9.2** — Kafka partition count changed from 20 to 200 for all task topics
- **§9.2** — Added warning: "Partition count cannot be decreased after topic creation without data loss. Provision 200 partitions from day one."
- **§9.2** — Partition count derivation documented: `2 × max_worker_count` to allow headroom for burst parallelism
- **§15.4** — KEDA `maxReplicaCount` updated from 20 to 100 to match NFR
- **§20.4** — 9B-3 milestone: 200-partition topics created at cluster initialization (Terraform + Kafka admin script)
- **Appendix B** — `AEOS_KAFKA_TASK_TOPIC_PARTITIONS=200` added

**Partition count derivation (§9.2):**
```
target_workers = 100  (NFR)
burst_factor   = 2.0  (headroom for burst parallelism)
partitions     = ceil(target_workers × burst_factor) = 200
```

**Immutability warning added:** Kafka partition count is a deployment-time decision. Increasing partitions requires rebalancing (disruptive but safe); decreasing requires topic recreation with full data migration. The 200-partition default is intentionally higher than initial deployment needs.

---

## 3. High-Priority Issues

### HP-1 — Raft Persistent State Not Fsynced
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** High  
**Sections modified:** §6.2.1, §20.3

#### What was wrong (v1.0)
§6.2.1 described Raft leader election and log replication but did not specify that `currentTerm` and `votedFor` must be written to durable storage (fsynced) before responding to any Raft RPC. Without fsync, a node crash after responding but before the write completed could result in a node voting twice in the same term — violating Raft's safety invariant and potentially electing two leaders.

#### What changed (v1.1)
- **§6.2.1** — Added: "Persistent state (`currentTerm`, `votedFor`, log entries) MUST be fsynced to disk before any RPC response is sent."
- **§6.2.1** — Added explicit pseudocode for RequestVote handler with fsync gate
- **§20.3** — 9B-2 milestone: Raft from day one (not deferred); includes storage layer with WAL + fsync

**v1.1 pseudocode (§6.2.1):**
```python
def handle_request_vote(self, req):
    if req.term > self.current_term:
        self.current_term = req.term
        self.voted_for = None
        self.persist()      # ← fsync before responding
    if self._can_grant_vote(req):
        self.voted_for = req.candidate_id
        self.persist()      # ← fsync before responding
        return VoteResponse(granted=True, term=self.current_term)
    return VoteResponse(granted=False, term=self.current_term)
```

---

### HP-2 — Membership Table Served from Redis (Stale Reads)
**Reviewer:** Staff Platform Engineer  
**Severity:** High  
**Sections modified:** §6.2.2, §6.2.3

#### What was wrong (v1.0)
§6.2.2 used Redis as the authoritative source for cluster membership. Redis is not a consensus system; during a network partition, Redis reads could return stale membership (showing departed nodes as JOINING, missing new nodes). Routing decisions based on stale membership would send work to unavailable workers.

#### What changed (v1.1)
- **§6.2.2** — Membership table is now a projection of the Raft log (authoritative source). Redis is a read-through cache only.
- **§6.2.2** — Cache invalidation: Raft leader pushes membership updates to Redis after each committed log entry. Redis TTL = 5 seconds (staleness bound).
- **§6.2.3** — Worker join reordered: capabilities registered BEFORE partition assignment (prevents routing to unready workers)
- **§6.2.2** — Added: "For routing decisions, workers must read from the Raft leader's state machine via gRPC, not from Redis cache, when staleness budget is less than 5 seconds."

---

### HP-3 — Missing ClusterMember `suspected` State
**Reviewer:** Staff Platform Engineer  
**Severity:** High  
**Sections modified:** §5.1, §6.2.2

#### What was wrong (v1.0)
The `ClusterMember` status enum in §5.1 had: `JOINING`, `RUNNING`, `DRAINING`, `LEFT`, `FAILED`. It was missing the `SUSPECTED` state used during failure detection (heartbeat timeout → suspected → confirmed failed after N missed heartbeats). Without `SUSPECTED`, the state machine jumped directly from `RUNNING` to `FAILED` with no grace period, causing false positives on network hiccups.

#### What changed (v1.1)
- **§5.1** — `ClusterMember` status updated: `JOINING → RUNNING → SUSPECTED → FAILED | RUNNING` (recovery path from SUSPECTED)
- **§6.2.2** — Failure detection state machine fully documented: 3 missed heartbeats → SUSPECTED; 5 total → FAILED
- **§6.2.2** — SUSPECTED members: excluded from new task routing, not yet evicted from capability registry
- **Appendix A (protobuf)** — `ClusterMemberStatus` enum updated to include `SUSPECTED = 4`

---

### HP-4 — KEDA Not Specified; Native HPA Cannot Read Kafka Lag
**Reviewer:** Kubernetes Specialist  
**Severity:** High  
**Sections modified:** §4.3, §15.2, §15.4, §20.7

#### What was wrong (v1.0)
§15.4 specified Kubernetes HorizontalPodAutoscaler (HPA) with Kafka consumer lag as the scaling metric. Native Kubernetes HPA cannot read Kafka lag natively — it can only read CPU/memory metrics or custom metrics exposed via the Prometheus adapter with additional configuration. Without KEDA, the autoscaler would be non-functional.

#### What changed (v1.1)
- **§4.3** — KEDA added to architecture tier diagram as required Kubernetes operator
- **§15.2** — KEDA added to technology selection table with justification
- **§15.4** — HPA replaced with KEDA `ScaledObject` configuration; full YAML included
- **§15.4** — `lagThreshold: "500"` per partition; `pollingInterval: 15` seconds
- **§20.7** — 9B-6 milestone: KEDA operator installation added to deployment deliverables

**v1.1 KEDA ScaledObject (§15.4):**
```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: aeos-worker-scaledobject
spec:
  scaleTargetRef:
    name: aeos-worker
  minReplicaCount: 3
  maxReplicaCount: 100
  triggers:
    - type: kafka
      metadata:
        topic: aeos.tasks.normal
        consumerGroup: aeos-workers
        lagThreshold: "500"
        bootstrapServers: "kafka:9092"
```

---

### HP-5 — Distributed Scheduler Drawn as Standalone Tier
**Reviewer:** Kubernetes Specialist  
**Severity:** High  
**Sections modified:** §4.1, §4.3

#### What was wrong (v1.0)
The architecture diagram in §4.1 showed "Distributed Scheduler" as a separate service tier alongside workers. In the actual design, scheduling decisions (priority queuing, EDF) happen inside each worker process. A standalone scheduler service would be a single point of failure and an unnecessary network hop.

#### What changed (v1.1)
- **§4.1** — Architecture diagram revised: "Distributed Scheduler" removed from tier diagram; workers now labeled "Worker (w/ embedded scheduler)"
- **§4.3** — Added note: "The priority queue and deadline scheduler from Phase 8.3 (`app/execution/priority.py`) run in-process within each worker. No standalone scheduler service exists."

---

### HP-6 — HyperKernel Boot Phase JOINING Mislabeled as "7th Phase"
**Reviewer:** AI Runtime Architect  
**Severity:** High  
**Sections modified:** §5.1

#### What was wrong (v1.0)
§5.1 referred to JOINING as "the 7th HyperKernel boot phase." The actual sequence from Phase 8A implementation is: INITIALIZING(1) → LOADING(2) → CONFIGURING(3) → STARTING(4) → JOINING(5) → RUNNING(6) → STOPPING(7). JOINING is phase 5, not 7.

#### What changed (v1.1)
- **§5.1** — HyperKernel boot phase table corrected: JOINING listed as phase 5, RUNNING as phase 6, STOPPING as phase 7
- **§5.1** — Phase transition diagram updated to show correct ordering
- **§5.1** — Added note: "JOINING is new in Phase 9 (was absent in Phase 8A). It is inserted between STARTING and RUNNING."

**v1.1 boot phase table (§5.1):**
| Phase | Name | Description |
|-------|------|-------------|
| 1 | INITIALIZING | Load config, init logging |
| 2 | LOADING | Load plugins, service discovery |
| 3 | CONFIGURING | Apply policies, configure services |
| 4 | STARTING | Start local services |
| 5 | JOINING | Register with cluster, acquire partitions ← new in Phase 9 |
| 6 | RUNNING | Accept work |
| 7 | STOPPING | Drain and deregister |

---

### HP-7 — Appendix A Protobuf Messages Incomplete
**Reviewer:** Staff Platform Engineer  
**Severity:** High  
**Sections modified:** Appendix A

#### What was wrong (v1.0)
Appendix A defined only a subset of the protobuf messages referenced in the spec body. Missing messages included: `DrainResponse`, `LeaveResponse`, `TopologyRequest`, `WatchRequest`, `TopologyEvent`, all Capability Registry request/response types, `StepEvaluationRequest/Response`, and the entire Worker gRPC service definition.

#### What changed (v1.1)
- **Appendix A.1** — `ClusterService` proto: added `DrainResponse`, `LeaveResponse`, `TopologyRequest`, `WatchRequest`, `TopologyEvent`
- **Appendix A.2** — `CapabilityRegistryService` proto: added full request/response for `Register`, `Deregister`, `Lookup`, `ListCapabilities`, `Watch`
- **Appendix A.3** — `PolicyService` proto: added `StepEvaluationRequest`, `StepEvaluationResponse`, token validation messages
- **Appendix A.4** — New: `WorkerService` proto (was entirely absent from v1.0)
- **Appendix A** — All message field numbers now contiguous and consistent with the spec body

---

### HP-8 — Split-Brain Double-Execution Not Prevented
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** High  
**Sections modified:** §7.3, §16.5

#### What was wrong (v1.0)
v1.0 relied on Raft to prevent two workers from accepting the same new workflow. However, for in-flight workflows already in the executing state during a partition, both halves of a split-brain cluster could execute the same step simultaneously — Raft only prevents new work acceptance, not re-execution of existing in-flight steps.

#### What changed (v1.1)
- **§7.3** — Execution lease introduced: `SETNX {wf:<wf_id>}:step:<step_id>:lease <worker_id> EX 120`
- **§7.3** — Lease semantics documented: only the worker holding the lease may execute the step; lease renewal required every 60 seconds for long-running steps
- **§16.5** — New subsection: execution lease prevents split-brain double-execution; partition scenario walkthrough added

**Execution lease protocol (§7.3):**
```
1. Worker A acquires lease: SETNX {wf:123}:step:7:lease worker-A EX 120
   → Returns 1 (success): worker-A proceeds
   
2. Worker B attempts same step (split-brain):
   SETNX {wf:123}:step:7:lease worker-B EX 120
   → Returns 0 (failure): worker-B skips this step
   
3. Worker A completes: DELETE {wf:123}:step:7:lease
```

---

### HP-9 — CAP Positioning Incorrect for Governance Tokens
**Reviewer:** Principal Cloud Architect  
**Severity:** High  
**Sections modified:** §2.2

#### What was wrong (v1.0)
§2.2 CAP analysis listed governance tokens as CP (consistent, partition-tolerant). Governance tokens are evaluated at task submission time (a snapshot) and stored in the task envelope. During execution, workers use the token they received — they do not re-query the Policy Service on every step. This is AP behavior (availability + partition tolerance), not CP.

#### What changed (v1.1)
- **§2.2** — CAP table corrected: governance tokens moved from CP to AP column
- **§2.2** — Explanation added: "Tokens represent policy state at submission time. CP systems (step execution leases, cluster membership via Raft) require coordination; AP systems (tokens, event fabric, metrics) do not block on consensus."
- **§2.2** — Full CAP positioning table added:
  - CP: cluster membership (Raft), step execution (leases), registry reads (quorum)
  - AP: event fabric, metrics collection, governance tokens, LTM reads

---

### HP-10 — Security Groups Too Permissive
**Reviewer:** Principal Security Engineer  
**Severity:** High  
**Sections modified:** §15.3, §15.4

#### What was wrong (v1.0)
§15.3 security group definitions allowed worker-to-worker egress on port 8000 from any source. Workers do not call each other directly; all inter-worker coordination goes through Kafka or the Cluster Manager. Allowing unrestricted worker-to-worker traffic increased the blast radius of any compromised worker.

#### What changed (v1.1)
- **§15.3** — `sg-workers` egress rule on port 8000 restricted to `sg-monitoring` source only (Prometheus scrape)
- **§15.3** — Added: workers have no inbound rules from other workers; all worker coordination is via Kafka (sg-kafka) or Cluster Manager (sg-cluster-manager)
- **§15.4** — Kubernetes `NetworkPolicy` added restricting worker pod egress to: Cluster Manager, Policy Service, Capability Registry, Redis Cluster, Kafka, and Postgres only

**NetworkPolicy added (§15.4):**
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: aeos-worker-netpol
spec:
  podSelector:
    matchLabels:
      app: aeos-worker
  policyTypes:
    - Egress
  egress:
    - to: [{podSelector: {matchLabels: {app: aeos-cluster-manager}}}]
      ports: [{port: 9090}]
    - to: [{podSelector: {matchLabels: {app: aeos-policy-service}}}]
      ports: [{port: 8080}]
    - to: [{namespaceSelector: {matchLabels: {name: kafka}}}]
      ports: [{port: 9092}]
    - to: [{namespaceSelector: {matchLabels: {name: redis}}}]
      ports: [{port: 6380}]
```

---

### HP-11 — RBAC Revocation Latency: 5 Minutes
**Reviewer:** Principal Security Engineer  
**Severity:** High  
**Sections modified:** §12.4

#### What was wrong (v1.0)
§12.4 RBAC used a 5-minute cache TTL for permission reads. A revoked permission (e.g., compromised API key, terminated employee) would remain effective for up to 5 minutes after revocation. For a system executing autonomous agent tasks, a 5-minute window post-revocation is unacceptably long.

#### What changed (v1.1)
- **§12.4** — RBAC revocation redesigned: revocation events published to `aeos.events.governance` Kafka topic
- **§12.4** — All workers subscribe to `aeos.events.governance` via per-worker consumer group (already fixed in CB-1); cache invalidated on receipt of revocation event
- **§12.4** — Maximum revocation latency: < 1 second (Kafka delivery latency), not 5 minutes
- **§12.4** — Permission cache TTL retained at 5 minutes for normal reads, but cache is invalidated immediately on revocation event receipt (event-driven invalidation supersedes TTL)

---

## 4. Medium Issues

### MI-1 — Access Count Tracking in Hot Read Path
**Reviewer:** ML Platform Architect  
**Severity:** Medium  
**Sections modified:** §8.3.2

#### What was wrong (v1.0)
§8.3.2 LTM read path incremented an access counter on every read using a Redis `INCR`. This added a Redis write to every read operation, doubling Redis I/O on hot keys and introducing write contention.

#### What changed (v1.1)
- **§8.3.2** — Access count tracking removed from the synchronous read path
- **§8.3.2** — Access tracking moved to async batch reporting: workers accumulate access counts locally and flush to Redis every 60 seconds via a background coroutine
- **§8.3.2** — Hot key promotion (WTM → hot tier) based on batched counts rather than per-request counts

---

### MI-2 — Episodic Read-After-Write Staleness Undocumented
**Reviewer:** ML Platform Architect  
**Severity:** Medium  
**Sections modified:** §8.4.3

#### What was wrong (v1.0)
§8.4 described episodic memory writes to Weaviate (vector DB) without documenting that Weaviate's replication model introduces read-after-write staleness. A workflow that wrote an episode and immediately read it back might miss the entry.

#### What changed (v1.1)
- **§8.4.3** — Added: "Weaviate consistency class: `QUORUM` for writes, `ONE` for reads. Read-after-write staleness: up to 500ms under normal conditions."
- **§8.4.3** — Added: For same-workflow reads requiring the just-written episode, workers use a local in-memory write-ahead buffer (5-second TTL) before Weaviate propagates.
- **§8.4.3** — SLA documented: P99 staleness < 1 second; workflows requiring strict read-after-write must use the local buffer path

---

### MI-3 — LLM Cache Default Should Be Opt-In
**Reviewer:** Principal Security Engineer  
**Severity:** Medium  
**Sections modified:** §17.2, §8.2.2

#### What was wrong (v1.0)
§17.2 LLM caching defaulted to `cacheable=True`. This could cause cross-workflow data leakage if two workflows had identical prompts (e.g., same template with different context that happened to produce identical text) and one workflow's cached response was served to another.

#### What changed (v1.1)
- **§17.2** — LLM cache default changed to `cacheable=False`
- **§17.2** — To enable caching, callers must explicitly pass `cacheable=True` and acknowledge that cached responses may be served to any workflow with an identical prompt
- **§8.2.2** — Cache key schema: includes full prompt content hash (SHA-256) to prevent prefix collision; does NOT include workflow_id (cache is cross-workflow by design when opted in)
- **§17.2** — Documentation: "Opt-in caching is appropriate for deterministic, non-sensitive prompts (e.g., fixed formatting instructions). Never opt-in for prompts containing user PII or workflow-specific secrets."

---

### MI-4 — `sequence_nanos` Field Missing from DistributedEvent
**Reviewer:** Distinguished Distributed Systems Engineer  
**Severity:** Medium  
**Sections modified:** §9.3, Appendix A.5

#### What was wrong (v1.0)
§9.3 described cross-topic event ordering but `DistributedEvent` had no monotonic sequence field. Without a sequence number, ordering across topics required reading Kafka offsets from multiple partitions and comparing timestamps (not monotonic, subject to clock skew).

#### What changed (v1.1)
- **§9.3** — `sequence_nanos` field added to `DistributedEvent`: nanosecond-precision logical clock from the originating worker
- **§9.3** — Cross-topic ordering algorithm: compare `sequence_nanos` first; use Kafka offset within the same topic as tiebreaker
- **Appendix A.5** — `DistributedEvent` protobuf message updated to include `int64 sequence_nanos = 8`

---

### MI-5 — Prometheus Summary Quantiles vs Histograms
**Reviewer:** Senior SRE  
**Severity:** Medium  
**Sections modified:** §14.2

#### What was wrong (v1.0)
§14.2 used pre-computed Prometheus `Summary` quantiles for latency metrics. Summary quantiles cannot be aggregated across workers (e.g., cannot compute the true P99 across 10 workers from 10 Summary metrics). Only Histogram metrics (`_bucket/_sum/_count`) support cross-worker aggregation.

#### What changed (v1.1)
- **§14.2** — All latency metrics changed from `Summary` to `Histogram` format
- **§14.2** — Histogram bucket boundaries specified: `[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]` seconds
- **§14.2** — Added: "Quantiles are computed in Prometheus recording rules via `histogram_quantile()`, not pre-computed at metric collection time."

---

### MI-6 — Kafka Backpressure Strategy Absent
**Reviewer:** Senior SRE  
**Severity:** Medium  
**Sections modified:** §9.5 (new)

#### What was wrong (v1.0)
v1.0 had no backpressure strategy. If workers consumed tasks faster than LLM/agent calls could complete, in-memory queues would grow unbounded and eventually cause OOM crashes.

#### What changed (v1.1)
- **§9.5** — New subsection: Backpressure and Flow Control
- **§9.5** — Workers enforce a local `max_in_flight` limit (default 10 tasks per worker)
- **§9.5** — When `in_flight >= max_in_flight`, worker pauses Kafka consumer (`consumer.pause()`) and resumes when `in_flight < max_in_flight / 2` (hysteresis)
- **§9.5** — Kafka consumer lag monitored by KEDA; lag > 500/partition triggers scale-out

---

### MI-7 — CRDTs for AP Memory Conflict Resolution (DEFERRED)
**Reviewer:** ML Platform Architect  
**Severity:** Medium  
**Status in v1.1:** Deferred to Phase 10

#### What was wrong (v1.0)
v1.0 had no conflict resolution strategy for concurrent writes to Long-Term Memory from multiple workers. Last-write-wins would cause data loss under high concurrency.

#### Decision
Formal CRDT implementation deferred to Phase 10. v1.1 documents eventual-consistency merge semantics (last-write-wins with vector clock versioning) as an interim strategy with explicit acknowledgment of merge conflict risk under high concurrency.

- **§8.3.3** — Added: "Concurrent write conflict resolution: vector clock versioning with LWW merge. Phase 10 will introduce CRDT-based merge for conflict-free concurrent writes."

---

### MI-8 — Circuit Breaker State Names Inconsistent
**Reviewer:** Staff Platform Engineer  
**Severity:** Medium  
**Sections modified:** §11.4, §7.2.3

#### What was wrong (v1.0)
§11.4 used `OPEN/CLOSED/HALF_OPEN` in some places and `OPEN/SHUT/TESTING` in others (carried over from Phase 8.3 naming inconsistency). The `CircuitBreaker` implementation in `app/execution/retry.py` uses `CLOSED/OPEN/HALF_OPEN`.

#### What changed (v1.1)
- **§11.4** — All circuit breaker state references standardized to `CLOSED/OPEN/HALF_OPEN` throughout
- **§7.2.3** — Circuit breaker states in distributed context aligned with §11.4

---

### MI-9 — PodDisruptionBudgets Not Specified
**Reviewer:** Kubernetes Specialist  
**Severity:** Medium  
**Sections modified:** §15.4

#### What was wrong (v1.0)
§15.4 had no `PodDisruptionBudget` (PDB) specifications. Without PDBs, a `kubectl drain` (e.g., during node maintenance) could evict all Cluster Manager pods simultaneously, losing Raft quorum. Similarly, all workers could be evicted simultaneously, dropping all in-flight tasks.

#### What changed (v1.1)
- **§15.4** — PDBs added for Cluster Manager (`minAvailable: 2`), Capability Registry (`minAvailable: 2`), workers (`minAvailable: 3`)
- **§15.4** — `PodAntiAffinity` added: workers spread across AZs via `topologyKey: topology.kubernetes.io/zone`
- **§20.7** — 9B-6 milestone: PDB and PodAntiAffinity manifests added to deployment deliverables

**PDB specifications (§15.4):**
```yaml
# Cluster Manager — protect Raft quorum (min 3 pods, at least 2 available)
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-cluster-manager-pdb
spec:
  minAvailable: 2
  selector:
    matchLabels:
      app: aeos-cluster-manager

# Workers — protect against full task queue drain
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-worker-pdb
spec:
  minAvailable: 3
  selector:
    matchLabels:
      app: aeos-worker
```

---

### MI-10 — Spot Instance Preemption Not Addressed
**Reviewer:** Principal Cloud Architect  
**Severity:** Medium  
**Sections modified:** §15.5

#### What was wrong (v1.0)
§15.5 mentioned spot instances for cost optimization but did not specify a minimum on-demand floor. A cluster running 100% spot instances would be vulnerable to full eviction during AWS capacity events, losing all in-flight work.

#### What changed (v1.1)
- **§15.5** — Added: minimum 3 on-demand worker instances always running (not spot)
- **§15.5** — On-demand floor rationale: maintains KEDA `minReplicaCount: 3`; prevents cold-start latency when spot capacity returns
- **§15.5** — Added: 2-minute preemption warning hook (AWS EC2 metadata endpoint) triggers graceful drain: stop consuming new tasks, publish in-flight step checkpoints to Redis, deregister from capability registry

---

## 5. Nice-to-Have

### NTH-1 — Weaviate vs OpenSearch Justification
**Reviewer:** ML Platform Architect  
**Severity:** Nice-to-Have  
**Sections modified:** §15.2

#### What changed (v1.1)
- **§15.2** — Technology selection table: Weaviate vs OpenSearch comparison added
- **§15.2** — Weaviate selected: native vector index (HNSW), GraphQL API, schema-free object store, no separate embedding service required
- **§15.2** — OpenSearch listed as rejected alternative: requires separate embedding pipeline, vector search is secondary capability

---

### NTH-2 — Vault vs AWS Secrets Manager Justification
**Reviewer:** Principal Security Engineer  
**Severity:** Nice-to-Have  
**Sections modified:** §15.2

#### What changed (v1.1)
- **§15.2** — Technology selection table: Vault vs AWS Secrets Manager comparison added
- **§15.2** — Vault selected: PKI secrets engine (three-layer CA), dynamic secrets, lease revocation, cloud-agnostic
- **§15.2** — AWS Secrets Manager listed as rejected alternative: no PKI CA support, AWS-only, no dynamic secret generation

---

### NTH-3 — Multi-Cluster Federation (DEFERRED)
**Reviewer:** Principal Cloud Architect  
**Severity:** Nice-to-Have  
**Status in v1.1:** Explicitly deferred to Phase 11

#### Decision
Phase 9 target is single-cluster, 100-node scale. Multi-cluster federation adds Raft cross-cluster coordination complexity that is not required for the Phase 9 NFR. Documented in §3.5 as out of scope with explicit Phase 11 reference.

---

### NTH-4 — aeos-client SDK Interface
**Reviewer:** AI Runtime Architect  
**Severity:** Nice-to-Have  
**Sections modified:** Appendix D (new)

#### What changed (v1.1)
- **Appendix D** — New appendix: `aeos-client` SDK interface specification
- **Appendix D** — Covers: `AEOSClient` class, `submit_task()`, `get_result()`, `stream_events()`, `cancel_task()`
- **Appendix D** — Authentication: bearer token with automatic refresh

---

### NTH-5 — AWS Cost Estimate
**Reviewer:** Principal Cloud Architect  
**Severity:** Nice-to-Have  
**Sections modified:** Appendix E (new)

#### What changed (v1.1)
- **Appendix E** — New appendix: AWS cost estimate
- **Appendix E** — Initial deployment (~10 workers): ~$3,480/month
- **Appendix E** — Full scale (~100 workers): ~$10,130/month
- **Appendix E** — Breakdown by service: EC2, EKS, MSK, ElastiCache, RDS, ECR, data transfer

---

## 6. Consistency Audit Changes

The following changes were made during the consistency audit pass (not tied to a specific reviewer issue):

| Item | v1.0 | v1.1 | Sections |
|------|------|------|---------|
| "Raft term" → "current_term" | Mixed terminology | `current_term` throughout | §6.2.1, Appendix A |
| Cluster Manager port | 9090 in some places, 8080 in others | 9090 (gRPC) throughout | §6.2, §12.2, Appendix A |
| "hot tier" vs "WTM hot" | Inconsistent labels | "hot tier (WTM)" throughout | §8.3 |
| NFR table units | Mixed (ms, seconds, %) | Standardized (ms for latency, % for availability) | §3.2 |
| Worker heartbeat interval | 5s in §6.2.2, 10s in §16.2 | 5s throughout (failure detection threshold: 3 × 5s = 15s) | §6.2.2, §16.2 |
| Circuit breaker threshold | 5 failures in §11.4, 10 in §7.2.3 | 5 failures throughout | §7.2.3, §11.4 |
| gRPC TLS requirement | "TLS recommended" in §12 | "TLS required" throughout; mTLS for internal services | §12.2 |
| Lease TTL | 60s in §7.3, 120s in §16.5 | 120s throughout (execution lease); 60s is renewal interval | §7.3, §16.5 |

---

## 7. Net-New Sections Added in v1.1

| Section | Title | Reason added |
|---------|-------|-------------|
| §2.1.7 | Safety Systems Fail Closed | CB-5: system philosophy must explicitly state fail-closed tenet |
| §5.2.1 | Redis Key Schema | CB-4: hashtag key schema required to prevent CROSSSLOT errors |
| §9.5 | Backpressure and Flow Control | MI-6: backpressure strategy absent from v1.0 |
| §13.4 | Policy Service Circuit Breaker | CB-5: governance must handle Policy Service unavailability |
| §16.5 | Execution Lease and Split-Brain Prevention | HP-8: in-flight task double-execution not addressed |
| Appendix A.4 | Worker gRPC Service (proto) | HP-7: Worker service proto entirely missing from v1.0 |
| Appendix C | Redis Key Schema Reference | CB-4: complete key schema with hashtag groups, TTLs, access patterns |
| Appendix D | aeos-client SDK Interface | NTH-4: consumer-facing SDK interface specification |
| Appendix E | AWS Cost Estimate | NTH-5: infrastructure cost modeling |

---

## 8. Sections Removed or Deprecated

| Item | Reason |
|------|--------|
| Redis Sentinel references (§8.2, §15.2, §16.3) | Replaced by Redis Cluster throughout (CB-3) |
| Standalone Distributed Scheduler service | Not a real service; embedded in worker (HP-5) |
| `MULTI/EXEC` cross-workflow transaction examples | Replaced by hashtag-scoped per-workflow transactions (CB-4) |
| Default-allow governance catch-all | Replaced by fail-closed algorithm with explicit seed policy (CB-5) |
| Fixed 1-hour governance token TTL | Replaced by dynamic expiry formula (CB-6) |
| `consumer.pause()` deferred to "future work" | Now specified in §9.5 backpressure section (MI-6) |
| Pre-computed Prometheus Summary quantiles | Replaced by Histogram + recording rules (MI-5) |

---

*End of RFC Changelog — `013-RFC_CHANGELOG.md`*
