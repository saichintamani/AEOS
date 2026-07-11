# AEOS Phase 9 DRP — Protocol Specification

**Document:** `016-PROTOCOL_SPECIFICATION.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06  
**Canonical source:** `012-PHASE_9_DRP_SPECIFICATION_v1_1.md`

---

## Purpose

This document provides the formal specification for every protocol used in AEOS Phase 9. Each protocol entry defines: participants, preconditions, message sequence, timeouts, retry rules, failure cases, recovery procedures, and idempotency guarantees.

Protocol IDs are referenced by conformance tests in `021-CONFORMANCE_TEST_PLAN.md`.

---

## Protocol Index

| ID | Name | Category |
|----|------|---------|
| [PROTO-001](#proto-001) | Node Join | Cluster Management |
| [PROTO-002](#proto-002) | Node Leave (Graceful) | Cluster Management |
| [PROTO-003](#proto-003) | Node Failure Detection | Cluster Management |
| [PROTO-004](#proto-004) | Leader Election (Raft) | Consensus |
| [PROTO-005](#proto-005) | Heartbeat | Cluster Management |
| [PROTO-006](#proto-006) | Task Dispatch | Execution |
| [PROTO-007](#proto-007) | Task Retry | Execution |
| [PROTO-008](#proto-008) | Two-Phase Checkpoint | Execution |
| [PROTO-009](#proto-009) | Checkpoint Recovery | Execution |
| [PROTO-010](#proto-010) | Worker Registration | Capability |
| [PROTO-011](#proto-011) | Capability Advertisement | Capability |
| [PROTO-012](#proto-012) | Capability Discovery | Capability |
| [PROTO-013](#proto-013) | Governance Token Issuance | Governance |
| [PROTO-014](#proto-014) | Governance Token Refresh | Governance |
| [PROTO-015](#proto-015) | RBAC Revocation | Security |
| [PROTO-016](#proto-016) | Memory Synchronization | Memory |
| [PROTO-017](#proto-017) | LTM Replication | Memory |
| [PROTO-018](#proto-018) | Security Token (mTLS) Rotation | Security |
| [PROTO-019](#proto-019) | Execution Lease Acquisition | Execution |
| [PROTO-020](#proto-020) | Cluster Drain (Rolling Deploy) | Operations |

---

## PROTO-001

### Node Join Protocol

**Category:** Cluster Management  
**RFC Reference:** §6.2.3  
**ADR Reference:** ADR-007

#### Participants
- `W`: Worker node requesting to join
- `CM-L`: Cluster Manager Raft leader
- `CM-F1, CM-F2`: Cluster Manager followers
- `CR`: Capability Registry
- `KA`: Kafka Admin (Cluster Manager calls on W's behalf)

#### Preconditions
1. Worker has completed kernel phases INITIALIZING through STARTING
2. Worker has a valid Vault-issued leaf certificate
3. Cluster Manager leader is elected and has quorum

#### Protocol Steps

```
W                    CM-L               CM-F1   CM-F2   CR      KA
|                     |                  |       |       |       |
|--JoinRequest------->|                  |       |       |       |
|  (node_id, caps,    |                  |       |       |       |
|   cert, region, AZ) |                  |       |       |       |
|                     |--AppendEntries-->|       |       |       |
|                     |  (JOIN log entry)|       |       |       |
|                     |                  |--ACK->|       |       |
|                     |<--AppendEntries--|       |       |       |
|                     |  (quorum = 2/3) |       |       |       |
|                     |--RegisterCaps-->|       |       |       CR
|                     |  (capabilities) |       |       |       |
|                     |                  |       |       |<--OK--|
|                     |--AssignPartitions>|      |       |       |
|                     |  (Kafka)         |       |       |      KA
|                     |                  |       |       |<--OK--|
|--JoinResponse<------|                  |       |       |       |
|  (node_id, partitions,                 |       |       |       |
|   peer_list, term)  |                  |       |       |       |
|                     |                  |       |       |       |
| [kernel: JOINING → RUNNING]            |       |       |       |
```

#### Step Sequence (Detailed)

1. **W → CM-L: JoinRequest**
   - Fields: `node_id`, `capabilities[]`, `mTLS_cert`, `region`, `az`, `max_concurrent_tasks`, `version`
   - Transport: gRPC with mTLS

2. **CM-L: Validate request**
   - Verify certificate chain against Intermediate CA
   - Verify `node_id` not already in membership (duplicate join)
   - Verify cluster has not reached `MAX_CLUSTER_SIZE`

3. **CM-L → Raft: AppendEntries(JOIN)**
   - Log entry: `{type: JOIN, node_id, capabilities, timestamp_ns}`
   - Wait for quorum acknowledgment (2 of 3 CM nodes)
   - Fsync to WAL before responding to any RPC (ADR-007)

4. **CM-L → CR: RegisterCapabilities**
   - Capabilities registered BEFORE partition assignment
   - This ordering ensures the worker is discoverable before receiving tasks

5. **CM-L → KA: AssignPartitions**
   - Assign Kafka task topic partitions to new worker
   - Kafka consumer group rebalance triggered

6. **CM-L → W: JoinResponse**
   - Fields: `node_id`, `assigned_partitions[]`, `peer_list[]`, `current_term`, `leader_id`

7. **W: Transition kernel JOINING → RUNNING**

#### Timeouts
| Step | Timeout | On Timeout |
|------|---------|-----------|
| Raft AppendEntries quorum | 5 seconds | Retry × 3, then return `CLUSTER_UNAVAILABLE` |
| CR RegisterCapabilities | 3 seconds | Retry × 3, then fail join |
| KA AssignPartitions | 10 seconds | Retry × 3, then fail join |

#### Failure Cases

| Failure | Detection | Recovery |
|---------|-----------|---------|
| CM-L crashes after Raft commit but before JoinResponse | W: gRPC timeout | W retries JoinRequest; idempotent: duplicate join is detected and treated as success |
| CR unavailable | RegisterCapabilities timeout | Join fails; W retries full join after 10s backoff |
| Kafka partition assignment fails | AssignPartitions timeout | Join fails; Cluster Manager rolls back Raft log entry via `REMOVE` entry |
| Network partition during join | Raft quorum fails | W receives `CLUSTER_UNAVAILABLE`; retries with exponential backoff |

#### Idempotency
JoinRequest is idempotent: if the Raft log already contains a JOIN entry for `node_id`, the Cluster Manager returns the existing JoinResponse without re-committing to the log.

---

## PROTO-002

### Node Leave Protocol (Graceful)

**Category:** Cluster Management  
**RFC Reference:** §6.2.3

#### Participants
- `W`: Worker initiating graceful leave
- `CM-L`: Cluster Manager Raft leader
- `CR`: Capability Registry

#### Protocol Steps

```
W                    CM-L               CR      Kafka
|                     |                  |       |
| [Stop consuming new tasks]             |       |
| [Complete in-flight steps]             |       |
|                     |                  |       |
|--DrainRequest------>|                  |       |
|  (node_id, reason)  |                  |       |
|                     |--AppendEntries-->|       |
|                     |  (DRAINING entry)|       |
|<--DrainAck----------|                  |       |
|                     |--DeregisterCaps->|       |
|                     |                  |<--OK--|
|                     |--RebalanceParts->|       Kafka
|                     |  (release parts) |       |
|                     |                  |       |<--rebalance done
|--LeaveRequest------>|                  |       |
|                     |--AppendEntries-->|       |
|                     |  (LEFT entry)    |       |
|<--LeaveResponse-----|                  |       |
| [kernel: RUNNING → STOPPED]            |       |
```

#### Timeouts
- Drain completion timeout: 120 seconds (all in-flight steps must complete)
- After 120s: force checkpoint remaining in-flight steps; proceed with leave

#### Idempotency
LeaveRequest is idempotent. If node_id is already in LEFT state, return success.

---

## PROTO-003

### Node Failure Detection Protocol

**Category:** Cluster Management  
**RFC Reference:** §6.2.2

#### Participants
- `CM-L`: Cluster Manager leader
- `W-X`: Worker under observation

#### State Transitions
```
RUNNING → SUSPECTED (3 consecutive missed heartbeats)
SUSPECTED → FAILED (5 total missed heartbeats OR 2 consecutive missed while SUSPECTED)
SUSPECTED → RUNNING (heartbeat received while SUSPECTED)
```

#### Protocol

```
CM-L monitors heartbeat timer for W-X:

