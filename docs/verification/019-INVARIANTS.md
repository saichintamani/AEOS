# AEOS Phase 9 DRP — System Invariants

**Document:** `019-INVARIANTS.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## Purpose

An invariant is a condition that MUST hold true at all times during system operation, regardless of concurrency, node failures, network partitions, or load. Violation of an invariant is a system correctness bug, not an operational issue.

Invariants are organized by subsystem. Each invariant has:
- **INV-ID**: Unique identifier, referenced by conformance tests
- **Statement**: Precise English description
- **Formal statement**: Semi-formal logical expression
- **Failure consequence**: What happens if the invariant is violated
- **Validation method**: How to detect violation

---

## Invariant Catalogue

### I. Execution Invariants

#### INV-EXEC-001 — No Duplicate Step Execution (without retry policy)

**Statement:** A task step MUST NOT be executed more than once unless the governing retry policy explicitly permits re-execution.

**Formal statement:**
```
∀ workflow W, step S:
  execute_count(W, S) > 1 →
    retry_policy(W).max_retries > 0 ∧
    retry_policy(W).permits_retry(last_error(W, S))
```

**Failure consequence:** Duplicate LLM calls (cost doubling), duplicate external API calls (side effects), duplicate writes to databases (data corruption or duplication).

**Validation method:**
1. Idempotency key check: verify that `{wf:W}:step:S:idem` exists before execution; if it does, return cached result
2. Execution lease check: verify that `SETNX` returned 1 before execution; if it returned 0, skip execution
3. Metrics: `aeos_step_executions_total{step_id, execution_number}` — alert on `execution_number > 1` for non-retry scenarios

---

#### INV-EXEC-002 — Checkpoint Precedes Kafka Offset Commit

**Statement:** The Kafka offset for a task MUST NOT be committed until the two-phase checkpoint for that task is complete (Phase 2: `next_published=true`).

**Formal statement:**
```
∀ task T with Kafka offset O:
  kafka_offset_committed(T, O) →
    phase1_checkpoint_complete(T) ∧
    phase2_checkpoint_complete(T) ∧
    next_published(T) = true
```

**Failure consequence:** If the worker crashes after offset commit but before checkpoint, the task is permanently lost (Kafka will not redeliver it; Redis has no record).

**Validation method:**
1. Code review: verify that `consumer.commit_offsets()` is called only at the end of the scheduler loop (after `next_published` is set)
2. Chaos test: crash worker after Phase 1 commit, before Phase 2 — verify task is recovered by orphan scanner
3. Metrics: track checkpoint completion vs offset commit; alert on divergence

---

#### INV-EXEC-003 — Governance Token Required for Execution

**Statement:** No task step MUST execute without a valid, non-expired, non-revoked governance token that explicitly authorizes the step's task type.

**Formal statement:**
```
∀ step execution E:
  E.executed →
    ∃ token T such that:
      T.valid_signature ∧
      T.expires_at > E.started_at ∧
      ¬T.revoked ∧
      T.allows(E.task_type)
```

**Failure consequence:** Unauthorized task execution; compliance violation; potential safety/security impact.

**Validation method:**
1. Code review: verify token validation is called before execution and throws if any check fails
2. Unit test: inject expired token; verify execution rejected
3. Unit test: inject revoked token; verify execution rejected
4. Integration test: disable Policy Service; verify tasks queue (not bypass)

---

#### INV-EXEC-004 — Execution Lease Acquired Before Execution

**Statement:** A worker MUST hold the execution lease for a step before executing that step.

**Formal statement:**
```
∀ step S executing on worker W:
  is_executing(W, S) →
    holds_lease(W, S) = true ∧
    redis_get({wf:S.wf_id}:step:S.id:lease) = W.node_id
```

**Failure consequence:** Multiple workers may execute the same step simultaneously (split-brain double execution), causing duplicate side effects.

**Validation method:**
1. Code review: verify SETNX is called before execution; verify it checks return value 1
2. Test: two workers simultaneously attempt the same step; verify exactly one executes

---

#### INV-EXEC-005 — Fail-Closed Governance Default

**Statement:** When governance evaluation produces no matching policy, the result MUST be REJECTED. When evaluation times out, the result MUST be REJECTED. APPROVED MUST NOT be the default for any unhandled case.

**Formal statement:**
```
evaluate(task_type) = APPROVED →
  ∃ policy P: matches(P, task_type) ∧ P.decision = APPROVE
  ∧ evaluation_elapsed < timeout

