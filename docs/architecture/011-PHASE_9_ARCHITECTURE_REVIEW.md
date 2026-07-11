# AEOS Phase 9 — Architecture Review Board Report
## RFC-009 Independent Architecture Review

**Review Document:** 011-PHASE_9_ARCHITECTURE_REVIEW.md  
**Subject RFC:** 010-PHASE_9_DRP_SPECIFICATION.md  
**Review Date:** 2026-07-06  
**Status:** REQUIRES REVISION — Score 52/100 (threshold for implementation approval: 95/100)

---

## Review Board Panel

| Reviewer Role | Focus Area |
|--------------|-----------|
| Distinguished Distributed Systems Engineer | Consensus, execution correctness, CAP |
| Principal Cloud Architect | AWS, Kubernetes, cost |
| Staff Platform Engineer | API completeness, lifecycle, interfaces |
| Senior Site Reliability Engineer | Failure recovery, operational complexity |
| Principal Security Engineer | Security, governance, secrets |
| AI Runtime Architect | Agent scheduling, LLM correctness |
| Kubernetes Specialist | Deployment, autoscaling, networking |
| ML Platform Architect | Memory, vector store, scalability |

**Reviewer note:** This panel has no prior exposure to this RFC. We treat it as a submission from an external engineering team. We do not defend its design decisions. Our sole obligation is to find every weakness before a line of implementation code is written.

---

## Table of Contents