t=0:   Expected heartbeat — not received
       missed_count = 1

t=5s:  Expected heartbeat — not received
       missed_count = 2

t=10s: Expected heartbeat — not received
       missed_count = 3
       → Transition W-X: RUNNING → SUSPECTED
       → Exclude W-X from new task routing
       → Begin active probing: send direct Probe(node_id) via gRPC

t=15s: Probe timeout — not received
       missed_count = 4
       → Log: "W-X suspected failure, probe timeout"

t=20s: Expected heartbeat — not received
       missed_count = 5
       → Transition W-X: SUSPECTED → FAILED
       → Commit FAILED entry to Raft log
       → Trigger orphan scan for W-X's in-flight workflows
       → Release W-X's Kafka partitions (trigger rebalance)
       → Deregister W-X's capabilities from CR
```

#### Failure Cases

| Failure | Handling |
|---------|---------|
| False positive (network hiccup) | SUSPECTED state provides grace period; heartbeat receipt reverts to RUNNING |
| CM-L crashes during failure detection | New CM-L reads Raft log; if last entry for W-X is SUSPECTED with no subsequent RUNNING, re-initiate failure detection from SUSPECTED state |
| W-X network partition (not crashed) | W-X detects it cannot reach CM-L; W-X stops accepting new tasks; W-X's leases expire; orphan scanner handles in-flight steps |

---

## PROTO-004

### Leader Election Protocol (Raft)

**Category:** Consensus  
**RFC Reference:** §6.2.1  
**ADR Reference:** ADR-007

#### Participants
- `CM-A, CM-B, CM-C`: Three Cluster Manager nodes

#### Raft Election Protocol

```
[CM-A is current leader]
[CM-A crashes]

