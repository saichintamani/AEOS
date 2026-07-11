# AEOS Phase 9 DRP — Distributed Correctness Specification

**Document:** `024-DISTRIBUTED_CORRECTNESS_SPEC.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## Purpose

This document provides formal guarantees for every distributed correctness property in AEOS Phase 9. For each property, it specifies: the precise guarantee (what is promised and what is NOT), the preconditions under which the guarantee holds, the proof sketch (why the design achieves it), and the test cases that verify it.

This document is authoritative for resolving disputes about expected behavior in distributed failure scenarios.

---

## DC-001 — Leader Election Safety

### Guarantee
At most one Cluster Manager node is the leader for any given Raft term.

### Formal Statement
```
∀ term T, ∀ nodes N1, N2:
  leader(N1, T) ∧ leader(N2, T) → N1 = N2
```

### Preconditions
- Three Cluster Manager nodes with persistent WAL storage
- Each node fsyncs `currentTerm` and `votedFor` before responding to RequestVote
- Network may be asynchronous (messages may be delayed or lost) but not Byzantine (no forged messages)

### Proof Sketch
Raft ensures at most one leader per term via the vote mechanism:
1. A node becomes leader only after receiving votes from a quorum (≥ 2 of 3 nodes)
2. Each node votes for at most one candidate per term (enforced by fsynced `votedFor`)
3. Two candidates cannot both receive quorum votes in the same term (pigeonhole: 3 nodes, 2 needed by each, only 3 total votes available — both getting 2 requires 4 votes)
4. Therefore: at most one candidate wins election per term
5. `votedFor` is fsynced before responding: a crash-restart cycle cannot result in a node voting twice in the same term

### What Is NOT Guaranteed
- A leader may lose contact with followers without knowing it (it remains "leader" locally); another leader may be elected in a new term. This is safe because any decisions made by the old leader in the new term will fail (followers reject messages with stale terms). The old leader's uncommitted entries are overwritten.

### Test Cases
- CT-T4-001 (split-brain prevention)
- FI-003 (network partition), FI-005 (CM leader failure)

---

## DC-002 — Consensus Safety (Log Matching)

### Guarantee
All Cluster Manager state machines that apply the same log index apply the same log entry. Committed entries are never overwritten.

### Formal Statement
```
∀ entries E1 at index I on node N1, E2 at index I on node N2:
  committed(E1) ∧ committed(E2) → E1 = E2
```

### Preconditions
- Raft log entries committed only after quorum acknowledgment
- Raft leader completeness: a candidate with an incomplete log cannot win election (log completeness check in RequestVote handler)

### Proof Sketch
Raft provides Log Matching via:
1. **Commitment rule**: An entry is committed only when a quorum has appended it; the quorum overlaps any future quorum, so committed entries are always present in the winning candidate's log
2. **Log completeness check**: A RequestVote is granted only if the candidate's log is at least as up-to-date as the voter's (prevents leaders with missing committed entries)
3. **AppendEntries consistency check**: Followers reject entries that don't match the leader's `prevLogIndex/prevLogTerm`, forcing sequential consistency

### What Is NOT Guaranteed
- Liveness during extended network partitions. If no quorum is reachable, no new entries are committed (Raft is a CP system — availability is sacrificed for consistency).

---

## DC-003 — Exactly-Once Semantic for Committed Entries

### Guarantee
A Raft log entry is applied to the state machine exactly once, even if AppendEntries is retried.

### Formal Statement
```
∀ log entry E, ∀ node N:
  applied_count(N, E) = 1 after E is committed
```

### Proof Sketch
1. Raft assigns each log entry a unique (term, index) pair
2. The state machine tracks `lastApplied` index; entries are applied sequentially
3. Duplicate AppendEntries (retried RPCs) are idempotent: a follower that already has entry (T, I) rejects a second AppendEntries for the same (T, I)

---

## DC-004 — Step Execution At-Least-Once

### Guarantee
Every accepted task step is executed at least once, or placed in the dead-letter queue.

### Formal Statement
```
∀ task T with status ≠ SUBMITTED:
  eventually(
    executed(T) ∨ in_dead_letter(T) ∨ cancelled(T)
  )