1. [Critical Blockers](#critical-blockers) — must be resolved before any implementation begins
2. [High-Priority Issues](#high-priority-issues) — must be resolved before Phase 9 GA
3. [Medium Issues](#medium-issues) — should be resolved before Phase 9 GA
4. [Nice-to-Have Improvements](#nice-to-have-improvements)
5. [Section-by-Section Review](#section-by-section-review)
6. [Architecture Readiness Score](#architecture-readiness-score)

---

## Critical Blockers

> Any one of these issues, left unresolved, will cause production data loss, silent incorrect behavior, or make the system impossible to implement as specified. Implementation of Phase 9B must not begin while any CRITICAL BLOCKER is open.

---

### CB-1: Consumer Group ID Bug — Every Task Delivered to Every Worker

**Severity:** CRITICAL BLOCKER  
**Section:** §9.4, §7.2, §9.6  
**Reviewer:** Distinguished Distributed Systems Engineer

**Finding:**

Section 9.4 specifies the Kafka consumer configuration as:

```python
AIOKafkaConsumer(
    *subscribed_topics,
    group_id=f"aeos-worker-{node_id}",   # ← THE BUG
    ...
)
```

In Apache Kafka, a consumer group is the unit of competitive consumption. All consumers sharing the same `group_id` form a single group — each partition is assigned to exactly one consumer in the group. **Each message is delivered to exactly one consumer.**

When each worker uses a unique `group_id` (e.g., `aeos-worker-abc123`, `aeos-worker-def456`), they form **separate consumer groups**. In this configuration, Kafka delivers every message on `aeos.tasks.*` to **every consumer group independently** — meaning every task is delivered to every worker simultaneously.

For a 10-worker cluster, every task is executed 10 times. This is catastrophic.

**Why it is not caught by the spec's own logic:**  
Section 9.6 correctly describes two Kafka patterns:
- "Fan-out (broadcast): Multiple independent consumer groups each receive every event." — Used for observability, policy reload. *Intentionally uses per-service group IDs.*
- "Work queue (competing consumers): Single consumer group with multiple workers." — Used for task execution.

The task consumer is supposed to use Pattern 2, but the code specifies Pattern 1 behavior. The spec contradicts itself.

**Production Impact:**  
Every task in `aeos.tasks.*` is executed N times (where N = worker count). This produces: N duplicate step results, N checkpoint writes to Redis (race condition on the same key), N governance token validations. The system appears to "work" in single-worker tests but silently corrupts all multi-worker deployments.

**Recommended Redesign:**  
Task consumers (all `aeos.tasks.*` topics) must use a shared group ID:
```python
# Task consumers — shared group, competing consumers
AIOKafkaConsumer(
    "aeos.tasks.critical", "aeos.tasks.high", "aeos.tasks.normal",
    "aeos.tasks.low", "aeos.tasks.batch",
    group_id="aeos-workers",          # ← shared group
    ...
)

# Event/broadcast consumers — per-worker group, fan-out
AIOKafkaConsumer(
    "aeos.events.governance", "aeos.events.cluster",
    group_id=f"aeos-worker-{node_id}",  # ← per-worker group (intentional)
    ...
)
```

The spec must clearly separate these two consumer configurations and explain which topics use which group ID strategy.

**Implementation Cost:** Low — configuration change, but requires updating §9.4, §7.2, and all consumer initialization code in milestones 9B-1 and 9B-3.

**Migration Impact:** None (no existing consumers to migrate).

---

### CB-2: Silent Task Loss — Kafka Offset Committed Before Checkpoint

**Severity:** CRITICAL BLOCKER  
**Section:** §7.2.2, §16.2  
**Reviewer:** Distinguished Distributed Systems Engineer

**Finding:**

Section 7.2.2 states:

> "Kafka offsets are committed after a task is accepted into the WorkerPool (slot acquired), not after the task completes. This ensures a crashed worker does not re-deliver a task that was already checkpointed."

This reasoning is inverted. Committing the offset when the slot is acquired — not when the step completes and is checkpointed — creates a window of silent task loss:

```
Timeline:
  T=0: Worker polls Kafka, receives task T1
  T=1: Worker acquires WorkerPool slot
  T=2: Worker commits Kafka offset for T1  ← offset committed
  T=3: Worker begins executing T1
  T=4: Worker crashes (OOM, SIGKILL, network error)
  T=5: Kafka does NOT redeliver T1 (offset was committed at T=2)
  T=6: T1 has no checkpoint in Redis (crash was at T=4, before completion)
  T=7: Orphan detection (§16.2) scans Redis for workflows with status=RUNNING
       → T1 was never written to Redis as RUNNING (crash before step start)
       → T1 is not detected as orphaned
  T=8: T1 is permanently lost
```

The orphan detection mechanism (§16.2) relies on scanning Redis for `status=RUNNING` workflows. A task that crashed after Kafka offset commit but before any Redis write produces no record in Redis — and is therefore invisible to the orphan scanner. It falls through every safety net.

**The spec's stated reasoning is wrong:** "This ensures a crashed worker does not re-deliver a task that was already checkpointed." — But the checkpoint happens *after* the offset commit. There is no checkpoint at the time of offset commit. The spec confuses "accepted into the pool" with "completed and checkpointed."

**Production Impact:**  
Any worker crash between offset commit and first checkpoint write causes silent, permanent task loss. The probability of this happening increases with:
- Higher LLM call latency (longer execution window)
- Kubernetes node pressure (OOM kills)
- gRPC connection drops to Policy Service mid-execution

**Recommended Redesign:**  
Two options, in order of preference:

**Option A (recommended):** Commit offset only after step is checkpointed to Redis AND next-step task is published to Kafka. Accept at-least-once delivery semantics and make all step executors idempotent (using `step_id` as an idempotency key in Redis).

```
T=0: Poll Kafka, receive T1
T=1: Acquire WorkerPool slot (DO NOT commit offset yet)
T=2: Execute step T1
T=3: Write checkpoint to Redis (atomic)
T=4: Publish next-step tasks to Kafka
T=5: Commit Kafka offset  ← only now
```

**Option B:** Keep offset-before-execution but write a "step accepted" marker to Redis immediately after slot acquisition (`workflow:{wf_id}:step:{step_id}:status = "accepted"`). The orphan scanner checks for `status=accepted` entries older than 60 seconds with no corresponding `status=completed` entry.

**Implementation Cost:** Medium — changes the scheduler loop, requires idempotent step execution, and updates §7.2.2, §16.2.

---

### CB-3: Redis Sentinel vs. Redis Cluster — Mutually Exclusive HA Modes

**Severity:** CRITICAL BLOCKER  
**Section:** §4.3, §15.2, §16.3, §8.2.3  
**Reviewer:** Principal Cloud Architect + Staff Platform Engineer

**Finding:**

The specification uses both "Redis Cluster" and "Redis Sentinel" to describe the same deployment, but these are fundamentally incompatible high-availability modes:

| Feature | Redis Cluster | Redis Sentinel |
|---------|--------------|----------------|
| Sharding | Yes (16384 hash slots across N shards) | No (single dataset) |
| HA mechanism | Automatic master election within cluster | Sentinel quorum-based failover |
| Transaction scope | Single hash slot only | Full dataset |
| Data model | Keys distributed across shards | All keys on one primary |

**Contradictions found:**
- §4.3: "Redis Cluster | Managed (ElastiCache) | 3 shards × 1 replica" → Redis Cluster
- §15.2: "ElastiCache for Redis | `r6g.large`, cluster mode, 3 shards" → Redis Cluster
- §16.3: "ElastiCache for Redis is configured with **Sentinel mode** (1 primary, 2 replicas, 3 Sentinel nodes)" → Redis Sentinel
- §8.2.3: "Redis Cluster with `requirepass` and `min-replicas-to-write 1`" → Redis Cluster config on a Sentinel deployment

These cannot coexist. A single deployment must choose one. This contradiction makes §16.3 (Redis failure recovery) impossible to implement as written.

**Secondary impact:** The WAIT command semantics differ significantly:
- In Redis Cluster: `WAIT` applies only to the shard the key is on, not cluster-wide
- In Redis Sentinel: `WAIT` applies to the single primary's replica set

The memory coherence protocol (§8.5) relies on `WAIT` but doesn't account for which mode is in use.

**Production Impact:**  
The implementation team cannot provision Redis from this spec. The failure recovery procedures in §16.3 apply specifically to Sentinel mode but the deployment specs call for Cluster mode. The two modes have different client behavior, different connection patterns, and different failover characteristics.

**Recommended Redesign:**  
Choose one mode and be consistent. The recommendation is **Redis Cluster** (ElastiCache cluster mode enabled) because:
1. 10 GB of workflow state exceeds a comfortable single-node dataset
2. Cluster mode provides horizontal write scaling
3. Phase 9 NFR (§3.2) requires scaling to 10 GB+ active state

Remove all Sentinel references. Update §16.3 to describe Redis Cluster node failure (automatic slot migration, no Sentinel involved). Update §8.2.3 to use Redis hashtags `{workflow_id}` to ensure MULTI/EXEC keys are on the same slot.

**Implementation Cost:** Low (configuration decision), but requires rewriting §16.3 entirely.

---

### CB-4: Redis MULTI/EXEC Cross-Slot Atomicity — ACID Claims Are False

**Severity:** CRITICAL BLOCKER  
**Section:** §2.2, §5.2.1, §8.2.3  
**Reviewer:** Distinguished Distributed Systems Engineer

**Finding:**

Section 2.2 states: "Workflow execution state follows ACID semantics using Redis transactions."

Section 5.2.1 states: "Every checkpoint write is a Redis MULTI/EXEC transaction. Checkpoints are keyed by `workflow:{workflow_id}:checkpoint:{seq}` [and] `workflow:{workflow_id}:latest_checkpoint`."

In Redis Cluster, MULTI/EXEC transactions are restricted to keys that hash to the same slot. Redis uses CRC16 of the key to determine the slot. Two keys with different prefixes almost certainly land on different slots:

```
workflow:abc123:checkpoint:1     → CRC16("workflow:abc123:checkpoint:1") % 16384 = slot X
workflow:abc123:latest_checkpoint → CRC16("workflow:abc123:latest_checkpoint") % 16384 = slot Y
```

Slot X ≠ slot Y (with overwhelming probability). A MULTI/EXEC transaction spanning these two keys will fail with `CROSSSLOT Keys in request don't hash to the same slot`.

The spec's ACID guarantee is therefore incorrect for Redis Cluster without hashtags.

**Same problem affects:**
- Working Memory: `wm:{session_id}:{key}` and `wm:{session_id}:__keys` — likely different slots
- Step results and next-step publication atomicity

**Production Impact:**  
Either:
(a) The implementation uses MULTI/EXEC naively → runtime errors, checkpoint writes fail → data loss
(b) The implementation abandons MULTI/EXEC → no atomicity → partial checkpoint writes possible → corrupted workflow state on node failure

**Recommended Redesign:**  
Use Redis **hashtags** to force co-location on the same slot. Keys with `{tag}` in the name hash the `tag` portion only:

```
# Old (cross-slot, MULTI/EXEC fails):
workflow:{workflow_id}:checkpoint:{seq}
workflow:{workflow_id}:latest_checkpoint

# New (same slot guaranteed, MULTI/EXEC works):
{wf:{workflow_id}}:checkpoint:{seq}
{wf:{workflow_id}}:latest_checkpoint

# Working memory:
{wm:{session_id}}:key_name
{wm:{session_id}}:__keys
{wm:{session_id}}:__meta
```

Update §5.2.1, §8.2.1, and all Redis key schema definitions throughout the spec.

**Implementation Cost:** Low — key naming change, but affects all Redis schema throughout the spec.

---

### CB-5: Governance Fail-Open Defaults — AI Safety Risk

**Severity:** CRITICAL BLOCKER  
**Section:** §13.3, §2.2  
**Reviewer:** Principal Security Engineer + AI Runtime Architect

**Finding:**

Section 13.3 defines two fail-open behaviors:

```
6. Default: APPROVED (allowlist model with safety net policies)

Timeout: 30 ms budget. If exceeded, fall back to APPROVE with DEGRADED flag.
```

An AI orchestration platform that defaults to APPROVE when (a) no policy matches or (b) the policy evaluation service times out is architecturally unsafe.

**Failure scenario 1 — Policy service overload:**  
Under sustained load, the Policy Service degrades and begins timing out. The governance gate automatically approves all tasks with a "DEGRADED" flag. Enhanced auditing is triggered, but the tasks execute. A malicious or misconfigured workflow that should have been REJECTED executes because the governance gate was under load.

**Failure scenario 2 — Policy configuration error:**  
An admin misconfigures policies (e.g., typo in a condition field, incorrect scope). No policy matches the submitted task. The default APPROVE fires. The task executes without any policy validation.

**Failure scenario 3 — Policy service restart:**  
During a rolling deployment of the Policy Service, a brief window exists where no Policy Service instance is available. All tasks submitted during this window are auto-approved.

**Production Impact:**  
For an AI platform designed to "enforce governance policy uniformly across all nodes" (§1.2), a fail-open governance gate defeats the entire governance architecture. The system cannot claim "Governance gate consistency: 100% of tasks pass policy gate" (§1.4) if the policy gate auto-approves on timeout.

**Recommended Redesign:**  
**The governance gate must be fail-closed.** Change the default behavior to REJECT:

```
evaluate_task(task):
  1. Load all enabled policies sorted by priority
  2. Evaluate conditions top-to-bottom
  3. If a REJECT policy matches → return REJECTED
  4. If an ESCALATE policy matches → return PENDING_APPROVAL
  5. If an explicit APPROVE policy matches → sign token, return APPROVED
  6. Default: REJECTED with reason "no policy explicitly approved this task type"

  Timeout: 30 ms budget. If exceeded → return REJECTED with reason "policy_service_timeout"
  The caller receives a 503 and can retry.
```

For the "everything is approved by default" use case, add a catch-all APPROVE policy with lowest priority:

```
Policy: "default-approve-all"
  scope: "global"
  priority: 9999  # lowest priority
  conditions: []  # matches anything
  action: APPROVE
  reason: "default policy"
```

This makes the fail-safe explicit and auditable. Removing this policy tightens the governance posture without changing the engine.

**Implementation Cost:** Low — behavioral change in policy evaluation algorithm. Requires adding a default policy to the seed data for new deployments.

---

### CB-6: Governance Token Expiry Under Queue Backpressure

**Severity:** CRITICAL BLOCKER  
**Section:** §12.3, §13.1, §7.2.1  
**Reviewer:** Principal Security Engineer + Senior SRE

**Finding:**

Governance tokens are JWTs with 1-hour expiry (§12.3). They are signed when a task is submitted and published to the Kafka task queue. Workers validate the token before execution (§7.2.1, §13.1).

Under backpressure (§10.5), the Kafka task queue can accumulate depth. The spec's HPA scaling metric is `kafka_consumer_lag_sum` — but HPA scaling takes 2+ minutes (§18.4: "HPA scaling: new workers join cluster and receive tasks within **2 minutes**").

Scenario:
```
T=0:    Task submitted, governance token issued (exp = T+3600)
T=0:    Task published to aeos.tasks.critical
T=60:   Cluster under load, queue growing
T=120:  HPA triggers, new workers start
T=180:  New workers join, Kafka rebalance completes
...
T=3600: Task finally dequeued — governance token EXPIRED
T=3601: Worker validates token → REJECTED → task sent to DLQ
```

At sustained high load, tasks can queue for hours. 1-hour token expiry creates silent task loss for any task that queues longer than 1 hour.

The spec claims "0 data loss under single-node failure" (§3.3) but this scenario produces data loss under normal queue backpressure — no failure required.

**Production Impact:**  
Under any sustained overload (the exact scenario that triggers HPA), tasks silently expire in the queue and are sent to DLQ. The caller receives a 202 Accepted at submission time but the task never executes. There is no notification to the caller that the task expired.

**Recommended Redesign:**  
Three options:

**Option A (recommended):** Move governance validation from token signature+expiry to an on-execution call to the Policy Service. The token carries only a `policy_version` and `approval_context` — the worker calls the Policy Service to re-validate on pickup. This eliminates the expiry problem at the cost of one gRPC call per task pickup.

**Option B:** Issue governance tokens with expiry tied to the task's `deadline_ms` + maximum queue wait time (estimate based on current queue depth). For tasks with no deadline, use a 24-hour expiry matching Kafka retention.

**Option C:** Workers that pick up an expired token re-submit the task for governance evaluation (re-call the Policy Service) rather than rejecting. Accept the latency cost of re-evaluation.

**Implementation Cost:** Medium (Option A requires adding gRPC call per task pickup; Option B requires dynamic token expiry calculation).

---

### CB-7: Kafka Partition Count Caps Cluster at 20 Workers, Contradicting 100-Node NFR

**Severity:** CRITICAL BLOCKER  
**Section:** §9.2, §3.2, §7.1  
**Reviewer:** Distinguished Distributed Systems Engineer + ML Platform Architect

**Finding:**

Section 9.2 specifies: "`aeos.tasks.*` topics: Partition count: 20"

Section 3.2 states: "Scale to 100+ nodes"

In Kafka's consumer group model, a consumer receives messages from at most as many partitions as it is assigned. With 20 partitions and 20 workers in the `aeos-workers` group, each worker gets exactly 1 partition. The 21st worker receives 0 partitions and is idle — it cannot consume any tasks.

Adding a 21st worker does not increase throughput. Adding more workers beyond the partition count provides zero benefit. The cluster cannot scale to 100 workers for task consumption.

**This is a direct, unambiguous contradiction between §9.2 (20 partitions) and §3.2 (100+ nodes).**

**Production Impact:**  
The system cannot achieve its stated NFR. Scaling to 21+ workers for task consumption is impossible without changing the topic partition count — and changing partition count requires either topic recreation or online partition reassignment (which causes a rebalance storm and temporary throughput dip).

**Recommended Redesign:**

Set the default partition count to `2 × max_expected_workers`:
```
aeos.tasks.{priority}: partitions = 200  (supports up to 100 workers with 2 partitions each)
```

Document in §9.2: "Partition count must be set at topic creation time to at least 2× the maximum expected worker count. This cannot be safely decreased after creation; only increased."

Also update the Kubernetes HPA (§15.4) to scale based on `kafka_consumer_lag_per_partition` (not total lag) to correctly account for per-worker partition assignment.

**Implementation Cost:** Low (topic configuration change). Medium if the topic was already created with 20 partitions in a staging environment (requires careful partition reassignment).

---

## High-Priority Issues

> These issues will not prevent the system from running but will cause incorrect behavior, security weaknesses, or operational failures in production if not addressed before GA.

---

### HP-1: Raft Term Not Persisted to Durable Storage

**Severity:** HIGH  
**Section:** §6.2.1  
**Reviewer:** Distinguished Distributed Systems Engineer

Raft's safety guarantee requires that each node's current term and vote are persisted to durable storage before responding to any RPC. The spec describes an entirely in-memory Raft state machine with no mention of persistence.

If a Cluster Manager node crashes and restarts, it loses its term counter. It could:
- Grant a vote to a candidate for a term it already voted in (violating the "one vote per term" guarantee)
- Accept log entries from an old leader it previously rejected

This breaks Raft's safety guarantee and can produce split-brain: two leaders elected in the same term.

**Recommended Fix:** Each Cluster Manager node must persist `{current_term, voted_for, log_entries}` to a durable store (local disk or Redis key with `{manager_id}` hashtag) before responding to any `RequestVote` or `AppendEntries` RPC. Add this requirement explicitly to §6.2.1 and Milestone 9B-2.

---

### HP-2: Milestone 9B-2 Directly Contradicts §6.2 Raft Design

**Severity:** HIGH  
**Section:** §20.3, §6.2  
**Reviewer:** Staff Platform Engineer

Section 6.2 states: "The Cluster Manager implements a simplified Raft consensus algorithm... It does not implement full Raft log replication for all state — only for the membership table."

Section 20.3 (Milestone 9B-2) states: "Cluster membership table (**Redis-backed** for simplicity; full Raft log replication in 9B-3)."

These are contradictory:
- §6.2: The Raft log IS the membership table
- §20.3: Redis is the membership table in 9B-2; Raft log replication is added later in 9B-3

If Redis stores the membership table in 9B-2, a Redis failure during 9B-2 deployment takes down cluster membership. More critically, when 9B-3 migrates from Redis to Raft log, there must be a migration protocol — which is never defined.

**Recommended Fix:** Decide on one authoritative design and remove the contradiction. Recommended: Keep Raft log as the authoritative source from day one (even in 9B-2 with a single manager node). Redis is a read-through cache of the Raft log, not the source of truth.

---

### HP-3: Next-Step Kafka Publish Not Atomic with Checkpoint Write

**Severity:** HIGH  
**Section:** §7.3, §7.4  
**Reviewer:** Distinguished Distributed Systems Engineer

The execution protocol (§7.3) specifies:
```
4a. Checkpoint completed step to Redis
4b. Emit TASK_COMPLETED event
4c. Determine next steps (from ExecutionGraph topology)
4d. Publish next-step tasks to Kafka
```

If 4a (checkpoint) succeeds but 4d (next-step publish) fails, the workflow enters a silent deadlock:
- The checkpoint shows this step as completed
- No next-step task exists in Kafka
- No worker will ever pick up the next step
- The workflow appears to be "in progress" forever (until TTL expires)
- The orphan detection only looks for `status=RUNNING` workflows with stale heartbeats — a step that completed but didn't publish its successor has no characteristic Redis state to scan for

**Recommended Fix:** Make checkpoint write and next-step publish a two-phase operation:
1. Write checkpoint with `next_steps_published: false`
2. Publish next-step tasks to Kafka
3. Update checkpoint with `next_steps_published: true`

A recovery scanner periodically checks for checkpoints with `next_steps_published: false` older than N seconds and re-publishes the next-step tasks (using the ExecutionGraph stored in Redis to determine which steps come next).

---

### HP-4: MergeNode Timeout Behavior Undefined — Leaves Parallel Steps Running

**Severity:** HIGH  
**Section:** §7.4.1  
**Reviewer:** AI Runtime Architect

Section 7.4.1: "The MergeNode waits until all prerequisite steps are checkpointed (polling Redis with exponential backoff, max 30 retries, max 60 seconds)."

What happens after 60 seconds? The spec is silent. Possible outcomes:
- MergeNode fails → workflow marked FAILED → but parallel workers are still executing their assigned nodes → resource waste, orphaned steps
- MergeNode retries indefinitely → workflow never terminates → resource leak
- MergeNode proceeds with partial results → downstream steps receive incomplete data

None of these behaviors are specified. For a fan-out/fan-in pattern, the merge semantics on timeout are critical correctness requirements.

**Recommended Fix:** Specify explicitly:
- MergeNode on timeout: emit `MERGE_TIMEOUT` event, mark workflow as FAILED with reason `merge_timeout`, cancel any registered in-flight steps for this workflow by publishing cancellation markers to Redis
- Add `MergeNode.timeout_s` as a required field with a documented default (60 seconds)
- Add `MergeNode.on_timeout` strategy: `FAIL | PARTIAL_RESULTS | WAIT_INDEFINITELY`

---

### HP-5: Pod Anti-Affinity and PodDisruptionBudget Missing

**Severity:** HIGH  
**Section:** §15.4, §6.1  
**Reviewer:** Kubernetes Specialist

The Kubernetes deployment manifests (§15.4) specify worker replicas across 3 zones but include no:
- `PodAntiAffinity` rules — Kubernetes may schedule all 3 workers on one node, negating multi-AZ benefit
- `PodDisruptionBudget` for Cluster Manager — a `kubectl drain` on a node could evict 2 of 3 Cluster Manager pods, breaking Raft quorum
- `PodDisruptionBudget` for Capability Registry — same risk

**Recommended Fix:** Add to all StatefulSet and Deployment specs:

```yaml
# Pod anti-affinity for workers
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: topology.kubernetes.io/zone
          labelSelector:
            matchLabels:
              app: aeos-worker

# PodDisruptionBudget for Cluster Manager
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: aeos-cluster-manager-pdb
spec:
  minAvailable: 2  # Raft quorum requires 2 of 3
  selector:
    matchLabels:
      app: aeos-cluster-manager
```

---

### HP-6: HPA External Metric Requires Undocumented KEDA Dependency

**Severity:** HIGH  
**Section:** §15.4  
**Reviewer:** Kubernetes Specialist

The HPA spec (§15.4) uses:
```yaml
metrics:
  - type: External
    external:
      metric:
        name: kafka_consumer_lag_sum
```

Kubernetes built-in HPA cannot consume arbitrary external metrics. This requires either:
1. **Prometheus Adapter** — exposes Prometheus metrics as Kubernetes custom/external metrics
2. **KEDA (Kubernetes Event-driven Autoscaling)** — native Kafka scaler, much simpler

Neither is mentioned anywhere in the spec. Without one of these, the HPA silently fails with "unable to get external metric" and auto-scaling does not function. This is not a "nice to have" — without it, the HPA is a non-functional YAML file.

**Recommended Fix:** Add KEDA to §15.2 AWS service mapping and §20.7 (9B-6 observability milestone deliverables). Replace the HPA spec with a KEDA `ScaledObject`:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: aeos-worker-scaledobject
spec:
  scaleTargetRef:
    name: aeos-worker
  minReplicaCount: 3
  maxReplicaCount: 20
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: kafka.aeos.svc.cluster.local:9092
        topic: aeos.tasks.normal
        consumerGroup: aeos-workers
        lagThreshold: "500"
```

---

### HP-7: DiskEventBuffer Requires PersistentVolumeClaim

**Severity:** HIGH  
**Section:** §16.4  
**Reviewer:** Senior SRE + Kubernetes Specialist

Section 16.4 describes the `DiskEventBuffer` for Kafka fallback: "Events and traces are buffered in a local `DiskEventBuffer` (ring buffer, max 100K events, stored to local disk)."

Worker pods in a Kubernetes Deployment use ephemeral storage by default. If a worker pod restarts (which is common — OOM, node drain, rolling update) during the Kafka outage, all buffered events on local disk are lost. The entire point of the DiskEventBuffer (durability during Kafka failure) is defeated.

**Recommended Fix:** Either:
(a) Mount a PersistentVolumeClaim for the DiskEventBuffer path — but this turns the stateless Deployment into something with stateful concerns, complicating pod scheduling
(b) Change the DiskEventBuffer to a Redis-backed buffer using a separate Redis key namespace — since Redis is still available during a Kafka outage
(c) Accept that events during a Kafka outage may be lost (acknowledge this explicitly) and rely on the checkpoint mechanism for correctness

Option (c) is the most honest: the DiskEventBuffer provides best-effort event durability, not a guarantee. The spec should not imply otherwise.

---

### HP-8: Split-Brain + Shared Redis Enables Double-Execution

**Severity:** HIGH  
**Section:** §16.5  
**Reviewer:** Distinguished Distributed Systems Engineer

Section 16.5 describes split-brain behavior: "Workers in the minority partition stop accepting new workflows."

However, workers in the minority partition can still:
1. Read from Redis (Redis is not partitioned with the workers)
2. Continue executing already-checkpointed steps from existing workflows
3. Write results to Redis

Meanwhile, the majority partition's Cluster Manager detects these workflows as orphaned (§16.2) and re-queues them. Workers in the majority partition begin executing the same workflows.

Result: The same workflow step is executed concurrently by workers on both sides of the partition, both writing to the same Redis keys. The last writer wins — but which result is correct is undefined.

**Recommended Fix:** Workers must fence their step executions against concurrent re-execution. When a worker picks up a step from the queue, it must atomically acquire a lease:

```
SETNX workflow:{wf_id}:step:{step_id}:lease worker_node_id EX 120
```

Only the worker that successfully sets the lease executes the step. If the lease already exists (set by a concurrent worker on the other side of the partition), the second worker skips the step. When the partition heals and the lease expires, normal recovery resumes.

---

### HP-9: Network Policy Objects Missing — Internal Services Reachable by Any Pod

**Severity:** HIGH  
**Section:** §15.3, §12.1  
**Reviewer:** Principal Security Engineer

The VPC security groups (§15.3) provide external boundary security. But within the Kubernetes cluster, without `NetworkPolicy` objects, any pod in any namespace can reach any other pod. A compromised worker pod can:
- Call the Policy Service gRPC directly (bypass queue)
- Read all Redis keys (access other sessions' working memory)
- Subscribe to all Kafka topics (read all workflow data and governance decisions)

The threat model (§12.1) assumes "An attacker who compromises one worker node should not be able to read another worker's in-flight task data." This claim requires NetworkPolicy enforcement.

**Recommended Fix:** Define NetworkPolicy objects for all services:

```yaml
# Workers may only reach: Redis, Kafka, Cluster Manager, Policy Service, Capability Registry
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: aeos-worker-egress
spec:
  podSelector:
    matchLabels:
      app: aeos-worker
  policyTypes: [Egress]
  egress:
    - to: [{podSelector: {matchLabels: {app: aeos-cluster-manager}}}]
      ports: [{port: 50051}]
    - to: [{podSelector: {matchLabels: {app: aeos-policy-service}}}]
      ports: [{port: 50054}]
    # ... etc.
```

---

### HP-10: CA Hierarchy Lacks Intermediate CA — Full Compromise If Vault Root Exposed

**Severity:** HIGH  
**Section:** §12.2  
**Reviewer:** Principal Security Engineer

Section 12.2 specifies a "Self-signed root CA, 10-year validity" stored in Vault. All node and service certificates are issued directly from this root CA.

Standard PKI hygiene requires: **Root CA (offline) → Intermediate CA (online, Vault) → Leaf Certs**.

Without an intermediate CA:
- If the self-signed root CA in Vault is compromised (Vault breach, operator error, misconfiguration), all certificates issued from it must be considered compromised
- There is no certificate chain to break — you cannot revoke the root without revoking all trust
- The CRL/OCSP infrastructure cannot function without an intermediate to revoke

**Recommended Fix:** Add an intermediate CA layer:
```
Root CA: Self-signed, 20-year validity, stored OFFLINE (not in Vault)
  ↓ Issues:
Intermediate CA: Signed by Root, 2-year validity, stored in Vault PKI engine
  ↓ Issues:
Leaf certs: Signed by Intermediate, 24-hour validity, issued per node/service
```

Update §12.2 to specify this hierarchy. The `pki/issue/aeos-node` Vault path should be configured on the intermediate PKI mount, not the root.

---

### HP-11: Join Protocol Sequencing Bug — Capabilities Advertised After Partition Assignment

**Severity:** HIGH  
**Section:** §6.2.3  
**Reviewer:** Staff Platform Engineer

The join protocol (§6.2.3) sequences as:
```
Step 3. Manager validates: version compatibility, zone quota, capability conflict
Step 4. Manager assigns Kafka partitions
Step 6. Manager responds: JoinResponse(assigned_partitions, topology_snapshot)
Step 8. Worker registers capabilities with Capability Registry
```

The Cluster Manager assigns Kafka partitions (step 4) before the worker has registered its capabilities (step 8). The partition assignment in a sophisticated system might be zone-aware or capability-aware (e.g., route GPU tasks only to GPU-capable partitions). But the Manager has no capability information at step 4.

Additionally, if step 8 fails (Capability Registry unreachable), the worker has already been assigned partitions and may begin consuming tasks — for capabilities it has not yet advertised. Other workers routing tasks based on capability lookups would not route to this worker, but the worker is consuming from the shared queue.

**Recommended Fix:** Reorder the join protocol:
```
Step 3. Manager validates: version compatibility, zone quota
Step 4. Worker registers capabilities with Capability Registry (before partition assignment)
Step 5. Worker reports registered capabilities to Manager
Step 6. Manager assigns Kafka partitions (optionally using capability info for zone-aware assignment)
Step 7. Manager replicates membership change to followers
Step 8. Manager responds: JoinResponse(assigned_partitions, topology_snapshot)
Step 9. Worker subscribes to Kafka partitions
Step 10. Worker sends JoinComplete
Step 11. Manager marks worker "active"
Step 12. Worker transitions kernel to RUNNING
```

---

## Medium Issues

---

### MI-1: `ClusterMember` Dataclass Missing `suspected` Status

**Section:** §6.2.2, §6.2.5  
**Reviewer:** Staff Platform Engineer

Section 6.2.5 describes a "suspected dead" intermediate state. The `ClusterMember` dataclass (§6.2.2) only defines: `"joining" | "active" | "draining" | "dead"`. The `suspected` state is referenced in prose but not in the data model. Implementation teams will invent their own field name, creating inconsistency.

**Fix:** Add `"suspected"` to the `ClusterMember.status` enum and define the transition: `active → suspected (after 15s no heartbeat) → dead (after 30s no heartbeat)`.

---

### MI-2: LLM Response Cache Keys Include Workflow-Specific Context

**Section:** §17.2  
**Reviewer:** AI Runtime Architect

Section 17.2: "The cache key is `sha256(model + prompt + temperature + max_tokens)`."

AI agent prompts in AEOS are not static — they include session context, prior step results, and dynamic tool outputs injected by the CognitiveAgent's 11-step lifecycle. Two prompts with the same `task_description` will produce different full prompts. The cache key as specified would rarely produce hits.

Worse: if two different workflows happen to produce the same cache key (same task at the same step), the second workflow receives the first workflow's output — potentially exposing cross-workflow data.

**Fix:** The LLM cache must be opt-in per prompt, explicitly enabled only for prompts known to be fully deterministic. Add a `cacheable: bool` flag to `LLMRequest`. Default: `cacheable=False`. The spec's 15-20% cache hit rate estimate should be revised downward significantly.

---

### MI-3: Prometheus Metric Types — Histogram vs. Summary Confusion

**Section:** §14.2  
**Reviewer:** Senior SRE

Several metrics in §14.2 are specified as:
```
aeos_workflow_duration_seconds{quantile="0.5|0.95|0.99"}
aeos_step_duration_seconds{agent_type="...", quantile="0.5|0.95|0.99"}
```

This is a Prometheus **Summary** metric format (pre-computed quantiles as label values). However, Summaries cannot be aggregated across instances — `sum(aeos_workflow_duration_seconds{quantile="0.99"})` across 20 workers produces a meaningless number, not the cluster-wide p99.

The AlertManager rule (§14.5) uses `histogram_quantile(0.99, aeos_step_duration_seconds)` — which is a **Histogram** query function. This function only works if `aeos_step_duration_seconds` is a Histogram (exposing `_bucket`, `_count`, `_sum` metrics), not a Summary.

The spec simultaneously specifies Summary labels and Histogram queries — these are incompatible.

**Fix:** Use Histograms for all latency metrics:
```
aeos_workflow_duration_seconds_bucket{le="0.1|0.5|1|2|5|10|30|60|+Inf"}
aeos_workflow_duration_seconds_count
aeos_workflow_duration_seconds_sum
```
Use `histogram_quantile(0.99, rate(aeos_workflow_duration_seconds_bucket[5m]))` in AlertManager rules and Grafana dashboards.

---

### MI-4: Weaviate Access Count Write Amplification

**Section:** §8.3.2  
**Reviewer:** ML Platform Architect

The `MemoryEntry` schema includes `access_count` and `last_accessed` fields. The query protocol (§8.3.3) updates these on every read: "Update access_count and last_accessed for returned entries."

At 10M embeddings with frequent retrieval (e.g., 100 queries/second each returning 10 results), this creates 1000 writes/second to Weaviate just from access tracking — doubling the write load on the vector store for no correctness benefit (LRU eviction can be implemented without per-read writes, using approximate counting or batch updates).

**Fix:** Remove `access_count` and `last_accessed` from the per-read hot path. Implement LRU eviction using a separate counter aggregation job that runs periodically (e.g., every 5 minutes) and updates counts in batch.

---

### MI-5: Episodic Write-Read Consistency Gap

**Section:** §8.4.2, §8.4.3  
**Reviewer:** Staff Platform Engineer

Episodic writes are fire-and-forget via Kafka (§8.4.2): worker → Kafka → `episodic-writer` consumer → PostgreSQL. This pipeline has non-zero latency (seconds to tens of seconds under load).

Episodic reads are synchronous from a PostgreSQL read replica (§8.4.3).

There is no acknowledgment in the spec that a read immediately after a write will return stale data. If an agent writes an episode and then reads it back to build context for the next step, it may see its own episode missing. This can cause agents to repeat work they believe they haven't done.

**Fix:** Document the episodic write-read staleness window explicitly. For workflows where read-after-write consistency is required, provide a synchronous write path: `POST /episodic/write/sync` that bypasses Kafka and writes directly to PostgreSQL.

---

### MI-6: RBAC Role Revocation Takes Up to 5 Minutes

**Section:** §12.4  
**Reviewer:** Principal Security Engineer

RBAC role assignments are cached in Redis with 5-minute TTL (§12.4). A revoked role continues to be effective for up to 5 minutes. For a security-critical event (e.g., revoking a compromised operator account), 5 minutes is unacceptably long.

**Fix:** On role revocation (via admin API), publish a Kafka event to `aeos.events.cluster` with type `RBAC_REVOKED`. All API gateway instances subscribe to this event and immediately invalidate the Redis cache for the affected user. Maximum propagation time: Kafka delivery latency (~100 ms) + event processing (~10 ms) = < 1 second.

---

### MI-7: Cross-Topic Event Ordering Not Specified

**Section:** §9.5  
**Reviewer:** Distinguished Distributed Systems Engineer

Section 9.5 guarantees ordering within a partition (within a topic). But the observability and replay systems consume multiple topics simultaneously (`aeos.events.workflow`, `aeos.events.node`, `aeos.events.agent`). When a `NODE_COMPLETED` event (on `aeos.events.node`) and `WORKFLOW_COMPLETED` event (on `aeos.events.workflow`) are published simultaneously, their relative order is undefined across topics.

For the Workflow Detail Grafana dashboard (§14.6) to render a correct Gantt chart, events must be temporally ordered. The spec provides no mechanism for this.

**Fix:** All events must include a monotonic `sequence_number` or `published_at_nanos` (nanosecond timestamp) in the envelope. Consumers sort by this field when rendering cross-topic timelines. Add this field to the `DistributedEvent` dataclass in §9.3.

---

### MI-8: No Database Migration Tooling Specified

**Section:** §20 (Roadmap), §8.4, §12.4  
**Reviewer:** Senior SRE + Staff Platform Engineer

The spec references PostgreSQL schemas (episodes, workflow_outcomes, rbac_assignments, audit_log, policy table) but never mentions schema migration management. As Phase 9 evolves and schemas change:
- How are migrations applied during rolling deploys?
- Which service "owns" each schema and applies migrations?
- What happens if a migration fails mid-apply?

Without a migration tool (Alembic for Python, or Flyway), schema changes require manual coordination. This is a known source of production incidents.

**Fix:** Add Alembic as a dependency to the `aeos-worker` project. Define a migration runner job that runs during deployment (Kubernetes Job, runs before new pods receive traffic). Add to §20.7 (9B-6 deliverables): "Alembic migration framework with initial schema migrations for all PostgreSQL tables."

---

### MI-9: Vault Self-Hosted Complexity Not Justified vs. AWS Secrets Manager

**Section:** §15.2  
**Reviewer:** Principal Cloud Architect

Self-hosted Vault on EKS (HA, 3 nodes, §15.2) is significantly more complex to operate than AWS Secrets Manager + Parameter Store. HashiCorp Vault requires: initialization and unsealing procedures, operator key shares management, audit log maintenance, backup procedures, upgrade runbooks, and high availability configuration.

For the specific use cases in this spec (static secrets, dynamic database credentials, PKI), AWS Secrets Manager + ACM Private CA covers ~80% of the use cases with significantly lower operational burden.

**Fix:** Explicitly justify Vault over AWS Secrets Manager in §15.2. If the justification holds (e.g., multi-cloud portability, complex dynamic secret workflows), acknowledge the operational cost. If not, replace Vault with:
- AWS Secrets Manager: for static secrets (LLM API keys, Redis password)
- RDS IAM authentication: for database credentials (eliminates password rotation entirely)
- ACM Private CA: for internal TLS certificates (replaces Vault PKI)

---

### MI-10: Gateway Routes Workers Directly — Bypasses Authentication for Health Endpoint

**Section:** §15.3, §6.4  
**Reviewer:** Principal Security Engineer

Section 15.3: `sg-workers` allows inbound from `sg-alb` on port 8000. Port 8000 is described in §6.4 as "Worker internal health/metrics." Making this port reachable from the ALB means the Prometheus `/metrics` endpoint (port 8000) is reachable from the internet (via ALB → sg-workers:8000).

The `/metrics` endpoint exposes internal operational data (queue depths, active workflow counts, error rates) that should not be publicly accessible. Additionally, `aeos_agent_llm_tokens_total` reveals LLM usage patterns; `aeos_workflow_steps_total{workflow_id="..."}` reveals internal workflow IDs.

**Fix:** Remove port 8000 from `sg-workers` inbound rules for `sg-alb`. Prometheus scraping should use a dedicated monitoring security group (`sg-monitoring`) with access only from Prometheus pods, not from the ALB.

---

## Nice-to-Have Improvements

---

### NTH-1: Define JSON→Avro Migration Path Now

**Section:** §9.3  
**Reviewer:** Staff Platform Engineer

The spec defers Avro migration to Phase 10. But the migration cost increases significantly after Phase 9 GA: all consumers must be updated simultaneously (no backward compatibility between JSON and Avro unless Schema Registry is configured with compatibility settings). Planning the schema ID structure and compatibility policy now costs nothing.

**Suggestion:** Add an Avro `schema_id` field to the JSON envelope in Phase 9. When Phase 10 migrates to Avro, this field is already established. Consumers can use `schema_id` to detect format and handle both.

---

### NTH-2: Define gRPC API Versioning Policy

**Section:** Appendix A  
**Reviewer:** Staff Platform Engineer

All proto packages use `v1`. No policy exists for `v2` evolution. gRPC backward compatibility allows adding optional fields, but renaming or removing fields requires versioned packages. Without a documented policy, the first breaking change will require a flag day across all services.

**Suggestion:** Add a versioning policy to Appendix A: "All protobuf fields are optional by default. Breaking changes require a new package version (e.g., `aeos.cluster.v2`). Both versions are served simultaneously for 2 release cycles before the old version is deprecated."

---

### NTH-3: Define the `aeos-client` SDK Interface

**Section:** §19.4  
**Reviewer:** ML Platform Architect

Section 19.4 references "the Python `aeos-client` SDK" but no SDK exists in prior phases and no interface is defined in this spec. If external SDK consumers are planned, the API should be defined now (even if implementation is Phase 10).

**Suggestion:** Add Appendix D: aeos-client SDK interface definition with the 5 core operations: `submit_workflow`, `get_workflow_status`, `cancel_workflow`, `stream_workflow_events`, `list_capabilities`.

---

### NTH-4: LLM API Key Rotation Needs Automation Plan

**Section:** §12.5  
**Reviewer:** Principal Security Engineer

LLM API key rotation is listed as "Manual (provider-dependent)." For a security-first platform, manual rotation is a risk: rotation schedules slip, keys remain active indefinitely, and compromised keys may not be detected until a billing anomaly surfaces.

**Suggestion:** Document a rotation checklist. For OpenAI and Anthropic: create new key → update Vault → rolling deploy workers to pick up new key → verify no errors → delete old key. Automate with a scheduled GitHub Actions workflow that issues a rotation reminder and links to the checklist.

---

### NTH-5: Cost Estimate for Phase 9 AWS Infrastructure

**Section:** §15  
**Reviewer:** Principal Cloud Architect

The spec specifies AWS services but provides no cost estimate. For a project planning document, the absence of a cost estimate means stakeholders cannot make informed build/buy decisions.

Rough estimate (to be formalized):
- EKS cluster (5 `c5.2xlarge` workers, on-demand): ~$730/month
- ElastiCache Redis Cluster (3× `r6g.large`): ~$450/month
- Amazon MSK (3× `kafka.m5.large`): ~$540/month
- RDS PostgreSQL (`db.r6g.large`, Multi-AZ): ~$280/month
- Weaviate on EKS (3× `r5.xlarge`): ~$720/month
- Vault HA on EKS (3× `t3.medium`): ~$90/month
- Jaeger + Prometheus + Grafana on EKS: ~$150/month
- Cross-AZ data transfer: ~$100-300/month (estimate)
- **Estimated total: ~$3,060–3,260/month**

**Suggestion:** Add Appendix E with a detailed cost breakdown. This also motivates the Vault → AWS Secrets Manager switch: saves ~$90/month on EC2 + eliminates operational complexity.

---

## Section-by-Section Review

| Section | Grade | Key Strengths | Key Gaps |
|---------|-------|--------------|---------|
| §1 Executive Vision | B+ | Clear problem statement, success criteria well-defined | Success criterion "0 data loss" contradicted by CB-2, CB-6 |
| §2 System Philosophy | B | Good CAP positioning, actor model noted | Redis "ACID" claim is technically incorrect (CB-4) |
| §3 NFR | B+ | Comprehensive, measurable targets | 100-node NFR contradicted by 20-partition limit (CB-7) |
| §4 Layered Architecture | C+ | Clean diagram, layer responsibilities clear | Scheduler placement contradicts §7.2; REST vs gRPC inconsistency |
| §5 Runtime Subsystems | B | Good adapter substitution pattern | JOINING phase ordering unclear; 7th vs 5th phase numbering error |
| §6 Cluster Design | B- | Thorough join/leave protocols | CB-3 (Sentinel/Cluster), HP-1 (Raft term), HP-2 (Redis vs Raft), HP-11 (join ordering) |
| §7 Distributed Execution | D | Offset commit strategy correctly identified as key | CB-2 (offset before checkpoint), HP-3 (next-step atomicity), HP-4 (merge timeout) |
| §8 Distributed Memory | B- | Tier-appropriate consistency model is good | CB-4 (MULTI/EXEC cross-slot), CB-3 (Sentinel/Cluster), MI-4 (write amplification) |
| §9 Event Fabric | D | Topic taxonomy and retention well-designed | CB-1 (consumer group ID — catastrophic), MI-7 (cross-topic ordering) |
| §10 Resource Management | B+ | Backpressure chain is well-designed | LLM rate limiter per-worker vs. per-cluster inconsistency (§10.4) |
| §11 Capability Federation | B | Good taxonomy, circuit breaker integration | Inconsistency with Phase 8.3 circuit breaker states |
| §12 Security | B- | mTLS lifecycle is comprehensive | CB-5 (fail-open), HP-10 (CA hierarchy), HP-9 (NetworkPolicy), MI-6 (RBAC TTL) |
| §13 Governance | D | Policy hot-reload via Kafka is good | CB-5 (fail-open default + timeout), CB-6 (token expiry), policy version mismatch |
| §14 Observability | B | Three-pillar model, AlertManager rules | MI-3 (histogram vs summary confusion), missing `aeos_governance_duration_seconds` metric definition |
| §15 Cloud Architecture | B- | Good VPC design, security groups | HP-6 (KEDA missing), HP-9 (NetworkPolicy), MI-10 (port 8000 exposure), HP-5 (PDB) |
| §16 Failure Analysis | C+ | Taxonomy is comprehensive | HP-8 (split-brain/Redis double execution), HP-7 (DiskEventBuffer ephemeral) |
| §17 Performance | B | LLM critical path analysis is realistic | MI-2 (LLM cache danger), MI-3 (metric type), cluster-wide rate limiter gap |
| §18 Testing | B+ | Test pyramid, chaos plan are solid | Missing: contract testing between gRPC services; no load test for Raft under partition |
| §19 Migration | A- | Phased migration with clear rollback table | Phase M6 (LTM data migration) underspecified — embedding format compatibility unverified |
| §20 Roadmap | B | Clear milestones with success gates | HP-2 (9B-2 contradicts §6.2), 19-week timeline aggressive for 4-person team |

---

## Architecture Readiness Score

### Scoring Methodology

Each of the 20 review dimensions is scored 0–5. The composite score is then mapped to 0–100.

| Dimension | Score /5 | Notes |
|-----------|---------|-------|
| Architectural consistency | 2 | Multiple contradictions (Sentinel/Cluster, Raft/Redis, Scheduler placement) |
| API and interface completeness | 2 | Proto messages missing for half the defined RPCs |
| Lifecycle correctness | 3 | JOINING phase adequate; join sequencing bug; phase numbering error |
| State machine validation | 2 | Circuit breaker duality; Raft term persistence missing |
| Distributed execution correctness | 1 | Offset-before-checkpoint is a fundamental correctness failure |
| Consensus protocol validation | 2 | Raft term persistence missing; 9B-2/§6.2 contradiction |
| Failure recovery | 2 | Split-brain/Redis gap; DiskEventBuffer ephemeral; next-step publish gap |
| CAP theorem implications | 2 | Redis ACID claim false; governance token expiry makes CP claim false |
| Event ordering | 1 | Consumer group ID bug is catastrophic; cross-topic ordering unspecified |
| Memory consistency | 3 | WAIT command approach is correct; cross-slot atomicity unaddressed |
| Scheduler correctness | 2 | Same consumer group bug; HPA dependency chain undocumented |
| Cloud architecture | 3 | Good VPC design; Sentinel/Cluster confusion; Vault complexity |
| Kubernetes deployment | 2 | Missing PDB, NetworkPolicy, PodAntiAffinity, KEDA |
| AWS cost optimization | 3 | Adequate but Vault/Weaviate self-hosting not justified |
| Security | 3 | Solid foundation; CA hierarchy, fail-open, NetworkPolicy gaps |
| Governance | 1 | Fail-open default is an AI safety risk; token expiry creates silent loss |
| Performance | 3 | LLM critical path analysis good; cache key danger; metric type mismatch |
| Scalability | 2 | Partition count caps cluster at 20 workers, contradicting 100-node NFR |
| Operational complexity | 3 | High but acknowledged; missing Alembic, KEDA, migration tooling |
| Long-term maintainability | 3 | JSON→Avro risk; no gRPC versioning policy; SDK undefined |

**Sum:** 46/100  
**Architecture Readiness Score: 52/100** *(adjusted +6 for overall structural completeness and breadth of coverage, which significantly exceeds typical RFC submissions)*

### Score Breakdown

| Category | Weight | Score |
|---------|--------|-------|
| Correctness (CBs resolved would add +30) | 35% | 18/35 |
| Security & Governance | 20% | 9/20 |
| Operational readiness | 20% | 12/20 |
| Completeness | 15% | 8/15 |
| Scalability | 10% | 5/10 |

---

### Required Actions Before Implementation Begins

#### Phase 1 — Resolve Critical Blockers (estimated 2 weeks of design work)

All 7 Critical Blockers must be resolved in the RFC before Milestone 9B-1 begins:

| ID | Issue | Owner | Design Change Required |
|----|-------|-------|----------------------|
| CB-1 | Consumer group ID | Distributed Systems Lead | Separate task vs. event consumer configurations |
| CB-2 | Offset before checkpoint | Distributed Systems Lead | Define offset commit protocol; make executors idempotent |
| CB-3 | Sentinel vs. Cluster | Cloud Architect | Choose one; rewrite §16.3 |
| CB-4 | MULTI/EXEC cross-slot | Distributed Systems Lead | Define Redis hashtag key schema throughout |
| CB-5 | Governance fail-open | Security Lead | Rewrite §13.3 evaluation algorithm |
| CB-6 | Token expiry | Security + Runtime Lead | Define token lifetime strategy for queued tasks |
| CB-7 | Partition count | Platform Lead | Set partitions ≥ 200; update §9.2 and HPA |

#### Phase 2 — Resolve High-Priority Issues (estimated 1 week of design work)

All 11 High-Priority issues should be resolved before Milestone 9B-2:

HP-1 through HP-11 as detailed above.

#### Phase 3 — Resolve Medium Issues (may be addressed during implementation)

MI-1 through MI-10 may be addressed during implementation milestones but must be resolved before Phase 9 GA.

---

### Re-Review Threshold

A revised RFC incorporating all Critical Blocker fixes and High-Priority issue resolutions should achieve a score in the range 82–90/100. A subsequent narrowly-scoped re-review of the revised RFC will determine readiness for Milestone 9B-1. 

**Implementation of Phase 9B must not begin until Architecture Readiness Score ≥ 95/100.**

---

*Architecture Review Board Report — Phase 9A.5*  
*Review completed: 2026-07-06*  
*Next action: RFC author team to address Critical Blockers and submit RFC v1.1 for re-review*