CM-B (election timeout fires):
  1. Increment currentTerm (persist to WAL, fsync)
  2. Set votedFor = self (persist to WAL, fsync)
  3. Transition to CANDIDATE state
  4. Send RequestVote(term, candidateId, lastLogIndex, lastLogTerm) to CM-A, CM-C

CM-C receives RequestVote:
  Safety checks:
    a. req.term > currentTerm? → update currentTerm (persist, fsync)
    b. votedFor is null or req.candidateId? → can grant vote
    c. req.lastLogTerm > localLastLogTerm OR
       (req.lastLogTerm == localLastLogTerm AND req.lastLogIndex >= localLastLogIndex)?
       → log is at least as up-to-date: can grant vote
  
  If all checks pass:
    set votedFor = req.candidateId (persist, fsync)
    return VoteGranted(term=currentTerm, granted=true)

CM-B receives quorum (2 of 3 votes):
  1. Transition to LEADER
  2. Send empty AppendEntries (heartbeat) immediately to assert leadership
  3. Begin heartbeat timer (100ms interval)
  4. Update Redis membership cache: leader = CM-B

[Election complete: CM-B is new leader]
```

#### Timing Parameters
| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Election timeout | 150–300ms (randomized) | Randomization prevents split vote |
| Heartbeat interval | 100ms | Must be << election timeout |
| Max election duration | 2 × max_election_timeout = 600ms | After this: restart election |

#### Split Vote Recovery
If no candidate receives quorum within the election timeout, all candidates increment their term, restart election with a new randomized timeout.

#### Persistent State (must fsync before responding to any RPC)
- `currentTerm`: monotonically increasing term number
- `votedFor`: candidateId voted for in current term (null if not voted)
- `log[]`: log entries (each entry: term, index, command)

---

## PROTO-005

### Heartbeat Protocol

**Category:** Cluster Management  
**RFC Reference:** §6.2.2

#### Workers → Cluster Manager

```
[Every 5 seconds, each worker sends:]

W → CM-L: Heartbeat {
  node_id: "worker-042",
  term: 7,
  timestamp_ns: 1751792400000000000,
  in_flight_tasks: 3,
  cpu_utilization: 0.42,
  memory_utilization: 0.31,
  kafka_consumer_lag: 12
}

