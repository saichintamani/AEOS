# AEOS Phase 9 DRP — State Machine Specification

**Document:** `017-STATE_MACHINE_SPECIFICATION.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

---

## Purpose

Every subsystem in AEOS Phase 9 that has observable state MUST implement the deterministic state machine defined in this document. Invalid transitions MUST raise a `StateMachineViolation` exception and be logged at ERROR severity. No implicit state transitions are permitted.

State machine IDs are referenced by conformance tests in `021-CONFORMANCE_TEST_PLAN.md`.

---

## State Machine Index

| ID | Subsystem | States | Terminal States |
|----|-----------|--------|----------------|
| [SM-KERNEL](#sm-kernel) | HyperKernel | 7 | STOPPED |
| [SM-WORKER](#sm-worker) | Worker Node | 6 | STOPPED |
| [SM-CLUSTER-MEMBER](#sm-cluster-member) | Cluster Membership | 6 | LEFT, FAILED |
| [SM-RAFT](#sm-raft) | Raft Node | 3 | — |
| [SM-TASK](#sm-task) | Task Execution | 7 | COMPLETED, FAILED, CANCELLED, TIMEOUT |
| [SM-WORKFLOW](#sm-workflow) | Workflow | 6 | COMPLETED, FAILED, CANCELLED |
| [SM-CHECKPOINT](#sm-checkpoint) | Checkpoint | 4 | COMPLETE |
| [SM-MEMORY](#sm-memory) | Memory Entry | 4 | EXPIRED |
| [SM-CAPABILITY](#sm-capability) | Capability Advertisement | 4 | DEREGISTERED |
| [SM-GOVERNANCE](#sm-governance) | Governance Token | 5 | APPROVED, REJECTED, EXPIRED, REVOKED |
| [SM-CIRCUIT-BREAKER](#sm-circuit-breaker) | Circuit Breaker | 3 | — |
| [SM-CLUSTER](#sm-cluster) | Cluster (global) | 4 | TERMINATING |

---

## SM-KERNEL

### HyperKernel State Machine

#### States
| State | Description |
|-------|-------------|
| `INITIALIZING` | Loading config, initializing logging and telemetry |
| `LOADING` | Loading plugins, service discovery |
| `CONFIGURING` | Applying policies, configuring services |
| `STARTING` | Starting local services (event bus, service registry) |
| `JOINING` | Registering with cluster, acquiring Kafka partitions |
| `RUNNING` | Accepting work; all subsystems operational |
| `STOPPING` | Draining tasks, deregistering, flushing state |
| `STOPPED` | Terminal; process may exit |

#### Transitions
```
INITIALIZING → LOADING          [event: init_complete]
LOADING      → CONFIGURING      [event: plugins_loaded]
CONFIGURING  → STARTING         [event: config_applied]
STARTING     → JOINING          [event: local_services_started]
JOINING      → RUNNING          [event: cluster_joined, partitions_assigned]
RUNNING      → STOPPING         [event: sigterm | shutdown_requested]
STOPPING     → STOPPED          [event: drain_complete, resources_released]

