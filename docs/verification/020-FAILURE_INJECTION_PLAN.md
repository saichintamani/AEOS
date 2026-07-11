# AEOS Phase 9 DRP — Failure Injection Plan

**Document:** `020-FAILURE_INJECTION_PLAN.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## Purpose

This document defines the chaos engineering experiments for AEOS Phase 9. Each experiment is a structured test of the system's failure handling, recovery protocols, and invariant preservation under adverse conditions. All experiments MUST be executable against a staging environment before production deployment.

Experiments are organized by severity: from infrastructure failures to distributed system edge cases.

---

## Experiment Framework

Every experiment follows this structure:

| Field | Description |
|-------|-------------|
| **FI-ID** | Unique identifier |
| **Category** | Failure category |
| **Prerequisite state** | System state required before injection |
| **Fault injection** | Exactly what is broken and how |
| **Expected behavior** | What MUST happen during the fault |
| **Recovery behavior** | What MUST happen after fault is healed |
| **Metrics** | Key metrics to monitor during experiment |
| **Acceptance criteria** | Pass/fail threshold |
| **Tools** | Chaos tools used |

---

## FI-001 — Redis Primary Shard Failure

**Category:** Data Store  
**INV Reference:** INV-EXEC-001, INV-EXEC-002, INV-CHKPT-002

**Prerequisite state:**
- 10-worker cluster, healthy
- 20 workflows in-flight across all priority levels
- Redis Cluster: 3 primaries + 3 replicas

**Fault injection:**
```bash
# Kill one Redis primary node (affects 1/3 of hash slots)
kubectl exec -n redis redis-cluster-0 -- kill -9 1
```

**Expected behavior during fault:**
1. Redis Cluster gossip detects primary failure within 1s
2. Replica for the affected shard is promoted to primary within 10s
3. Workers executing steps on affected shard: see brief `ConnectionError` during promotion
4. Affected steps retry checkpoint with exponential backoff (1s, 2s, 4s)
5. No tasks lost: either checkpoint succeeds on retry, or orphan scanner recovers

**Recovery behavior:**
1. All in-flight steps complete (with possible retry delay)
2. No duplicate step executions (idempotency key prevents re-execution)
3. Orphan scanner recovers any steps with expired leases

**Metrics to monitor:**
- `aeos_redis_operation_errors_total{operation=MULTI_EXEC}` — spike during promotion, returns to 0
- `aeos_orphan_scanner_recovered_steps_total` — may increment for steps mid-checkpoint
- `aeos_step_execution_duration_seconds` — P99 increases during failure, returns to normal

**Acceptance criteria:**
- Zero tasks permanently lost
- Zero duplicate step executions
- Recovery within 60 seconds of shard promotion
- P99 step latency returns to baseline within 120 seconds

**Tools:** kubectl exec, Redis Cluster sentinel simulation

---

## FI-002 — Kafka Broker Failure (One of Three)

**Category:** Message Broker  
**INV Reference:** INV-DIST-003

**Prerequisite state:**
- 3-broker Kafka cluster (MSK)
- 200-partition task topics; 15 workers consuming
- 50 workflows queued in Kafka

**Fault injection:**
```bash
# Simulate MSK broker failure via network block
kubectl exec -n kafka kafka-1 -- tc qdisc add dev eth0 root netem loss 100%
```

**Expected behavior during fault:**
1. Kafka client detects broker failure within `request.timeout.ms` (30s default)
2. Consumer group rebalances to remaining 2 brokers (~30s rebalance)
3. During rebalance: workers temporarily stop consuming (Kafka consumer paused)
4. After rebalance: workers resume consuming from their new partition assignments
5. Tasks that were assigned to the failed broker's partitions: rebalanced to other partitions

**Recovery behavior:**
1. When broker returns: Kafka rebalances again, redistributing partitions
2. No message loss: Kafka replication factor = 3, so all messages survive broker loss

**Metrics to monitor:**
- `aeos_kafka_consumer_lag{topic}` — spikes during rebalance, recovers after
- `aeos_kafka_consumer_rebalances_total` — increments
- `aeos_task_execution_rate` — drops to 0 during rebalance, recovers

**Acceptance criteria:**
- Zero tasks lost
- Consumer lag returns to pre-failure levels within 120 seconds of broker recovery
- No task executes twice due to rebalance (idempotency key)

**Tools:** tc (traffic control), Kafka consumer group describe

---

## FI-003 — Network Partition (Split Brain)

**Category:** Network  
**INV Reference:** INV-CONS-001, INV-DIST-001, INV-EXEC-004

**Prerequisite state:**
- 3 Cluster Manager nodes: CM-A, CM-B, CM-C (CM-A is leader)
- 10 workers: 5 connected to CM-A, 5 connected to CM-B+CM-C
- 20 workflows in-flight

**Fault injection:**
```bash
# Isolate CM-A: block all traffic between CM-A and CM-B/CM-C
iptables -A INPUT -s <CM-B-IP> -j DROP
iptables -A INPUT -s <CM-C-IP> -j DROP
# (applied to CM-A only)
```

**Expected behavior during fault:**

*Majority partition (CM-B + CM-C):*
1. CM-A's heartbeats stop arriving at CM-B, CM-C
2. Election timeout fires (150–300ms)
3. CM-B or CM-C wins election (new leader elected, term++)
4. New leader serves cluster membership
5. Workers on majority side continue processing tasks normally

*Minority partition (CM-A):*
1. CM-A cannot receive AppendEntries from majority
2. CM-A's heartbeats stop reaching workers on majority side
3. Workers on minority side: step back from Raft perspective; local tasks continue (existing leases)
4. Workers on minority side: new task acceptance stops (Kafka consumer still connected, but governance re-evaluation fails)

*In-flight workflows:*
1. Steps held by workers on majority side: execute normally
2. Steps held by workers on minority side: complete if lease is valid; checkpoint may fail if WTM (Redis) is on majority side

**Recovery behavior (partition healed):**
1. CM-A detects higher term from CM-B/C; reverts to FOLLOWER
2. Raft log reconciliation: CM-A's uncommitted entries (if any) are overwritten by majority log
3. Workers on minority side rejoin normally
4. No split-brain permanent state: only committed entries survive

**Acceptance criteria:**
- Majority partition continues processing within 2 seconds of partition
- No cluster with two simultaneous leaders at the same term
- All workflows resume after partition heals
- No data corruption (no committed entries from minority overwrite majority)

**Tools:** iptables, tc, Raft leader election monitoring

---

## FI-004 — Worker Crash (OOM)

**Category:** Compute  
**INV Reference:** INV-EXEC-001, INV-CHKPT-002

**Prerequisite state:**
- Worker A executing 5 concurrent steps
- All steps in various phases of the two-phase checkpoint

**Fault injection:**
```bash
# Simulate OOM kill
kubectl exec worker-042 -- kill -9 1
```

**Expected behavior:**
1. CM detects missed heartbeats after 15s (SUSPECTED)
2. After 25s total: FAILED
3. CM releases Worker A's Kafka partitions
4. Orphan scanner (next cycle): detects steps with:
   - Expired leases + status=EXECUTING → Pattern 2 recovery
   - Completed status + next_published absent → Pattern 3 recovery
5. Steps requeued to Kafka
6. Other workers execute recovered steps (idempotency key prevents re-execution of already-completed steps)

**Acceptance criteria:**
- All workflows complete (no permanent stall)
- Zero duplicate completed steps (idempotency enforced)
- Recovery within `heartbeat_failure_detection_time + orphan_scan_interval + lease_ttl`
  = 25s + 60s + 120s = 205s maximum (in worst case)
  Typical: ~90s

**Tools:** kubectl, metrics dashboards

---

## FI-005 — Cluster Manager Leader Failure

**Category:** Consensus  
**INV Reference:** INV-CONS-001, INV-CONS-002, INV-CONS-003

**Prerequisite state:**
- CM-A is leader; 5 workers joined; 10 workflows in-flight
- New worker attempting to join simultaneously

**Fault injection:**
```bash
kubectl delete pod aeos-cluster-manager-0 --force
```

**Expected behavior:**
1. CM-B or CM-C election timeout fires (150–300ms)
2. New leader elected (term + 1)
3. Pending JoinRequest: requester times out, retries (join is idempotent)
4. Existing workers: detect leader change via heartbeat failure; continue processing in-flight tasks (leases are in Redis, not CM)
5. Cluster Manager leadership recovery: < 1 second

**Recovery behavior:**
1. New leader re-reads membership from Raft log projection
2. Redis membership cache refreshed
3. All workers reconnect to new leader endpoint
4. Normal operation resumes

**Acceptance criteria:**
- New leader elected within 600ms (2× max election timeout)
- No in-flight tasks lost
- Redis membership cache consistent with Raft state within 5s of new leader election
- PodDisruptionBudget prevents simultaneous loss of 2 CM nodes

**Tools:** kubectl, Raft leader election metrics

---

## FI-006 — Policy Service Failure (Governance Circuit)

**Category:** Governance  
**INV Reference:** INV-EXEC-003, INV-EXEC-005

**Prerequisite state:**
- 5 workers running; new tasks arriving at 10/second
- Policy Service healthy

**Fault injection:**
```bash
kubectl scale deployment aeos-policy-service --replicas=0
```

**Expected behavior:**
1. API Gateway: governance token issuance fails → new tasks return HTTP 503
2. Tasks with valid, non-expired tokens: continue executing normally
3. Workers attempting token re-evaluation (tokens near expiry): Policy Service unreachable → task SUSPENDED (not failed)
4. Policy Service circuit breaker: CLOSED → OPEN after 5 consecutive failures
5. Workers: stop attempting re-evaluation during OPEN period; resume when HALF_OPEN
6. No task executes without a valid token

**Recovery behavior:**
1. Policy Service restored
2. Circuit breaker: OPEN → HALF_OPEN (after 30s reset timeout)
3. First re-evaluation attempt: succeeds → CLOSED
4. Suspended tasks resume
5. New task submission resumes

**Acceptance criteria:**
- Zero tasks execute without a valid governance token during Policy Service outage
- Tasks do not fail permanently due to Policy Service outage (they SUSPEND and resume)
- Token re-evaluation succeeds within 60s of Policy Service recovery
- `AEOS_GOVERNANCE_FAIL_OPEN=false` verified (no bypasses during outage)

**Tools:** kubectl, governance circuit breaker metrics

---

## FI-007 — Disk Corruption / WAL Failure (Raft)

**Category:** Storage  
**INV Reference:** INV-CONS-002, INV-CONS-003

**Prerequisite state:**
- 3-node Cluster Manager; CM-B is follower
- WAL file on CM-B is intact

**Fault injection:**
```bash
# Corrupt the WAL tail on CM-B
kubectl exec aeos-cluster-manager-1 -- \
  dd if=/dev/urandom of=/var/raft/wal bs=1 count=100 seek=$(stat -c%s /var/raft/wal)