```

### Preconditions
- Kafka retains messages until explicitly committed (offset management by workers)
- Orphan scanner runs continuously
- Dead-letter queue exists and is durable

### Proof Sketch
A task can only be "lost" if:
1. Its Kafka message is deleted before delivery — impossible: we commit offset only after Phase 2 checkpoint; Kafka retains messages until offset is committed
2. Its Redis checkpoint is lost — impossible: Phase 1 uses MULTI/EXEC (atomic); Redis Cluster replicates synchronously within a shard; data survives single-primary failure
3. The orphan scanner fails to recover it — the orphan scanner has three independent recovery patterns covering all intermediate states; a step with any Phase 1 state visible in Redis will be recovered

### What Is NOT Guaranteed
- Exactly-once for external side effects (LLM calls, tool calls). The guarantee is at-least-once. Idempotency keys prevent duplicate execution of AEOS steps but cannot prevent a downstream LLM provider from processing a duplicate request if the original request completed on their side but the response was lost.

---

## DC-005 — Idempotency of Step Execution

### Guarantee
Executing a step twice with the same idempotency key produces the same result as executing it once, and the result is returned without re-invoking the underlying capability.

### Formal Statement
```
∀ step S, ∀ executions E1 at t1, E2 at t2 (t1 < t2, same idem_key):
  idem_key_exists(S) at t2 →
    result(E2) = result(E1) ∧
    capability_invocations(E2) = 0
```

### Preconditions
- Idempotency key written in Phase 1 (atomic with result)
- Idempotency key TTL (24h) has not expired
- Step input (task envelope) is deterministic — no non-deterministic inputs that would change the result

### Proof Sketch
1. Phase 1 MULTI/EXEC writes `result` and `idem` atomically
2. Before any execution, the idem key is checked: if present, cached result returned
3. The SETNX lease prevents concurrent execution (only one worker at a time)
4. Together: if the step completed (idem key present), no re-execution occurs; cached result returned

### What Is NOT Guaranteed
- Idempotency is not provided for steps where the step input changes between retries (e.g., a dynamic prompt that includes the current timestamp). Such steps MUST set `cacheable=False` and must not rely on the idem key for correctness.

---

## DC-006 — Two-Phase Checkpoint Durability

### Guarantee
If the Phase 1 checkpoint completes, the step result is durable and will survive any single-component failure (worker crash, single Redis primary failure, single Kafka broker failure).

### Formal Statement
```
phase1_checkpoint_complete(step S) →
  ∀ single failure F:
    result_recoverable(S, after F)
```

### Preconditions
- Redis Cluster: replication factor ≥ 2 (minimum 3 primaries + 3 replicas)
- Kafka: replication factor ≥ 3 (minimum 3 brokers for MSK)
- Phase 1 uses MULTI/EXEC (atomic within hash slot)

### Proof Sketch
After Phase 1 completes:
- `{wf:X}:step:N:result` exists in Redis Cluster → survives single primary failure via replica promotion
- `{wf:X}:step:N:status = COMPLETED` → orphan scanner will not retry
- `{wf:X}:step:N:idem` exists → any retry returns cached result

After Phase 2 completes:
- Next tasks published to Kafka (replication factor 3 → survives single broker failure)
- `next_published=true` → orphan scanner (Pattern 3) will not re-publish

Offset committed after Phase 2: if committed and worker crashes, Kafka delivers next tasks to other workers; idem key prevents re-execution.

---

## DC-007 — Split-Brain Step Execution Prevention

### Guarantee
No workflow step is executed concurrently by two different workers, regardless of network partition state.

### Formal Statement
```
∀ step S, ∀ workers W1, W2:
  ¬(executing(W1, S) ∧ executing(W2, S))