-- Error transitions --
INITIALIZING → STOPPED          [event: fatal_error(phase=INITIALIZING)]
LOADING      → STOPPED          [event: fatal_error(phase=LOADING)]
CONFIGURING  → STOPPED          [event: fatal_error(phase=CONFIGURING)]
STARTING     → STOPPED          [event: fatal_error(phase=STARTING)]
JOINING      → STOPPING         [event: join_failed] → STOPPED
```

#### Invalid Transitions (MUST raise StateMachineViolation)
```
Any state → INITIALIZING        [kernel cannot restart without process restart]
RUNNING   → LOADING             [no backward transitions]
RUNNING   → STARTING            [no backward transitions]
STOPPED   → any                 [terminal: no exit from STOPPED]
```

#### State Invariants
- In `JOINING`: no tasks may be accepted
- In `RUNNING`: all required services must be healthy; kernel health check returns 200
- In `STOPPING`: no new tasks may be accepted; in-flight tasks may continue up to drain timeout
- In `STOPPED`: all Kafka consumers must be closed; all Redis connections must be returned to pool; all gRPC channels must be closed

---

## SM-WORKER

### Worker Node State Machine

#### States
| State | Description |
|-------|-------------|
| `INITIALIZING` | Worker process starting up |
| `JOINING` | Registering with cluster (PROTO-001) |
| `RUNNING` | Consuming tasks from Kafka |
| `SUSPENDED` | Temporarily stopped due to governance circuit breaker open |
| `DRAINING` | Completing in-flight tasks; no new task acceptance (PROTO-020) |
| `STOPPED` | Terminal; worker process may exit |

#### Transitions
```
INITIALIZING → JOINING          [event: kernel_reached_JOINING]
JOINING      → RUNNING          [event: join_complete]
RUNNING      → SUSPENDED        [event: governance_circuit_open]
SUSPENDED    → RUNNING          [event: governance_circuit_closed]
RUNNING      → DRAINING         [event: drain_requested | sigterm]
SUSPENDED    → DRAINING         [event: drain_requested | sigterm]
DRAINING     → STOPPED          [event: drain_complete]

-- Failure transitions --
JOINING      → STOPPED          [event: join_timeout | cluster_unavailable]
RUNNING      → STOPPED          [event: fatal_error]
```

#### State Invariants
- In `RUNNING`: `in_flight_tasks < max_concurrent_tasks`
- In `RUNNING`: Kafka consumer is active
- In `SUSPENDED`: Kafka consumer is paused; in-flight tasks continue to completion
- In `DRAINING`: `no new task acquisitions`; `heartbeat continues`
- In `STOPPED`: `in_flight_tasks == 0`

---

## SM-CLUSTER-MEMBER

### Cluster Member State Machine

#### States
| State | Description |
|-------|-------------|
| `JOINING` | Node is completing join protocol |
| `RUNNING` | Node is healthy and accepting tasks |
| `SUSPECTED` | 3 missed heartbeats; excluded from routing |
| `DRAINING` | Gracefully leaving; no new tasks |
| `LEFT` | Clean departure (terminal) |
| `FAILED` | Unclean departure; 5 missed heartbeats (terminal) |

#### Transitions
```
JOINING   → RUNNING             [event: join_complete]
RUNNING   → SUSPECTED           [event: missed_heartbeats(count=3)]
RUNNING   → DRAINING            [event: drain_requested]
SUSPECTED → RUNNING             [event: heartbeat_received]
SUSPECTED → FAILED              [event: missed_heartbeats(count=5)]
DRAINING  → LEFT                [event: leave_request_received, log_committed]