```

**Expected behavior:**
1. CM-B detects WAL corruption on startup (or during recovery)
2. CM-B refuses to start with corrupted WAL
3. CM-B requests full log snapshot from leader (log catch-up protocol)
4. Leader sends snapshot up to `commitIndex`
5. CM-B applies snapshot, resumes normal follower operation
6. Raft quorum maintained (CM-A, CM-C) throughout

**Acceptance criteria:**
- CM-B recovers to consistent state from snapshot
- Quorum maintained during CM-B recovery (no cluster stall)
- No divergent log entries after recovery
- Cluster membership state after recovery = state before corruption

**Tools:** dd, Raft snapshot metrics

---

## FI-008 — Clock Skew Between Nodes

**Category:** Time  
**INV Reference:** INV-EXEC-002, INV-SEC-003

**Prerequisite state:**
- 10 workers; 5 workflows in-flight
- All clocks synchronized (NTP)

**Fault injection:**
```bash
# Introduce 5-minute clock skew on Worker A
kubectl exec worker-042 -- \
  date -s "$(date -d '+5 minutes' +'%Y-%m-%d %H:%M:%S')"
```

**Expected behavior:**
1. Governance token validation: Worker A's clock is ahead; tokens appear expired to Worker A
2. Worker A: re-evaluates tokens even for tokens with 45+ minutes remaining
3. Redis key TTLs: Worker A may see different remaining TTLs than other workers
4. Execution lease: `SETNX EX 120` uses Redis server time (not worker time) — leases are unaffected
5. Kafka consumer: offset commits use broker time — unaffected

**Recovery behavior:**
1. Clock skew removed (NTP resync)
2. Worker A rejoins normal operation
3. No permanent data corruption (Redis and Kafka use server-side time for TTLs)

**Acceptance criteria:**
- No tasks executed past their governance token expiry (clock-ahead worker correctly rejects)
- Execution leases remain valid (Redis server clock, not worker clock)
- No cryptographic errors from mismatched timestamps in JWT validation

**Tools:** date command, NTP configuration, JWT validation testing

---

## FI-009 — Spot Instance Preemption Storm

**Category:** Infrastructure  
**INV Reference:** INV-OPS-001

**Prerequisite state:**
- 7 workers: 3 on-demand + 4 Spot
- 20 workflows in-flight

**Fault injection:**
```bash
# Simulate AWS Spot interruption notice on all 4 Spot workers
# (2-minute preemption warning)
for worker in spot-worker-{1,2,3,4}; do
  kubectl exec $worker -- \
    curl -X PUT http://169.254.169.254/latest/meta-data/spot/instance-action \
    -d '{"action":"terminate","time":"<2min_from_now>"}'