evaluate(task_type) = REJECTED when:
  (no_policy_matches) ∨ (evaluation_timed_out)
```

**Failure consequence:** Tasks with no governing policy execute without authorization, bypassing governance entirely.

**Validation method:**
1. Unit test: submit task type with no matching policy; verify REJECTED
2. Unit test: simulate 6-second evaluation; verify REJECTED after 5s timeout
3. Integration test: take Policy Service offline; verify tasks queue in PENDING, not execute

---

### II. Consensus and Membership Invariants

#### INV-CONS-001 — Single Leader per Term

**Statement:** At most one Raft node MUST be the leader for any given term.

**Formal statement:**
```
∀ term T: |{node N : N.state = LEADER ∧ N.currentTerm = T}| ≤ 1
```

**Failure consequence:** Two leaders accepting conflicting membership changes; split-brain cluster membership; inconsistent routing decisions.

**Validation method:**
1. Log monitoring: alert if two nodes emit leader heartbeats for the same term
2. Raft implementation review: verify vote-granting only once per term (fsynced `votedFor`)
3. Chaos test: network partition isolating all three CM nodes; verify at most one partition establishes a leader

---

#### INV-CONS-002 — Raft State Machine Monotonicity

**Statement:** Raft log entries MUST be applied in strict monotonically increasing index order. An entry at index N MUST NOT be applied until all entries at indices < N are applied.

**Formal statement:**
```
∀ log entry E at index N:
  applied(E) → ∀ E' at index N' < N: applied(E')
```

**Failure consequence:** Out-of-order state machine application produces inconsistent membership state (e.g., a JOIN applied before a prerequisite CONFIGURE causes undefined worker state).

**Validation method:**
1. Raft implementation test: verify apply loop uses monotonically increasing lastApplied
2. Log inspection: verify `commit_index ≥ apply_index` always

---

#### INV-CONS-003 — Leader Persistence Before Response

**Statement:** A Raft leader MUST persist `currentTerm` and `votedFor` to durable storage (with fsync) before responding to any RequestVote or AppendEntries RPC.

**Formal statement:**
```
∀ RPC response R sent by leader L:
  fsynced_to_wal(L.currentTerm) ∧ fsynced_to_wal(L.votedFor) 
  → before → R.sent
```

**Failure consequence:** A node may vote twice in the same term after a crash-restart cycle, violating the single-leader invariant (INV-CONS-001).

**Validation method:**
1. Code review: verify `persist()` is called before every RPC return in vote handler
2. Crash test: crash node mid-vote; restart; verify it does not grant a second vote for the same term

---

#### INV-CONS-004 — Membership Table is Raft Log Projection

**Statement:** The cluster membership visible to external callers MUST be the projection of the committed Raft log. Redis MUST NOT be the source of truth for membership.

**Formal statement:**
```
membership_table = project(raft_log, committed_index)
redis_membership_cache = read_through_cache(membership_table)
staleness(redis_membership_cache) ≤ 5_seconds
```

**Failure consequence:** Stale Redis membership causes routing decisions based on outdated worker state (routing to failed workers, not routing to newly joined workers).

**Validation method:**
1. Integration test: kill a worker; verify Redis membership cache reflects FAILED within 5 seconds
2. Integration test: join a new worker; verify Redis reflects RUNNING within 5 seconds

---

### III. Checkpoint and Recovery Invariants

#### INV-CHKPT-001 — Phase 1 Atomicity

**Statement:** The Phase 1 checkpoint write (result, status, idempotency key) MUST be atomic. Either all three keys are written or none are.

**Formal statement:**
```
∀ checkpoint C:
  ∃ key_result(C) ↔ ∃ key_status(C) ↔ ∃ key_idem(C)
```

**Failure consequence:** Partial checkpoint leaves the step in an ambiguous state — result exists but no idempotency key means re-execution would occur despite a completed step.

**Validation method:**
1. Code review: verify all three keys are in a single MULTI/EXEC block
2. Redis fault injection: force EXEC to fail; verify none of the three keys exist

---

#### INV-CHKPT-002 — Orphan Recovery Completeness

**Statement:** Every in-flight step belonging to a crashed worker MUST be recovered by the orphan scanner within one scan cycle (60 seconds) after the lease expires.

**Formal statement:**
```
∀ step S with worker W crashed:
  lease_expired(S) →
  recovered(S) within scan_interval + lease_ttl
```

**Failure consequence:** Workflow permanently stalls; task is neither completed nor retried.

**Validation method:**
1. Chaos test: crash a worker mid-execution; verify orphan scanner requeues the step within `scan_interval + lease_ttl + 10s`
2. Metrics: `aeos_orphan_scanner_recovered_steps_total` — must be > 0 after worker crash test

---

### IV. Memory Invariants

#### INV-MEM-001 — Working Memory Key Co-location

**Statement:** All Redis keys participating in a single MULTI/EXEC transaction MUST share the same workflow hashtag (`{wf:<workflow_id>}`).

**Formal statement:**
```
∀ MULTI/EXEC block B containing keys K1, K2, ..., Kn:
  hashtag(K1) = hashtag(K2) = ... = hashtag(Kn)
where hashtag({wf:X}:*) = {wf:X}
```

**Failure consequence:** `CROSSSLOT Keys in request don't hash to the same slot` Redis error; transaction fails; checkpoint fails; task may be lost.

**Validation method:**
1. Static analysis: lint all Redis calls; verify every MULTI/EXEC only contains keys with the same `{wf:X}` prefix
2. Integration test: attempt cross-workflow transaction; verify CROSSSLOT error is thrown

---

#### INV-MEM-002 — LTM Vector Consistency

**Statement:** After a Weaviate write completes with QUORUM consistency, the same object MUST be readable from at least one replica within 500ms.

**Formal statement:**
```
∀ write W to Weaviate with consistency=QUORUM:
  ∃ replica R: readable(W, R) within 500ms of W.ack_time
```

**Failure consequence:** Same-workflow read-after-write returns empty (episode not found); incorrect workflow behavior.

**Validation method:**
1. Load test: write and immediately read 1000 episodic records; measure read availability at t+0, t+100ms, t+500ms
2. Alert: `aeos_episodic_memory_read_after_write_miss_rate` > 1% triggers investigation

---

### V. Security Invariants

#### INV-SEC-001 — No Plaintext Internal Traffic

**Statement:** All service-to-service communication MUST use TLS. No plaintext connection between AEOS services is permitted.

**Formal statement:**
```
∀ connection C between AEOS services S1 and S2:
  C.protocol ∈ {TLS 1.2, TLS 1.3, mTLS}
```

**Failure consequence:** Credentials, tokens, and task payloads are transmitted in plaintext; subject to network-level interception.

**Validation method:**
1. Network scan: periodic `nmap --script ssl-enum-ciphers` against all service endpoints
2. NetworkPolicy audit: verify no service exposes plaintext port to other services
3. Admission controller: reject pods with insecure service configurations

---

#### INV-SEC-002 — RBAC Revocation Propagation

**Statement:** An RBAC revocation MUST propagate to all workers within 1 second of being published to the governance event topic.

**Formal statement:**
```
∀ revocation event E published at t:
  ∀ worker W:
  W.revocation_cache[E.entity_id] = REVOKED
  within t + 1000ms
```

**Failure consequence:** A revoked credential continues to be accepted by workers, allowing unauthorized task execution for up to 1 second.

**Validation method:**
1. Integration test: publish revocation event; poll 10 workers for cache update; verify all updated within 1s
2. Metrics: `aeos_revocation_propagation_duration_ms` Histogram; P99 must be < 1000ms

---

#### INV-SEC-003 — Certificate Validity Window

**Statement:** No AEOS service MUST use a TLS certificate with remaining validity < 1 hour for new connections.

**Formal statement:**
```
∀ TLS connection establishment at time T:
  certificate.expiry > T + 3600
```

**Failure consequence:** TLS handshake failures due to expired certificate; service becomes unreachable.

**Validation method:**
1. Certificate monitoring: alert when any leaf cert has remaining validity < 2 hours
2. Vault Agent test: simulate renewal failure; verify service uses old cert until new cert available

---

### VI. Distributed Correctness Invariants

#### INV-DIST-001 — Kafka Consumer Group Correctness

**Statement:** Task topics MUST use a shared consumer group (`aeos-workers`). Event/broadcast topics MUST use per-worker consumer groups (`aeos-worker-{node_id}`).

**Formal statement:**
```
∀ topic T in task_topics:
  consumer_group(T) = "aeos-workers"

∀ topic T in event_topics:
  consumer_group(T) = "aeos-worker-" + node_id
  ∀ workers W1, W2: consumer_group(T, W1) ≠ consumer_group(T, W2)
```

**Failure consequence (if violated for event topics):** Governance events, cluster membership changes, and revocation events are delivered to at most 1 of N workers instead of all workers. N-1 workers operate with stale state.

**Validation method:**
1. Static analysis: lint all AIOKafkaConsumer instantiations; verify group_id assignment matches topic type
2. Integration test: publish governance event; verify all workers receive it within 1 second

---

#### INV-DIST-002 — Partition Count vs Cluster Size

**Statement:** The number of Kafka task topic partitions MUST be ≥ the maximum expected worker count.

**Formal statement:**
```
kafka_task_topic_partitions ≥ max_worker_count
Currently: 200 ≥ 100  ✓
```

**Failure consequence:** Workers beyond the partition count receive no task messages; they exist but are idle.

**Validation method:**
1. Deployment check: verify partition count = 200 at cluster initialization
2. Alert: if `worker_count > kafka_task_topic_partitions - 10` (approaching limit)

---

#### INV-DIST-003 — At-Least-Once Delivery Guarantee

**Statement:** Every accepted task MUST be delivered to a worker for execution at least once, or placed in the dead-letter queue.

**Formal statement:**
```
∀ task T with status=QUEUED:
  eventually(
    (executed(T) ∧ status(T) ∈ {COMPLETED, FAILED}) ∨
    in_dead_letter_queue(T)
  )
```

**Failure consequence:** Tasks silently dropped; workflows stall without user notification.

**Validation method:**
1. End-to-end test: inject 1000 tasks; crash workers randomly; verify 0 tasks lost (all eventually reach COMPLETED, FAILED, or DLQ)
2. Chaos test: Kafka broker failure; verify tasks buffered and redelivered on recovery

---

#### INV-DIST-004 — Causal Ordering of Memory Writes

**Statement:** For a single workflow, memory writes MUST be applied in causal order. A write from step N MUST be visible before the write from step N+1 is applied.

**Formal statement:**
```
∀ workflow W, steps N < N+1:
  write(W, step_N) →before→ write(W, step_N+1)
```

**Failure consequence:** Step N+1 reads stale state from step N, producing incorrect results.

**Validation method:**
1. Sequential execution test: verify step N's output is readable before step N+1 begins
2. WTM: synchronous Redis write ensures causal ordering within a workflow (same hashtag slot)

---

#### INV-DIST-005 — Raft Quorum for Cluster Changes

**Statement:** No cluster membership change (JOIN, LEAVE, FAIL) MUST be committed unless a quorum of Cluster Manager nodes acknowledges it.

**Formal statement:**
```
∀ membership change M:
  committed(M) → |{node N : acknowledged(N, M)}| ≥ quorum_size
where quorum_size = ceil((cm_nodes + 1) / 2) = 2 (for 3-node cluster)
```

**Failure consequence:** Membership changes committed in a minority partition diverge from majority; after partition heals, conflicting membership states exist.

**Validation method:**
1. Network partition test: isolate one CM node; attempt worker join; verify join fails (quorum unavailable)
2. Verify no worker appears as RUNNING in majority partition that only joined through minority

---

### VII. Operational Invariants

#### INV-OPS-001 — On-Demand Worker Floor

**Statement:** At all times, at least 3 worker pods MUST run on on-demand (non-preemptible) infrastructure.

**Formal statement:**
```
∀ time T:
  |{worker W : W.instance_type = on_demand ∧ W.state = RUNNING}| ≥ 3
```

**Failure consequence:** Full worker eviction during Spot capacity event; complete task queue drain; cold-start delay.

**Validation method:**
1. AWS: monitor on-demand worker count; alert if < 3
2. KEDA: verify `minReplicaCount: 3` with on-demand node group `minSize: 3`

---

#### INV-OPS-002 — Raft Quorum Maintained During Maintenance

**Statement:** At least 2 of 3 Cluster Manager nodes MUST be available at all times. A maintenance operation that would reduce availability below quorum MUST be blocked.

**Formal statement:**
```
∀ time T:
  |{cm_node N : N.state = RUNNING}| ≥ 2
```

**Failure consequence:** Loss of Raft quorum; cluster stops accepting new workflows; in-flight workflows may stall.

**Validation method:**
1. PodDisruptionBudget: `minAvailable: 2` for Cluster Manager
2. Alert: if only 2 CM nodes running, block any maintenance operations on CM pods

---

*End of System Invariants — `019-INVARIANTS.md`*