CM-L → W: HeartbeatAck {
  term: 7,
  leader_id: "cluster-manager-1",
  cluster_version: 42   ← increments on membership change; W detects config change
}
```

If `cluster_version` in HeartbeatAck is higher than the worker's local version, the worker fetches updated membership from the Cluster Manager.

#### Cluster Manager → Workers (Leader Heartbeat)
The Raft leader sends empty `AppendEntries` (heartbeat) to all followers every 100ms to prevent election timeouts.

---

## PROTO-006

### Task Dispatch Protocol

**Category:** Execution  
**RFC Reference:** §7.2

#### Flow

```
API Gateway              Kafka                 Worker               Redis
     |                     |                     |                    |
     |--Submit(task)------->|                     |                    |
     |  topic: aeos.tasks.{priority}              |                    |
     |<--task_id, token-----|                     |                    |
     |                      |--Poll()------------>|                    |
     |                      |<--TaskMessage-------|                    |
     |                      |                     |--AcquireLease----->|
     |                      |                     |  SETNX lease EX120 |
     |                      |                     |<--1 (acquired)-----|
     |                      |                     |--ValidateToken      |
     |                      |                     |  (local check)      |
     |                      |                     |--CheckIdemKey----->|
     |                      |                     |<--nil (not seen)---|
     |                      |                     |--Execute step       |
     |                      |                     |--Phase1Checkpoint->|
     |                      |                     |  MULTI/EXEC        |
     |                      |                     |<--OK---------------|
     |                      |--PublishNext------->|                    |
     |                      |  (next task msg)     |                    |
     |                      |                     |--SetNextPublished->|
     |                      |                     |<--OK---------------|
     |                      |<--CommitOffset------|                    |
```

#### Priority Routing
Tasks are dispatched to the appropriate priority topic based on governance token:
- `CRITICAL` → `aeos.tasks.critical`
- `HIGH` → `aeos.tasks.high`
- `NORMAL` → `aeos.tasks.normal` (default)
- `LOW` → `aeos.tasks.low`
- `BATCH` → `aeos.tasks.batch`

#### Consumer Group
All task topics use `group_id="aeos-workers"` (competing consumer — one worker processes each task).

---

## PROTO-007

### Task Retry Protocol

**Category:** Execution  
**RFC Reference:** §11.4  
**ADR Reference:** ADR-018

#### Retry Decision Tree

```
Step execution completes with failure:
  ↓
Is this a retriable error? (check RetryPolicy)
  → Non-retriable (e.g., INVALID_INPUT, POLICY_REJECTED):
     Set status=FAILED, write to dead-letter topic
  → Retriable (e.g., TIMEOUT, UPSTREAM_UNAVAILABLE):
     ↓
Has retry_count < max_retries?
  → No: Set status=FAILED, write to dead-letter topic
  → Yes:
     ↓
Is circuit breaker OPEN for the capability?
  → Yes: Do NOT retry; wait for circuit breaker to enter HALF_OPEN
  → No:
     ↓
Calculate backoff: min(base * 2^attempt, max_backoff) + jitter
Schedule retry message in Kafka with delay header
Increment retry_count in task envelope
```

#### Retry Policy (Default Values)
```
max_retries: 3
base_backoff_s: 1.0
max_backoff_s: 60.0
jitter_factor: 0.25
retriable_errors: [TIMEOUT, UPSTREAM_UNAVAILABLE, RATE_LIMITED, RESOURCE_EXHAUSTED]
non_retriable_errors: [INVALID_INPUT, POLICY_REJECTED, UNAUTHORIZED, NOT_FOUND]
```

#### Dead-Letter Protocol
Tasks that exhaust retries are published to `aeos.tasks.dead_letter` with:
- Original task envelope (unchanged)
- `failure_reason`: last error message
- `retry_history[]`: array of attempt timestamps and error codes
- `dlq_at_ns`: DLQ entry timestamp

Dead-letter tasks are NOT automatically retried. Human or automated intervention is required.

---

## PROTO-008

### Two-Phase Checkpoint Protocol

**Category:** Execution  
**RFC Reference:** §7.3  
**ADR Reference:** ADR-003

#### Full Protocol (Success Path)

```
Worker                          Redis                    Kafka
  |                               |                        |
  | [Step execution completes]    |                        |
  |                               |                        |
  | Phase 1: Atomic write         |                        |
  |---MULTI---------------------->|                        |
  |---SET result_key result------>|                        |
  |---SET status_key COMPLETED--->|                        |
  |---SETEX idem_key 86400 "1"--->|                        |
  |---EXEC----------------------->|                        |
  |<--[OK, OK, OK]----------------|                        |
  |                               |                        |
  | Phase 2: Publish next         |                        |
  |---Produce(next_task_msg)----->|                        Kafka
  |                               |           <--ACK-------|
  |---SET next_published_key "1"->|                        |
  |<--OK--------------------------|                        |
  |                               |                        |
  | Commit offset                 |                        |
  |---CommitOffset(offset+1)----->|                        Kafka
  |                               |           <--ACK-------|
  |                               |                        |
  | Delete lease                  |                        |
  |---DEL lease_key-------------->|                        |
  |<--1---------------------------|                        |