done
```

**Expected behavior:**
1. Each Spot worker receives preemption notice (SIGTERM from preemption handler)
2. Each Spot worker: stop consuming new tasks (consumer.pause())
3. Each Spot worker: checkpoint in-flight steps (max 90s to complete, within 2-minute window)
4. Each Spot worker: send LeaveRequest to Cluster Manager
5. CM: marks 4 workers as LEFT; Kafka partitions rebalanced to 3 on-demand workers
6. KEDA: detects consumer lag increase; triggers scale-out on new Spot capacity

**Recovery behavior:**
1. New Spot instances launch (varies: 2–10 minutes for new capacity)
2. New workers join cluster and begin processing backlog

**Acceptance criteria:**
- 3 on-demand workers remain running (INV-OPS-001 satisfied)
- Zero tasks lost (in-flight steps checkpointed before eviction)
- Workflows resume on on-demand workers within 30s
- KEDA triggers scale-out within 60s of lag increase
- Recovery to 7 workers within 15 minutes of new Spot capacity becoming available

**Tools:** EC2 metadata mock, kubectl, KEDA metrics

---

## FI-010 — Kafka Consumer Group Rebalance Storm

**Category:** Message Broker  
**INV Reference:** INV-DIST-001, INV-EXEC-001

**Prerequisite state:**
- 10 workers; all actively consuming
- 100 tasks in various stages

**Fault injection:**
```bash
# Force rapid consumer group rebalances by repeatedly adding/removing a worker
for i in {1..10}; do
  kubectl scale deployment aeos-worker --replicas=11
  sleep 5
  kubectl scale deployment aeos-worker --replicas=10
  sleep 5
