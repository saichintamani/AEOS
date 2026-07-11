# AEOS Phase 9 DRP — Sequence Diagrams

**Document:** `018-SEQUENCE_DIAGRAMS.md`  
**Suite:** Architecture Verification Suite (AVS)  
**Status:** Approved  
**Date:** 2026-07-06

All diagrams are in PlantUML notation for rendering in documentation tools and IDEs.

---

## Diagram Index

| ID | Title |
|----|-------|
| [SEQ-001](#seq-001) | Happy Path Task Execution |
| [SEQ-002](#seq-002) | Distributed Fan-Out / Fan-In Execution |
| [SEQ-003](#seq-003) | Raft Leader Election |
| [SEQ-004](#seq-004) | Worker Crash and Recovery |
| [SEQ-005](#seq-005) | Checkpoint Recovery (Orphan Scan) |
| [SEQ-006](#seq-006) | Cluster Join |
| [SEQ-007](#seq-007) | Rolling Deployment (Drain Protocol) |
| [SEQ-008](#seq-008) | KEDA Autoscaling |
| [SEQ-009](#seq-009) | Governance Token Approval and Execution |
| [SEQ-010](#seq-010) | Governance Token Revocation |
| [SEQ-011](#seq-011) | mTLS Certificate Rotation |
| [SEQ-012](#seq-012) | Split-Brain Prevention via Execution Lease |

---

## SEQ-001

### Happy Path Task Execution

```plantuml
@startuml SEQ-001-HappyPath

participant "Client" as C
participant "API Gateway" as GW
participant "Policy Service" as PS
participant "Kafka" as K
participant "Worker A" as WA
participant "Redis Cluster" as R
participant "Capability (LLM)" as CAP

C -> GW : POST /api/v1/tasks\n{task_type, payload, deadline}
activate GW

GW -> PS : EvaluateRequest\n{task_type, submitter_id, deadline}
activate PS
PS --> GW : EvaluateResponse\n{decision=APPROVED, token, expiry}
deactivate PS

GW -> K : Produce(aeos.tasks.normal, TaskMessage\n{task_id, token, payload, deadline})
K --> GW : ack(offset)
GW --> C : HTTP 202\n{task_id, status=QUEUED}
deactivate GW

WA -> K : Poll(aeos.tasks.normal, group_id=aeos-workers)
K --> WA : TaskMessage

activate WA
WA -> R : SETNX {wf:X}:step:1:lease worker-A EX 120
R --> WA : 1 (acquired)

WA -> WA : ValidateToken\n(signature, expiry, revocation_cache)
WA -> R : GET {wf:X}:step:1:idem
R --> WA : nil (not seen)

WA -> CAP : ExecuteCapability(research.web_search, payload)
activate CAP
CAP --> WA : result
deactivate CAP

note over WA,R : Phase 1 Checkpoint (atomic)
WA -> R : MULTI\nSET {wf:X}:step:1:result <result>\nSET {wf:X}:step:1:status COMPLETED\nSETEX {wf:X}:step:1:idem 86400 "1"\nEXEC
R --> WA : [OK, OK, OK]

note over WA,K : Phase 2
WA -> K : Produce(aeos.tasks.normal, NextTaskMessage)
K --> WA : ack
WA -> R : SET {wf:X}:step:1:next_published 1
R --> WA : OK

WA -> K : CommitOffset(offset+1)
K --> WA : ack
WA -> R : DEL {wf:X}:step:1:lease
R --> WA : 1
deactivate WA

@enduml
```

---

## SEQ-002

### Distributed Fan-Out / Fan-In Execution

```plantuml
@startuml SEQ-002-FanOut

participant "Workflow Engine" as WE
participant "Kafka" as K
participant "Worker A" as WA
participant "Worker B" as WB
participant "Worker C" as WC
participant "Redis" as R

WE -> K : Produce(ParallelDispatch)\n[Step 2A, Step 2B, Step 2C]
activate K

K --> WA : Step 2A
K --> WB : Step 2B
K --> WC : Step 2C

activate WA
activate WB
activate WC

WA -> R : AcquireLease(step:2A)
WB -> R : AcquireLease(step:2B)
WC -> R : AcquireLease(step:2C)
R --> WA : 1
R --> WB : 1
R --> WC : 1

WA -> WA : Execute Step 2A
WB -> WB : Execute Step 2B
WC -> WC : Execute Step 2C

WA -> R : Checkpoint(step:2A, result_A)
WB -> R : Checkpoint(step:2B, result_B)
WC -> R : Checkpoint(step:2C, result_C)

note over WA,WC : Fan-in: MergeNode waits for all 3

WA -> K : Produce(MergeStep, partial_result=A)
WB -> K : Produce(MergeStep, partial_result=B)
WC -> K : Produce(MergeStep, partial_result=C)

deactivate WA
deactivate WB
deactivate WC

note over K : MergeNode in Worker D accumulates\nall 3 results (timeout: 120s)

participant "Worker D" as WD
K --> WD : MergeStep (result_A)
K --> WD : MergeStep (result_B)
K --> WD : MergeStep (result_C)

activate WD
WD -> WD : MergeResults(A, B, C)
WD -> R : Checkpoint(step:3, merged_result)
WD -> K : Produce(NextStep)
deactivate WD

@enduml
```

---

## SEQ-003

### Raft Leader Election

```plantuml
@startuml SEQ-003-LeaderElection

participant "CM-A (crashed)" as A
participant "CM-B (candidate)" as B
participant "CM-C (follower)" as C
participant "Redis" as R

note over A : CM-A crashes

note over B : Election timeout fires (random 150-300ms)

B -> B : currentTerm++ (persist, fsync)\nvotedFor = self (persist, fsync)
B -> B : transition: FOLLOWER → CANDIDATE

B -> C : RequestVote\n{term=8, candidateId=CM-B,\nlastLogIndex=42, lastLogTerm=7}

activate C
C -> C : Check: term(8) > currentTerm(7)\ncurrentTerm=8, votedFor=nil (persist, fsync)
C -> C : Check: log completeness OK
C --> B : VoteGranted\n{term=8, granted=true}
deactivate C

note over B : Received quorum (2/3: self + CM-C)
B -> B : transition: CANDIDATE → LEADER
B -> B : currentTerm=8

B -> C : AppendEntries (heartbeat)\n{term=8, leaderId=CM-B}
activate C
C --> B : AppendEntriesAck\n{term=8, success=true}
deactivate C

B -> R : SET cluster:leader CM-B\nSET cluster:term 8
note over B,C : Leader established\nHeartbeat every 100ms

note over A : CM-A restarts
A -> B : RequestVote\n{term=7, candidateId=CM-A}
B --> A : VoteRejected\n{term=8, granted=false}\n(stale term)
A -> A : currentTerm=8, transition→FOLLOWER
A -> B : AppendEntriesAck (catch-up log sync)

@enduml
```

---

## SEQ-004

### Worker Crash and Recovery

```plantuml
@startuml SEQ-004-WorkerCrash

participant "Worker A" as WA
participant "Cluster Manager" as CM
participant "Kafka" as K
participant "Redis" as R
participant "Orphan Scanner" as OS
participant "Worker B" as WB

WA -> WA : [Executing Step 7 of Workflow W]

note over WA : CRASH (OOM / kernel kill)

loop 3 missed heartbeats (t=15s)
  CM -> WA : Heartbeat probe
  WA --> CM : [no response]
end

CM -> CM : Transition WA: RUNNING → SUSPECTED
CM -> CM : Log SUSPECTED to Raft

loop 2 more missed heartbeats (t=25s)
  CM -> WA : Heartbeat probe
  WA --> CM : [no response]
end

CM -> CM : Transition WA: SUSPECTED → FAILED
CM -> CM : Commit FAILED to Raft log

CM -> K : Release WA's partitions\n(trigger consumer group rebalance)
CM -> CM : Deregister WA capabilities

note over R : Lease: {wf:W}:step:7:lease = WA (TTL: 95s remaining)

note over OS : Orphan scanner fires (t+60s)

OS -> R : SCAN {wf:W}:step:*:status
R --> OS : step:7:status = EXECUTING
OS -> R : TTL {wf:W}:step:7:lease
R --> OS : 35 (still valid!)

note over OS : Wait for lease to expire (35s)

OS -> R : TTL {wf:W}:step:7:lease
R --> OS : -2 (expired)
OS -> R : GET {wf:W}:step:7:idem
R --> OS : nil (step not completed)
OS -> K : Produce(aeos.tasks.normal, Step7Message\n{retry=true, original_task_id=...})

K --> WB : Step7Message (rebalanced partition)

activate WB
WB -> R : SETNX {wf:W}:step:7:lease WB EX 120
R --> WB : 1 (acquired)
WB -> R : GET {wf:W}:step:7:idem
R --> WB : nil (safe to re-execute)
WB -> WB : Re-execute Step 7
WB -> R : Checkpoint(step:7)
WB -> K : Produce(Step 8)
WB -> K : CommitOffset
deactivate WB

@enduml
```

---

## SEQ-005

### Checkpoint Recovery (Orphan Scan — Phase 2 Incomplete)

```plantuml
@startuml SEQ-005-CheckpointRecovery

participant "Worker A" as WA
participant "Redis" as R
participant "Kafka" as K
participant "Orphan Scanner" as OS

note over WA : Phase 1 complete\nPhase 2 starting...

WA -> K : Produce(NextTaskMessage)
note over WA : CRASH before setting next_published

note over R : State:\n{wf:X}:step:5:result = <data>\n{wf:X}:step:5:status = COMPLETED\n{wf:X}:step:5:idem = "1"\n{wf:X}:step:5:next_published = [absent]

note over OS : Orphan scanner fires

OS -> R : SCAN for:\n  status=COMPLETED\n  AND next_published absent
R --> OS : [{wf:X}:step:5 matches]

OS -> OS : Re-derive step 6 from\n  execution plan
OS -> K : Produce(Step6Message)
K --> OS : ack

OS -> R : SET {wf:X}:step:5:next_published 1
R --> OS : OK

note over OS : Recovery complete\nWorkflow resumes from Step 6

note over K : If Step6Message was already\nproduced before crash:\n  Worker checks idem key on\n  receipt → finds key, skips execution\n  (at-least-once + idempotency)

@enduml
```

---

## SEQ-006

### Cluster Join

```plantuml
@startuml SEQ-006-ClusterJoin

participant "New Worker W" as W
participant "CM Leader" as CM
participant "CM Follower 1" as CMF1
participant "CM Follower 2" as CMF2
participant "Capability Registry" as CR
participant "Kafka Admin" as KA
participant "Redis" as R

W -> W : Kernel: STARTING → JOINING

W -> CM : JoinRequest\n{node_id, capabilities[], cert, region, az}

activate CM
CM -> CM : Validate mTLS cert\nCheck duplicate node_id

CM -> CMF1 : AppendEntries\n{JOIN log entry}
CM -> CMF2 : AppendEntries\n{JOIN log entry}

activate CMF1
activate CMF2
CMF1 -> CMF1 : Persist to WAL (fsync)
CMF2 -> CMF2 : Persist to WAL (fsync)
CMF1 --> CM : AppendEntriesAck
CMF2 --> CM : AppendEntriesAck
deactivate CMF1
deactivate CMF2

note over CM : Quorum achieved (2/3)

CM -> CR : RegisterCapabilities\n{node_id, capabilities[]}
activate CR
CR --> CM : RegisterResponse{ok}
deactivate CR

note over CM : Capabilities registered BEFORE\npartition assignment (ADR-007)

CM -> KA : AssignPartitions\n{node_id, partition_count=2}
activate KA
KA --> CM : PartitionAssignment{partitions=[42,43]}
deactivate KA

CM -> R : SET cluster:member:W RUNNING
R --> CM : OK

CM --> W : JoinResponse\n{node_id, partitions=[42,43],\npeer_list[], term=8}
deactivate CM

W -> W : Kernel: JOINING → RUNNING
W -> W : Start Kafka consumers\n(partitions 42, 43)

@enduml
```

---

## SEQ-007

### Rolling Deployment (Drain Protocol)

```plantuml
@startuml SEQ-007-RollingDeploy

participant "Kubernetes" as K8S
participant "CM Leader" as CM
participant "Worker A (old)" as WA
participant "Kafka" as K
participant "Worker B (new)" as WB

K8S -> CM : DrainRequest\n{node_id=WA, reason=rolling_update}

activate CM
CM -> CM : Commit DRAINING to Raft log
CM -> WA : DrainNotification
deactivate CM

activate WA
WA -> K : consumer.pause()\n[stop accepting new tasks]
WA -> WA : [Complete in-flight steps]
WA -> WA : [in_flight_tasks drains to 0]
WA -> CM : LeaveRequest\n{node_id=WA}
deactivate WA

activate CM
CM -> CM : Commit LEFT to Raft log
CM -> K : TriggerRebalance\n[release WA's partitions]
CM --> K8S : DrainComplete
deactivate CM

K8S -> K8S : Replace WA pod with new image

WB -> WB : Kernel boot (new version)
WB -> CM : JoinRequest

note over K8S : PodDisruptionBudget enforces:\nminAvailable=3 workers at all times\nDrain waits if below minimum

CM -> K : AssignPartitions\n[previously held by WA → WB]
K -> K : Consumer group rebalance\n(partitions transfer to WB)

note over WB : New worker begins consuming\nfrom WA's former partitions

@enduml
```

---

## SEQ-008

### KEDA Autoscaling

```plantuml
@startuml SEQ-008-Autoscaling

participant "Kafka" as K
participant "KEDA Operator" as KEDA
participant "Kubernetes API" as K8S
participant "New Worker W" as W
participant "CM Leader" as CM

note over K : Consumer group lag grows:\naeos.tasks.normal lag = 3,200\n(threshold: 500 per partition → scale up)

KEDA -> K : Poll consumer group lag\n(every 15s)
K --> KEDA : lag=3200, partitions=200\n→ desired_replicas = ceil(3200/500) = 7

KEDA -> K8S : Scale(aeos-worker, replicas=7)
K8S -> W : Create pod(s) [from 3 → 7]

W -> W : Kernel boot
W -> CM : JoinRequest

activate CM
CM -> CM : Commit JOIN to Raft
CM -> K : Assign partitions to W
CM --> W : JoinResponse
deactivate CM

W -> K : Start consuming\n(new partitions)

note over K : Consumer lag decreasing...

loop lag < threshold for 5 consecutive polls
  KEDA -> K : Poll consumer group lag
  K --> KEDA : lag=0
end

note over KEDA : Scale down (stabilization window: 300s)

KEDA -> K8S : Scale(aeos-worker, replicas=3)\n[back to minReplicaCount]

K8S -> W : Drain pod (evict)
W -> CM : DrainRequest (SIGTERM handler)
CM -> K : Release partitions
K -> K : Rebalance to remaining workers

@enduml
```

---

## SEQ-009

### Governance Token Approval and Execution

```plantuml
@startuml SEQ-009-GovernanceApproval

participant "Client" as C
participant "API Gateway" as GW
participant "Policy Service" as PS
participant "Postgres" as DB
participant "Kafka" as K
participant "Worker" as W

C -> GW : POST /api/v1/tasks\n{task_type=research.web_search, payload, deadline}

GW -> PS : EvaluateRequest{task_type, submitter_id, deadline}

activate PS
PS -> DB : SELECT policy WHERE task_type=research.web_search\nORDER BY priority DESC
DB --> PS : [policy-research-web-001: APPROVE if\n submitter has role=researcher]

PS -> PS : Check: submitter_id has role=researcher → YES
PS -> PS : Calculate expiry:\nmax(deadline + 300, 3600) = 7200s

PS -> PS : Issue JWT token\n(HMAC-SHA256 signed)

PS -> DB : INSERT INTO audit_log\n{task_id, policy_id, decision=APPROVED, ...}
DB --> PS : OK

PS --> GW : EvaluateResponse\n{decision=APPROVED, token=JWT, expiry=+7200s}
deactivate PS

GW -> K : Produce(TaskMessage{task_id, token, payload})
GW --> C : HTTP 202 {task_id, status=QUEUED}

K --> W : TaskMessage

activate W
W -> W : JWT.verify(token, secret)\n→ valid signature
W -> W : Check token.expires_at > now → OK
W -> W : Check revocation_cache[token_id] → NOT_REVOKED
W -> W : Check token.task_type == step.task_type → OK
W -> W : PROCEED WITH EXECUTION
deactivate W

@enduml
```

---

## SEQ-010

### Governance Token Revocation

```plantuml
@startuml SEQ-010-TokenRevocation

participant "Admin" as ADM
participant "Admin API" as API
participant "Kafka" as K
participant "Worker A" as WA
participant "Worker B" as WB
participant "Redis" as R

ADM -> API : POST /admin/revoke\n{entity_id=api-key-xyz, reason=compromised}

API -> K : Produce(aeos.events.governance,\nRevocationEvent{\n  entity_id=api-key-xyz,\n  entity_type=API_KEY,\n  revoked_permissions=[*],\n  effective_at_ns=now\n})

note over K : Fan-out via per-worker consumer groups

K --> WA : RevocationEvent
K --> WB : RevocationEvent

activate WA
WA -> WA : Invalidate permission_cache[api-key-xyz]
WA -> R : GET in_flight_tasks WHERE token.entity_id=api-key-xyz
R --> WA : [task-789 is in-flight]
WA -> WA : Complete current step (step already authorized)
WA -> WA : After step: re-validate token\n→ token references revoked entity → INVALID
WA -> WA : Transition task-789: EXECUTING → CANCELLED
WA -> K : Produce(aeos.events.cluster,\nWorkflowCancelledEvent{task_id=task-789})
deactivate WA

activate WB
WB -> WB : Invalidate permission_cache[api-key-xyz]
note over WB : No in-flight tasks for api-key-xyz
deactivate WB

note over WA,WB : Total propagation time: < 1 second

@enduml
```

---

## SEQ-011

### mTLS Certificate Rotation

```plantuml
@startuml SEQ-011-CertRotation

participant "Vault" as V
participant "Vault Agent\n(sidecar)" as VA
participant "Worker Process" as W
participant "Other Services" as OS

note over VA : Certificate approaching expiry\n(12h before 24h TTL expires)

VA -> V : vault write pki/issue/aeos-internal\n  common_name=worker-042.aeos.svc\n  ttl=24h
activate V
V -> V : Generate key pair\nSign with Intermediate CA
V --> VA : {certificate, private_key,\nca_chain, lease_id, expiry}
deactivate V

VA -> VA : Write to shared volume:\n/var/run/secrets/tls/tls.crt\n/var/run/secrets/tls/tls.key

VA -> W : SIGHUP (or reload endpoint)

activate W
W -> W : Load new cert from shared volume
W -> W : TLS server: hot-swap cert\n(no connection drop for existing conns)
W -> W : TLS client: use new cert for new connections
note over W : Old cert remains valid\nuntil its original 24h expiry\n(overlap window)
deactivate W

W -> OS : New connection (uses new cert)
activate OS
OS -> OS : Verify cert against\nIntermediate CA certificate
OS --> W : Connection accepted
deactivate OS

note over V : Previous leaf cert lease expires\n(Vault auto-cleans)

@enduml
```

---

## SEQ-012

### Split-Brain Prevention via Execution Lease

```plantuml
@startuml SEQ-012-SplitBrain

participant "Worker A\n(partition 1)" as WA
participant "Worker B\n(partition 2)" as WB
participant "Redis Cluster" as R
participant "Kafka" as K

note over WA,WB : Network partition event:\nWorker A and Worker B both\nreceive the same task message\n(e.g., Kafka rebalance race condition)

WA -> K : Poll → receives Task{step:7}
WB -> K : Poll → receives Task{step:7}\n(duplicate delivery)

WA -> R : SETNX {wf:X}:step:7:lease WA EX 120
R --> WA : 1 (acquired — Worker A wins)

WB -> R : SETNX {wf:X}:step:7:lease WB EX 120
R --> WB : 0 (FAILED — lease already held by WA)

note over WB : Worker B MUST NOT execute step 7
WB -> WB : LOG: "lease held by WA; skipping step:7"
WB -> K : CommitOffset (consume without executing)

note over WA : Worker A proceeds normally
WA -> WA : Execute Step 7
WA -> R : Checkpoint(step:7, result)
WA -> K : Produce(Step8)
WA -> K : CommitOffset
WA -> R : DEL {wf:X}:step:7:lease

note over WA,WB : Step 7 executed exactly once\ndespite duplicate delivery

@enduml
```

---

*End of Sequence Diagrams — `018-SEQUENCE_DIAGRAMS.md`*