```

#### Recovery from Partial Failure

| State | Detection | Recovery Action |
|-------|-----------|----------------|
| Phase 1 not started | `result_key` absent | Kafka redelivers task; worker re-executes (idempotency key absent) |
| Phase 1 complete, Phase 2 not started | `result_key` present, `next_published` absent | Orphan scanner: re-publish next task(s), set `next_published=true` |
| Phase 2 complete, offset not committed | `next_published=true`, Kafka offset behind | Kafka redelivers task; worker finds `idem_key` present; returns cached result; re-publishes next (idempotent); commits offset |
| Offset committed, lease not deleted | `lease_key` present with stale TTL | Lease TTL expires naturally (max 120s); no action required |

---

## PROTO-009

### Checkpoint Recovery Protocol

**Category:** Execution  
**RFC Reference:** §7.3, §16.2

#### Orphan Scanner (runs every 60 seconds on Cluster Manager)

**Pattern 1 — Stale heartbeat + RUNNING status:**
```
Find workflows where:
  step.status = EXECUTING
  AND last_heartbeat < now - 15s  (3 missed heartbeats)
  
Action:
  Transition worker to SUSPECTED
  After 2 more missed heartbeats: FAILED
  Trigger Pattern 3 scan for this workflow
```

**Pattern 2 — Expired lease + accepted/executing status:**
```
Find Redis keys matching:
  {wf:*}:step:*:lease  where TTL = -2 (expired/missing)
  AND {wf:*}:step:*:status = "accepted" OR "executing"

Action:
  Requeue step to Kafka with same task envelope
  (idempotency key prevents re-execution if step actually completed)
```

**Pattern 3 — Completed step, `next_published` absent:**
```
Find Redis keys matching:
  {wf:*}:step:*:status = "completed"
  AND {wf:*}:step:*:next_published does not exist

Action:
  Re-derive next step(s) from execution plan
  Publish next task(s) to Kafka
  Set next_published = "1"
```

---

## PROTO-010

### Worker Registration Protocol

**Category:** Capability  
**RFC Reference:** §6.2.3

This protocol is a sub-step of PROTO-001 (Node Join). Capabilities MUST be registered with the Capability Registry BEFORE Kafka partition assignment.

```
CM-L → CR: RegisterWorkerCapabilities {
  node_id: "worker-042",
  capabilities: [
    { name: "research.web_search", version: "1.0", max_concurrency: 5 },
    { name: "analysis.summarize", version: "2.1", max_concurrency: 10 },
  ],
  region: "us-east-1",
  az: "us-east-1a",
  worker_grpc_endpoint: "worker-042.aeos.svc:9090"
}

CR → CM-L: RegisterResponse {
  registered: ["research.web_search", "analysis.summarize"],
  failed: [],
  registry_version: 142
}
```

---

## PROTO-011

### Capability Advertisement Protocol

**Category:** Capability  
**RFC Reference:** §11

#### Capability Record Schema
```
Capability {
  name: string              # e.g., "research.web_search"
  version: string           # semver
  worker_id: string
  endpoint: string          # gRPC address
  max_concurrency: int32
  current_load: float       # 0.0–1.0
  health: HEALTHY | DEGRADED | UNAVAILABLE
  metadata: map<string, string>
  ttl_seconds: int32        # capability advertisement TTL
  registered_at_ns: int64
}
```

#### Capability Heartbeat
Workers refresh capability advertisements every `ttl_seconds / 2`. If the Capability Registry does not receive a refresh before TTL expiry, the capability is automatically deregistered.

---

## PROTO-012

### Capability Discovery Protocol

**Category:** Capability  
**RFC Reference:** §11

```
Caller → CR: LookupCapability {
  capability_name: "research.web_search",
  version_constraint: ">=1.0,<2.0",
  require_health: HEALTHY,
  max_load: 0.8
}