```

### Preconditions
- Redis Cluster is reachable from both workers during lease acquisition
- `SETNX` is atomic at the Redis server level
- Both workers attempt lease acquisition before execution

### Proof Sketch
1. `SETNX` is an atomic operation at the Redis server (single-threaded command execution)
2. For any two concurrent SETNX calls for the same key, exactly one returns 1 (success) and one returns 0 (failure)
3. Workers check the SETNX return value and skip execution if 0
4. Therefore: exactly one worker executes any given step

### Boundary Condition
If Redis itself is partitioned and both workers reach different Redis primaries for the same hash slot during a split-brain Redis event (extremely rare, < 1ms window during failover), both could get SETNX=1. This is accepted: the lease TTL (120s) and idempotency key provide the second line of defense. The idempotency key will prevent the second execution from producing a different result.

---

## DC-008 — Kafka Consumer Group Delivery Correctness

### Guarantee — Task Topics (Competing Consumer)
Each task message in `aeos.tasks.*` topics is delivered to exactly one worker (in steady state; at-least-once under rebalance).

### Guarantee — Event Topics (Fan-Out)
Each event message in `aeos.events.*` topics is delivered to every worker with a per-worker consumer group.

### Formal Statement (Task Topics)
```
∀ message M in task_topic, ∀ workers W1, W2 (W1 ≠ W2):
  delivered(M, W1) → ¬delivered(M, W2)    [steady state]
  [under rebalance: at-most-twice, idempotency enforced]
```

### Formal Statement (Event Topics)
```
∀ message M in event_topic, ∀ worker W:
  produced(M) → eventually(delivered(M, W))
```

### Preconditions
- Task topics: all workers use `group_id="aeos-workers"` (shared group)
- Event topics: each worker uses `group_id=f"aeos-worker-{node_id}"` (unique per worker)
- 200 partitions per task topic (> max_worker_count = 100)

### Proof Sketch (Fan-Out)
Kafka delivers each message in a topic to each consumer group independently. With N workers using N unique group IDs, Kafka maintains N independent offsets, ensuring all N workers receive every message.

### What Is NOT Guaranteed (Task Topics under Rebalance)
During a consumer group rebalance, uncommitted messages may be re-delivered. This is the "at-least-once" regime. Idempotency keys handle this case.

---

## DC-009 — Memory Causal Ordering

### Guarantee
Within a single workflow, a step cannot observe a stale value from a preceding step's output.

### Formal Statement
```
∀ workflow W, steps N, N+1:
  write_step_N_result(W) →before→ read_by_step_N+1(W)
```

### Proof Sketch
1. Step N writes result to `{wf:W}:step:N:result` in Redis (synchronous)
2. Kafka message for step N+1 is published after Redis write (Phase 2)
3. Worker consuming step N+1 reads step N's result from Redis
4. Since Kafka message is published after Redis write, and the worker reads Redis after consuming the Kafka message, the causal ordering is preserved by the happens-before relationship: Redis write → Kafka produce → Kafka consume → Redis read

---

## DC-010 — Governance Token AP Consistency

### Guarantee
Governance tokens represent a consistent snapshot of policy state at the time of task submission. Policy changes after submission do not affect in-flight tasks until token expiry or revocation.

### Formal Statement
```
∀ token T issued at time t0:
  policy_state(T) = evaluate(policies, t0)
  
∀ policy change P at time t1 > t0:
  P does not affect T until:
    T.expires_at ≤ now  OR  revocation_received(T.token_id)