done
```

**Expected behavior:**
1. Each rebalance: some consumers temporarily stop consuming (rebalance protocol)
2. Tasks pulled but not yet committed: redelivered after rebalance
3. Idempotency key: prevents re-execution of already-completed steps
4. Execution lease: prevents concurrent execution of redelivered tasks

**Acceptance criteria:**
- Zero duplicate step executions (idempotency enforced)
- All tasks eventually complete or reach DLQ
- No workflow permanently stalls due to rebalance storm

**Tools:** kubectl, consumer group describe, idempotency verification

---

## FI-011 — Policy Service Slow Response (Timeout Test)

**Category:** Governance  
**INV Reference:** INV-EXEC-005

**Prerequisite state:**
- Policy Service running
- New tasks being submitted at 5/second

**Fault injection:**
```bash
# Introduce 6-second latency to Policy Service responses
kubectl exec aeos-policy-service -- \
  tc qdisc add dev eth0 root netem delay 6000ms
```

**Expected behavior:**
1. Governance evaluation timeout (5s) triggers before Policy Service responds
2. Every governance evaluation returns REJECTED (reason=policy_evaluation_timeout)
3. API Gateway returns HTTP 503 for all new task submissions
4. In-flight tasks: unaffected (tokens already issued)
5. Circuit breaker: opens after 5 consecutive 503s

**Acceptance criteria:**
- ZERO tasks approved during Policy Service timeout (fail-closed verified)
- HTTP 503 returned within 5.1 seconds of submission (not after 6s)
- Circuit breaker opens within 30 seconds
- `AEOS_GOVERNANCE_FAIL_OPEN` confirmed = false throughout

**Tools:** tc, governance evaluation logs, HTTP response codes

---

## FI-012 — Memory Pressure (OOM Prevention)

**Category:** Compute  
**INV Reference:** INV-OPS-001

**Prerequisite state:**
- 5 workers; each with resource limits: 4Gi memory
- Backpressure limit: `max_in_flight=10` per worker

**Fault injection:**
```bash
# Submit 1000 tasks simultaneously (100× normal rate)
# Verify workers don't exceed max_in_flight
ab -n 1000 -c 100 http://api-gateway/api/v1/tasks
```

**Expected behavior:**
1. Kafka lag grows rapidly
2. KEDA triggers scale-out (workers: 5 → up to 20 within 2 minutes)
3. Each worker: when `in_flight >= 10`, pauses Kafka consumer
4. Consumer lag continues growing (bounded by topic retention, not memory)
5. Memory usage per worker: stays within limits (bounded by `max_in_flight`)
6. No OOM kills

**Acceptance criteria:**
- No OOM kills on any worker pod
- `in_flight_tasks` per worker never exceeds `max_in_flight + 1` (one over during transition)
- Consumer pauses when `in_flight >= max_in_flight`; resumes when `in_flight <= max_in_flight / 2`
- All 1000 tasks eventually complete or fail

**Tools:** Prometheus metrics, k8s events (OOM), load generator

---

## Experiment Execution Guidelines

### EEG-001 — Environment Requirements
All failure injection experiments MUST be executed in a dedicated staging environment that mirrors production:
- Same Kubernetes version
- Same instance types (including Spot configuration)
- Same Kafka partition count (200)
- Same Redis Cluster topology (3p + 3r)
- Production-equivalent data volume

### EEG-002 — Baseline Capture
Before each experiment, capture baseline metrics for:
- Task completion rate (tasks/second)
- P50/P95/P99 step execution latency
- Consumer lag
- Worker CPU/memory utilization

### EEG-003 — Experiment Authorization
Each experiment category requires authorization from:

| Category | Authorized by |
|----------|--------------|
| Infrastructure (FI-001, FI-002) | On-call SRE |
| Consensus (FI-003, FI-005, FI-007) | Platform Lead + SRE |
| Security (FI-008, FI-011) | Security Lead + Platform Lead |
| Full-cluster (FI-009, FI-012) | VP Engineering + Platform Lead |

### EEG-004 — Abort Criteria
Abort the experiment immediately if:
- Any governance bypass is detected (invariant INV-EXEC-005 violated)
- Data loss exceeds 0 tasks (invariant INV-DIST-003 violated)
- Production spillover (experiment should be isolated to staging)

### EEG-005 — Experiment Cadence
Recommended execution schedule:
- **Pre-release**: FI-001, FI-002, FI-004, FI-006, FI-011 (every release)
- **Monthly**: FI-003, FI-005, FI-007, FI-008 (consensus and security)
- **Quarterly**: FI-009, FI-010, FI-012 (operational and load)

---

*End of Failure Injection Plan — `020-FAILURE_INJECTION_PLAN.md`*