-- All transitions committed to Raft log before state change is visible --
```

#### Invalid Transitions
```
LEFT    → any                   [terminal]
FAILED  → any                   [terminal; recovery requires new JOINING cycle]
RUNNING → JOINING               [no regression]
```

#### State Invariants
- In `SUSPECTED`: no new tasks routed to this member; capabilities remain in registry
- In `FAILED`: capabilities deregistered from registry; Kafka partitions released
- In `DRAINING`: no new tasks accepted; current in-flight tasks continue

---

## SM-RAFT

### Raft Node State Machine

#### States
| State | Description |
|-------|-------------|
| `FOLLOWER` | Receiving AppendEntries from leader; responding to RequestVote |
| `CANDIDATE` | Election in progress; soliciting votes |
| `LEADER` | Sending AppendEntries; handling client requests |

#### Transitions
```
FOLLOWER  → CANDIDATE           [event: election_timeout_expired]
CANDIDATE → LEADER              [event: received_votes >= quorum]
CANDIDATE → FOLLOWER            [event: received_AppendEntries(term >= currentTerm)]
CANDIDATE → FOLLOWER            [event: received_RequestVote(term > currentTerm)]
LEADER    → FOLLOWER            [event: received_message(term > currentTerm)]
CANDIDATE → CANDIDATE           [event: election_timeout (split vote); new election]
```

#### State Invariants
- In `LEADER`: heartbeat (empty AppendEntries) sent every 100ms
- In `FOLLOWER`: election timeout reset on valid AppendEntries receipt
- In `CANDIDATE`: election timeout restarted with new randomized value on split vote
- Any state: if received term > currentTerm → revert to FOLLOWER, update currentTerm

#### Persistent State (must survive process restart)
- `currentTerm`: current Raft term
- `votedFor`: node_id voted for in current term
- `log[]`: all log entries

All persistent state mutations MUST be fsynced to WAL before sending any RPC response.

---

## SM-TASK

### Task Execution State Machine

#### States
| State | Description |
|-------|-------------|
| `SUBMITTED` | Task received by API Gateway; governance token being issued |
| `QUEUED` | Token issued; task message published to Kafka |
| `ACCEPTED` | Worker has pulled task from Kafka; lease acquired |
| `EXECUTING` | Worker is actively executing the step |
| `COMPLETED` | Step completed successfully (terminal) |
| `FAILED` | Step failed (terminal) |
| `CANCELLED` | Task cancelled by user or governance revocation (terminal) |
| `TIMEOUT` | Task exceeded deadline without completion (terminal) |

#### Transitions
```
SUBMITTED → QUEUED              [event: governance_token_issued]
SUBMITTED → FAILED              [event: governance_rejected]
QUEUED    → ACCEPTED            [event: worker_lease_acquired]
QUEUED    → TIMEOUT             [event: deadline_exceeded_while_queued]
ACCEPTED  → EXECUTING           [event: execution_started]
ACCEPTED  → FAILED              [event: execution_refused(no_capability)]
EXECUTING → COMPLETED           [event: step_execution_complete, checkpoint_phase2_done]
EXECUTING → FAILED              [event: step_execution_error, retries_exhausted]
EXECUTING → CANCELLED           [event: cancellation_requested | governance_revoked]
EXECUTING → TIMEOUT             [event: deadline_exceeded_while_executing]
FAILED    → QUEUED              [event: retry_scheduled]    ← only if retries remain
```

#### Invalid Transitions (MUST raise StateMachineViolation)
```
COMPLETED → any                 [terminal]
FAILED    → EXECUTING           [only valid retry path is FAILED → QUEUED]
CANCELLED → any                 [terminal]
TIMEOUT   → any                 [terminal]
```

#### State Invariants
- In `EXECUTING`: execution lease held in Redis
- In `EXECUTING`: governance token has not expired (or re-evaluation in progress)
- In `COMPLETED`: Phase 1 and Phase 2 checkpoint complete; Kafka offset committed
- In `FAILED` (with retries): retry message published to Kafka with backoff delay

---

## SM-WORKFLOW

### Workflow State Machine

#### States
| State | Description |
|-------|-------------|
| `INITIALIZING` | Workflow being compiled from ExecutionGraph |
| `RUNNING` | One or more steps currently executing |
| `WAITING` | All current steps complete; waiting for next step trigger |
| `PAUSED` | Governance suspension or human approval gate |
| `COMPLETED` | All steps completed; final aggregation done (terminal) |
| `FAILED` | One or more steps failed beyond retry limit (terminal) |
| `CANCELLED` | Explicitly cancelled by user or governance (terminal) |

#### Transitions
```
INITIALIZING → RUNNING          [event: first_step_dispatched]
RUNNING      → WAITING          [event: current_steps_complete, next_steps_pending]
WAITING      → RUNNING          [event: next_steps_dispatched]
RUNNING      → PAUSED           [event: human_approval_gate | governance_circuit_open]
PAUSED       → RUNNING          [event: approval_granted | governance_circuit_closed]
RUNNING      → COMPLETED        [event: all_steps_complete, aggregation_done]
WAITING      → COMPLETED        [event: no_more_steps_to_dispatch]
RUNNING      → FAILED           [event: step_failed, retries_exhausted, workflow_policy=FAIL_FAST]
PAUSED       → CANCELLED        [event: timeout_waiting_for_approval]
any          → CANCELLED        [event: explicit_cancel_request]
```

---

## SM-CHECKPOINT

### Checkpoint State Machine

#### States
| State | Description |
|-------|-------------|
| `PENDING` | Step execution complete; checkpoint not started |
| `PHASE_1` | Phase 1 MULTI/EXEC write in progress |
| `PHASE_2` | Phase 1 complete; publishing next tasks |
| `COMPLETE` | Phase 2 complete; Kafka offset committed (terminal) |

#### Transitions
```
PENDING  → PHASE_1              [event: start_checkpoint]
PHASE_1  → PHASE_2              [event: multi_exec_success]
PHASE_1  → PENDING              [event: multi_exec_failed]  ← retry from start
PHASE_2  → COMPLETE             [event: next_published, offset_committed]
PHASE_2  → PHASE_2              [event: kafka_publish_retry]  ← retry publish
```

#### Invalid Transitions
```
COMPLETE → any                  [terminal; checkpoint is immutable once complete]
```

---

## SM-MEMORY

### Memory Entry State Machine

#### States
| State | Description |
|-------|-------------|
| `ACTIVE` | Entry is valid and accessible |
| `STALE` | Entry past TTL but not yet evicted |
| `EVICTING` | Entry scheduled for removal |
| `EXPIRED` | Entry removed from store (terminal) |

#### Transitions
```
ACTIVE   → STALE                [event: ttl_exceeded]
STALE    → ACTIVE               [event: ttl_refreshed]
STALE    → EVICTING             [event: eviction_scheduled]
EVICTING → EXPIRED              [event: eviction_complete]
ACTIVE   → EVICTING             [event: explicit_delete]
```

---

## SM-CAPABILITY

### Capability Advertisement State Machine

#### States
| State | Description |
|-------|-------------|
| `REGISTERING` | Registration in progress |
| `AVAILABLE` | Capability is discoverable and healthy |
| `DEGRADED` | Capability is discoverable but degraded |
| `DEREGISTERED` | Capability removed from registry (terminal) |

#### Transitions
```
REGISTERING  → AVAILABLE        [event: registration_confirmed]
AVAILABLE    → DEGRADED         [event: health_check_degraded]
DEGRADED     → AVAILABLE        [event: health_check_healthy]
AVAILABLE    → DEREGISTERED     [event: deregister_request | ttl_expired | worker_failed]
DEGRADED     → DEREGISTERED     [event: deregister_request | ttl_expired | worker_failed]
REGISTERING  → DEREGISTERED     [event: registration_failed]
```

#### State Invariants
- In `AVAILABLE`: capability is included in LookupCapability results
- In `DEGRADED`: capability is included in LookupCapability results only if no AVAILABLE alternative exists
- In `DEREGISTERED`: capability is excluded from all lookups

---

## SM-GOVERNANCE

### Governance Token State Machine

#### States
| State | Description |
|-------|-------------|
| `EVALUATING` | Policy Service is evaluating the request |
| `APPROVED` | Token issued; task may execute (terminal for initial decision) |
| `REJECTED` | Token denied; task must not execute (terminal) |
| `EXPIRED` | Token TTL exceeded; re-evaluation required |
| `REVOKED` | Token explicitly revoked (terminal) |

#### Transitions
```
EVALUATING → APPROVED           [event: all_policies_approved]
EVALUATING → REJECTED           [event: any_policy_rejected | evaluation_timeout | no_policy_matched]
APPROVED   → EXPIRED            [event: token_ttl_exceeded]
EXPIRED    → APPROVED           [event: re_evaluation_approved]
EXPIRED    → REVOKED            [event: re_evaluation_rejected]
APPROVED   → REVOKED            [event: revocation_event_received]
```

#### Invalid Transitions
```
REJECTED   → any                [terminal; new submission required]
REVOKED    → any                [terminal; new submission required]
```

#### State Invariants
- In `EVALUATING`: task is in SUBMITTED state; no execution is occurring
- In `APPROVED`: worker may proceed with step execution
- In `EXPIRED`: worker MUST pause execution and initiate re-evaluation; task remains in EXECUTING state
- In `REVOKED`: worker MUST stop execution and transition task to CANCELLED

---

## SM-CIRCUIT-BREAKER

### Circuit Breaker State Machine

#### States
| State | Description |
|-------|-------------|
| `CLOSED` | Normal operation; requests pass through |
| `OPEN` | Failure threshold exceeded; requests rejected immediately |
| `HALF_OPEN` | Trial period; limited requests permitted to test recovery |

#### Transitions
```
CLOSED    → OPEN                [event: failure_count >= threshold (default: 5 in 60s)]
OPEN      → HALF_OPEN           [event: reset_timeout_elapsed (default: 30s)]
HALF_OPEN → CLOSED              [event: trial_request_succeeded]
HALF_OPEN → OPEN                [event: trial_request_failed]
```

#### State Invariants
- In `CLOSED`: all requests pass through; failure counter incremented on failure
- In `OPEN`: all requests immediately return `CircuitBreakerOpenError`; failure counter frozen
- In `HALF_OPEN`: exactly 1 trial request permitted at a time; concurrent requests rejected (not permitted as trials)

---

## SM-CLUSTER

### Cluster State Machine (Global)

#### States
| State | Description |
|-------|-------------|
| `BOOTSTRAPPING` | Cluster initializing; Raft consensus not yet established |
| `DEGRADED` | Raft quorum maintained but below optimal size |
| `HEALTHY` | Quorum established; operating normally |
| `TERMINATING` | Cluster graceful shutdown in progress |

#### Transitions
```
BOOTSTRAPPING → HEALTHY         [event: raft_quorum_established, min_workers_joined]
BOOTSTRAPPING → DEGRADED        [event: raft_quorum_established, workers_below_minimum]
HEALTHY       → DEGRADED        [event: worker_count < min_workers | raft_member_failed]
DEGRADED      → HEALTHY         [event: worker_count >= min_workers AND raft_quorum_maintained]
HEALTHY       → TERMINATING     [event: cluster_shutdown_requested]
DEGRADED      → TERMINATING     [event: cluster_shutdown_requested]
```

#### State Invariants
- In `BOOTSTRAPPING`: no tasks may be accepted by any worker
- In `DEGRADED`: tasks accepted; alert fired; autoscaler attempts recovery
- In `HEALTHY`: all NFRs in effect
- In `TERMINATING`: no new task submissions accepted at API Gateway level

---

## State Machine Implementation Requirements

### SMR-001 — Explicit State Storage
Every state machine's current state MUST be stored in a named field (e.g., `self.state: TaskState`). State MUST NOT be inferred from other fields.

### SMR-002 — Transition Validation
Every state machine MUST validate transitions before applying them. The validation function MUST check both the current state and the event against the allowed transitions table.

### SMR-003 — Transition Logging
Every state transition MUST produce a structured log entry:
```json
{
  "event": "state_transition",
  "machine": "SM-TASK",
  "entity_id": "task-uuid",
  "from_state": "EXECUTING",
  "to_state": "COMPLETED",
  "trigger_event": "step_execution_complete",
  "timestamp_ns": 1751792400000000000
}
```

### SMR-004 — Distributed State
For state machines that are shared across multiple nodes (SM-CLUSTER-MEMBER, SM-CLUSTER), state transitions MUST be committed to the Raft log before being applied locally. Redis is a read-through cache; it MUST NOT be the source of truth for state machine state.

### SMR-005 — Invalid Transition Handling
An invalid transition MUST:
1. NOT be applied
2. Raise `StateMachineViolation(machine=..., from_state=..., event=..., to_state=...)`
3. Log at ERROR level with full context
4. Increment the `aeos_state_machine_violations_total{machine}` Prometheus counter

---

*End of State Machine Specification — `017-STATE_MACHINE_SPECIFICATION.md`*