```

### CAP Classification
Governance tokens are **AP** (Availability + Partition Tolerance):
- A worker in a network partition continues using its current token without re-querying the Policy Service
- This is intentional: Policy Service unavailability must not halt in-flight execution
- The trade-off: a policy change takes effect after token expiry (up to 24h), not immediately

### Revocation as CP Override
The RBAC revocation mechanism (ADR-011, DC-011) provides a CP override when immediate effect is required: publishing a revocation event via Kafka forces immediate invalidation regardless of token expiry.

---

## DC-011 — RBAC Revocation Strong Consistency

### Guarantee
A published RBAC revocation is delivered to all workers within 1 second and takes effect immediately upon delivery. No worker continues to honor a revoked permission after receiving the revocation event.

### Formal Statement
```
∀ revocation R published at t:
  ∀ worker W:
    revocation_applied(W, R) within t + 1000ms  [Kafka delivery SLA]
    
  After revocation_applied(W, R):
    ∀ permission P in R.revoked_permissions:
      W.check_permission(P) = DENIED
```

### Preconditions
- Event consumer uses per-worker group (fan-out — PROTO-015)
- Kafka delivery latency < 200ms (MSK P99)
- Worker processes revocation event before any new task evaluations

### Proof Sketch
1. Revocation event published to `aeos.events.governance`
2. Each worker has an independent consumer group → receives every event
3. Revocation event processing invalidates the local permission cache entry immediately (synchronous cache invalidation, not deferred)
4. All subsequent permission checks for the revoked entity return DENIED

---

## DC-012 — Checkpoint Recovery Liveness

### Guarantee
Every in-flight step at the time of a worker failure will eventually be recovered and re-executed (or found to have already completed).

### Formal Statement
```
∀ step S that was executing on failed worker W:
  eventually(
    completed(S) ∨ failed(S) ∨ in_dead_letter(S)
  )
```

### Proof Sketch (Orphan Scanner Coverage)
The orphan scanner covers three patterns (§16.2, PROTO-009):
1. **Pattern 1** (heartbeat stale): Detects W as FAILED → triggers scan for W's in-flight steps → Pattern 2 or 3 handles recovery
2. **Pattern 2** (expired lease): `result_key` absent, lease expired → task requeued to Kafka → re-executed (idem key absent = not completed)
3. **Pattern 3** (`next_published` absent): `result_key` present, `next_published` absent → Phase 1 completed, Phase 2 incomplete → re-publish next tasks

These three patterns are exhaustive. Every possible state a step can be in after a crash is covered by at least one pattern.

---

## DC-013 — At-Most-Once Kafka Offset Commitment Externality

### Guarantee
Committing a Kafka offset does not constitute an external side effect. Kafka offset commitment is purely internal bookkeeping. The observable externality (step result, downstream action) is produced only after the two-phase checkpoint completes.

### Statement
This guarantee means: offset commitment failures (rare, transient Kafka errors) are safe to retry. The "committed" state of a step is determined by `{wf:X}:step:N:next_published=true` in Redis, not by Kafka offset state.

### Consequence for Recovery
The orphan scanner uses Redis state (not Kafka offsets) as the ground truth for recovery. A step may be in any of: no Redis state (not started), Phase 1 only (result written, not published), Phase 2 (next_published=true). All are recoverable. Kafka offset state is derived from Redis state, not the other way around.

---

## Summary: Correctness Guarantees by Property

| Property | Guarantee Level | Approach |
|----------|----------------|----------|
| Leader election safety | Strong (CP) | Raft consensus |
| Log consistency | Strong (CP) | Raft log matching |
| Step at-least-once delivery | Guaranteed | Kafka + orphan scanner |
| Step idempotency | Strong | Redis idem key + Phase 1 atomicity |
| Checkpoint durability | Strong (single failure) | Redis Cluster + Kafka replication |
| Split-brain prevention | Strong (Redis available) | Execution lease SETNX |
| Fan-out event delivery | Guaranteed | Per-worker Kafka consumer group |
| Memory causal ordering | Strong (within workflow) | Redis synchronous write before Kafka produce |
| Governance token consistency | AP (snapshot semantics) | Token-in-envelope |
| RBAC revocation | Strong (< 1s) | Kafka fan-out + cache invalidation |
| Recovery liveness | Guaranteed | 3-pattern orphan scanner |

---

*End of Distributed Correctness Specification — `024-DISTRIBUTED_CORRECTNESS_SPEC.md`*