CR → Caller: LookupResponse {
  workers: [
    { worker_id: "worker-042", endpoint: "...", current_load: 0.3 },
    { worker_id: "worker-067", endpoint: "...", current_load: 0.5 },
  ],
  registry_version: 142
}
```

#### Load Balancing
Callers SHOULD select the worker with the lowest `current_load`. If loads are within 0.1 of each other, random selection is acceptable.

---

## PROTO-013

### Governance Token Issuance Protocol

**Category:** Governance  
**RFC Reference:** §13.2, §12.3  
**ADR Reference:** ADR-004, ADR-005

```
API Gateway → Policy Service: EvaluateRequest {
  task_id: uuid,
  task_type: "research.web_search",
  submitter_id: "user-123",
  deadline_unix: 1751878800,
  priority: HIGH,
  metadata: {...}
}

Policy Service:
  1. Look up matching policies for task_type
  2. If no match: return REJECTED(reason="no_policy_matched")
  3. If timeout (5s): return REJECTED(reason="evaluation_timeout") HTTP 503
  4. Evaluate all matching policies in priority order
  5. If any REJECT: return REJECTED(reason=policy.reason)
  6. All APPROVE: calculate token expiry (ADR-005 formula)
  7. Issue token: HMAC-SHA256 signed JWT

Policy Service → API Gateway: EvaluateResponse {
  decision: APPROVED,
  token: "eyJ...",  # JWT
  expiry_unix: 1751968800,
  policy_id: "policy-research-web-001",
  token_id: uuid   # for revocation
}
```

#### Token Contents (JWT Payload)
```json
{
  "token_id": "uuid",
  "task_id": "uuid",
  "task_type": "research.web_search",
  "submitter_id": "user-123",
  "issued_at": 1751792400,
  "expires_at": 1751968800,
  "policy_id": "policy-research-web-001",
  "decision": "APPROVED",
  "allowed_capabilities": ["research.web_search"],
  "max_compute_units": 100
}
```

---

## PROTO-014

### Governance Token Refresh Protocol

**Category:** Governance  
**RFC Reference:** §13.2.2  
**ADR Reference:** ADR-005

```
Worker (5 min before token expiry):

W → Policy Service: RefreshTokenRequest {
  token_id: "uuid",
  current_token: "eyJ...",
  worker_node_id: "worker-042"
}

Policy Service:
  1. Validate current token signature
  2. Re-evaluate original task against current policies
  3a. Still APPROVED: issue new token with extended expiry
  3b. Now REJECTED: return REVOKED signal

Policy Service → W: RefreshTokenResponse {
  decision: APPROVED | REVOKED,
  new_token: "eyJ..." | null,
  reason: null | "policy_changed" | "token_revoked"
}

If REVOKED:
  W → Workflow: Graceful termination
  W: Checkpoint current step
  W: Publish WORKFLOW_CANCELLED event
```

If Policy Service is unavailable during refresh:
- Worker pauses task acceptance (not failure)
- Retry on circuit breaker schedule (exponential backoff: 1s, 2s, 4s, 8s, max 30s)
- If unavailable for > 5 minutes: escalate to Cluster Manager; mark task as SUSPENDED

---

## PROTO-015

### RBAC Revocation Protocol

**Category:** Security  
**RFC Reference:** §12.4  
**ADR Reference:** ADR-011

```
Admin API → Kafka: RevocationEvent {
  entity_id: "api-key-xyz",
  entity_type: API_KEY | USER | ROLE,
  revoked_permissions: ["research.web_search", "analysis.*"],
  effective_at_ns: 1751792400000000000,
  revocation_id: uuid
}

Topic: aeos.events.governance
Group ID: aeos-worker-{node_id}  ← per-worker fan-out

Each Worker (on receipt of RevocationEvent):
  1. Match entity_id against local permission cache
  2. Invalidate all matching cache entries immediately
  3. For in-flight tasks using a token for entity_id:
     a. Complete current step (step is already authorized)
     b. After step completion: re-validate token before proceeding
     c. If token references revoked permission: cancel workflow
  4. ACK Kafka event
```

**Maximum propagation latency:** < 1 second (Kafka delivery SLA)

---

## PROTO-016

### Memory Synchronization Protocol

**Category:** Memory  
**RFC Reference:** §8

#### Working Memory (WTM) — Synchronous
WTM reads/writes are synchronous Redis Cluster operations. No async synchronization protocol; consistency is provided by Redis Cluster replication (primary→replica synchronous replication within shard).

#### LTM — Asynchronous
LTM writes to Postgres are synchronous. Postgres streaming replication (async to read replicas, sync to one synchronous replica) handles durability.

#### Episodic Memory — Eventual Consistency
Weaviate vector writes are eventually consistent. Protocol:
1. Write to Weaviate (consistency: QUORUM)
2. Write to local in-memory write-ahead buffer (TTL: 5s)
3. Reads within 5s of write: served from local buffer
4. Reads after 5s: served from Weaviate (propagation complete)

---

## PROTO-017

### LTM Replication Protocol

**Category:** Memory  
**RFC Reference:** §8.3

LTM is replicated via Postgres streaming replication:

```
Worker → Postgres Primary: INSERT INTO ltm_entries (...)
Primary → Synchronous Replica: WAL record (synchronous_commit=on)
Primary → Async Replicas: WAL record (async, <1s lag typical)
```

Write acknowledgment is returned after the synchronous replica confirms WAL receipt. This ensures LTM writes survive a single primary failure without data loss.

**Vector embeddings (Weaviate):** Weaviate's internal RAFT-based replication handles vector store replication. AEOS does not manage Weaviate replication directly; Weaviate's `replicationFactor: 2` ensures one-replica redundancy.

---

## PROTO-018

### Security Token (mTLS Cert) Rotation Protocol

**Category:** Security  
**RFC Reference:** §12.2  
**ADR Reference:** ADR-010

```
Vault Agent Sidecar (runs in every pod):

Every 12 hours (half of 24h TTL):
  1. Request new leaf certificate from Vault Intermediate CA:
     vault write pki/issue/aeos-internal \
       common_name="worker-042.aeos.svc.cluster.local" \
       ttl="24h"
  
  2. Vault issues new cert (signed by Intermediate CA)
  
  3. Vault Agent writes new cert to pod shared volume:
     /var/run/secrets/tls/tls.crt
     /var/run/secrets/tls/tls.key
  
  4. Vault Agent sends SIGHUP to the service process (or calls reload endpoint)
  
  5. Service reloads TLS configuration from shared volume
     WITHOUT process restart (hot reload required by AC-NET-002)
  
  6. Old cert remains valid until its 24h expiry
     (overlap prevents hard cutover issues)
```

---

## PROTO-019

### Execution Lease Acquisition Protocol

**Category:** Execution  
**RFC Reference:** §7.3, §16.5  
**ADR Reference:** ADR-009

```
Worker attempting to execute step:

1. Attempt lease acquisition:
   SETNX {wf:<wf_id>}:step:<step_id>:lease <worker_node_id> EX 120

2a. Returns 1 (success):
   Worker acquired lease; proceed to execution
   Start lease renewal background task (every 60s):
     EXPIRE {wf:<wf_id>}:step:<step_id>:lease 120

2b. Returns 0 (failure):
   Another worker holds the lease
   DO NOT execute step
   Log: "step <step_id> lease held by another worker; skipping"

3. On step completion:
   DEL {wf:<wf_id>}:step:<step_id>:lease

4. On step failure (not crash):
   DEL {wf:<wf_id>}:step:<step_id>:lease
   (allows retry to acquire a fresh lease)

5. On worker crash (lease not deleted):
   Lease TTL expires after 120s
   Orphan scanner (PROTO-009 Pattern 2) detects and requeues
```

---

## PROTO-020

### Cluster Drain Protocol (Rolling Deployment)

**Category:** Operations  
**RFC Reference:** §6.2.3

Used during rolling deployments, maintenance, or voluntary node removal.

```
Deployment Controller → CM-L: DrainRequest {
  node_id: "worker-042",
  timeout_s: 300,
  reason: "rolling_update"
}

CM-L:
  1. Commit DRAINING entry to Raft log
  2. Remove worker-042 from task routing (no new tasks assigned)
  3. Notify worker-042: transition to DRAINING state

worker-042 (on DRAINING notification):
  1. Stop consuming from Kafka (pause consumer)
  2. Complete all in-flight steps (or checkpoint them)
  3. When in_flight_tasks == 0:
     Send LeaveRequest to CM-L (PROTO-002)

CM-L (on LeaveRequest):
  1. Commit LEFT entry to Raft log
  2. Trigger Kafka partition rebalance
  3. Return DrainComplete to Deployment Controller

[Deployment Controller proceeds to replace the pod]
```

**PodDisruptionBudget enforcement:** Kubernetes enforces `minAvailable: 3` for workers. The Deployment Controller will wait if draining would reduce available workers below 3.

---

*End of Protocol Specification — `016-PROTOCOL_SPECIFICATION.md`*
