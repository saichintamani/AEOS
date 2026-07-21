# 030 — Phase 13 Sprint 3: Distributed Reliability Validation — Evidence

**Phase:** 13, Sprint 3
**Status:** Delivered
**Predecessor:** [029-PHASE_13_SPRINT_2_DISTRIBUTED_RUNTIME_EVIDENCE](029-PHASE_13_SPRINT_2_DISTRIBUTED_RUNTIME_EVIDENCE.md)
**Scope of this document:** Evidence that AEOS' *existing* distributed
architecture — Raft consensus, the two-phase checkpoint engine, the five domain
services, and cross-cluster federation — actually holds under the failure modes
that separate "has Raft / has checkpoints / has federation" from "these survive
real crashes, partitions, and trust boundaries." **No new product features were
built this sprint.** Every artifact below either drives an existing subsystem to
failure or gives it a real OS-process / real-socket boundary to cross.

---

## 1. What this sprint set out to prove

Sprint 2 made *task dispatch* physical (real gRPC sockets, real worker
processes). It explicitly deferred four things (029 §5): the five domain-service
facades, cross-process Raft failover, cross-process checkpoint recovery, and
two-cluster federation. Sprint 3 closes all four, plus the adversarial reliability
cases (quorum loss, partition, split-brain) that a "we have Raft" claim needs
before it can be trusted.

| Reliability property | Before Sprint 3 | After Sprint 3 |
|---|---|---|
| Domain services (Scheduler/Worker/Governance/Federation/Observability) | protos compiled; no servicers | all five implemented, driven by real client stubs over a real socket |
| Raft leader failover | unit-level role transitions | leader **crashed**; new leader elected in higher term, **RTO measured** |
| Quorum safety/liveness | asserted structurally | majority commits; **minority provably cannot commit** (safety) |
| Network partition / split-brain | not exercised | 5-node 3\|2 partition; minority blocked; **heals to one leader** (INV-CONS-001) |
| Checkpoint recovery | in-process only | **fresh OS process** resumes a dead process' workflow from last committed step |
| Federation | single-cluster handshake | **two independent clusters, separate trust roots**, cross-dispatch + fail-closed |

---

## 2. Evidence — tests an in-process / single-cluster test cannot substitute for

New Sprint 3 additions: **19 tests** (6 Raft reliability, 8 domain-service,
3 two-cluster federation, 2 cross-process checkpoint). Full
`tests/integration/distributed/` suite: **52 passed** (48 fast + 4 `slow`).

### 2.1 Raft under failure — `test_raft_reliability.py` (6 tests)

Driven by `RoutableCluster`, an in-process cluster whose transport links can be
**partitioned at runtime** and whose nodes can be **crashed and restarted**. This
is what provokes *real* elections and partitions deterministically (nodes keep
ticking during a partition) rather than mocking the outcome.

- **`test_leader_failover_on_leader_crash`** — the leader is crashed; a *stable*
  new leader (single leader whose term no follower exceeds) emerges in a strictly
  higher term. No two leaders in one term (`check_raft_single_leader == []`).
- **`test_quorum_liveness_with_one_follower_down`** — 2/3 alive: the leader still
  commits a proposal; log monotonicity holds.
- **`test_quorum_safety_no_majority_no_progress`** — 1/3 alive: the lone node
  spins elections but **never wins** and **`commit_index` stays -1**. Commit-safety
  under quorum loss.
- **`test_partition_minority_cannot_commit_majority_can`** — 5 nodes split 3\|2
  with the *old* leader stranded in the minority: the majority elects a new leader
  and commits; the minority leader's `commit_index` **does not advance** — the
  load-bearing safety assertion ("minority committed without quorum" would be a
  violation).
- **`test_split_brain_heals_to_single_leader`** — after the 3\|2 partition heals,
  the cluster **converges to exactly one leader** with no term collision.
- **`test_restarted_node_rejoins_under_current_leader`** — a crashed follower is
  restarted fresh and learns the current leader via heartbeat; leadership stays
  stable (availability preserved).

### 2.2 Five domain servicers over a real socket — `test_domain_services.py` (8 tests)

All five servicers hosted on one `grpc.aio` `DomainServiceServer`, driven by real
client stubs over an ephemeral localhost port with a shared ES256 keystore. The
load-bearing assertions are **fail-closed**:

- Governance: approve→signed JWT, verify→valid, **tampered token → `valid=False`
  (no exception)**, empty subject → denied with no token minted, audit recorded.
- Scheduler: signed task admitted; **unsigned task under `require_governance` →
  `PERMISSION_DENIED` and never enters the registry**; cancel works.
- Worker: register→heartbeat (with a scheduler-set DRAIN directive riding back)→
  deregister, reflected in the `WorkerPool`.
- Observability: span validation (missing trace/span id rejected) and a **live
  `WatchEvents` stream** receiving a submitted event.
- Federation: handshake mints a session token; dispatch **with** it lands in the
  local scheduler; dispatch **without** it → `PERMISSION_DENIED`.

### 2.3 Cross-process checkpoint recovery — `test_checkpoint_recovery.py` (2 tests, `slow`)

Two separate `python -m app.distributed.testbed.checkpoint_worker_node`
invocations share one durable `FileCheckpointStore` directory. The first is
**hard-killed** (`os._exit(137)`) mid-workflow; a second, independently-started
process takes over. Drives the **real** `CheckpointEngine` two-phase protocol.

- **`test_workflow_resumes_from_last_committed_checkpoint`** — process 1 commits
  steps 0–2 then crashes; process 2 (fresh interpreter) resumes `from_step=3` and
  finishes. The side-effect ledger is exactly `[0,1,2,3,4]` — **no committed step
  lost, none re-executed** across the process boundary. Recovery time measured.
- **`test_uncommitted_checkpoint_is_not_resumed`** — process 1 writes step 2's
  checkpoint (Phase 1) then crashes **before commit** (Phase 2). Process 2 resumes
  from step **2, not 3** — the uncommitted checkpoint is invisible to recovery
  (**INV-EXEC-002**). Step 2 appears in the ledger under *both* workers, proving it
  was correctly redone.

### 2.4 Two-cluster federation across separate trust roots — `test_federation_two_cluster.py` (3 tests)

Two standalone clusters, each its **own** `KeyStore` (separate signing keys, separate
trust roots), each a real `grpc.aio` server:

- **`test_cluster_a_runs_task_on_cluster_b`** — A handshakes B (B mints a token
  signed by **B's** key), dispatches a task presenting that token; the task really
  lands in **B's** scheduler registry (queried over B's wire) and **A's own
  scheduler never saw it** (`NOT_FOUND`).
- **`test_capabilities_exchange_requires_session`** — capabilities exchange works
  with a session token, `PERMISSION_DENIED` without.
- **`test_foreign_token_is_rejected_cross_cluster`** — the trust boundary is **real
  and fail-closed**: a token A mints with **A's own** signer is **rejected by B**
  (B honours only tokens it issued), and nothing lands in B's scheduler.

---

## 3. Measured reliability metrics

All measurements on the dev workstation (Windows + Anaconda CPython 3.13,
`grpcio` 1.82). Raft timers are the fast test profile
(`heartbeat=20ms, election=60–120ms`).

| Metric | Measurement | Method |
|---|---:|---|
| **Failover time (RTO)** — leader crash → new stable leader | **77–95 ms** (median ~93 ms, n=5) | `RoutableCluster`: crash leader, wait for `stable_leader() != old` |
| **Recovery time** — dead process → fresh process resumes & completes | **0.63–0.78 s** (n=3) | spawn worker, crash after commit 2, time a 2nd `python -m` resume-to-completion |
| **Failover success rate** | **100%** (6/6 Raft reliability tests, every trial) | test assertions + measurement harness |
| **Recovery success rate** | **100%** (committed prefix never lost/duplicated; uncommitted never resumed) | ledger equality `[0,1,2,3,4]` + INV-EXEC-002 |
| **Quorum behavior** | majority (2/3, 3/5) commits; minority (1/3, 2/5) **never** commits | quorum liveness + safety + partition tests |
| **Distributed suite** | **52 passed** (48 fast, 4 slow), 0 failed | `pytest tests/integration/distributed/` |

### 3.1 Honest interpretation of the numbers

- **The RTO is a floor shape, not a production SLA.** 77–95 ms is in-process Raft
  with deliberately fast election timers on loopback. It proves failover happens
  *quickly and deterministically*; real-network RTO is dominated by the election
  timeout you configure (production defaults are 150–300 ms+) plus real RTT, not
  by this code path. The **relative** result — failover ≈ one-to-two election
  timeouts, no split-brain — is what transfers.
- **Recovery time is dominated by interpreter cold-start**, not recovery logic.
  ~0.6–0.8 s for a fresh `python -m` process is mostly import cost; the actual
  "read last committed checkpoint and resume" is a single store scan. The
  guarantee that matters is **exactly-once over the committed prefix**, which is
  binary and holds, not the wall-clock number.
- **Quorum behavior is asserted as a safety property, not a benchmark.** The
  strong result is negative: with no majority, `commit_index` provably does **not**
  advance. That is the property that makes the consensus layer trustworthy.

---

## 4. Known gaps (carried forward — not hidden)

Honest accounting so the launch review is not misled. These are properties the
Sprint 3 tests **deliberately do not claim**, matching limitations of the current
Raft core (documented in `test_raft_reliability.py` and `raft.py`):

1. **No pre-vote.** A reconnecting partitioned candidate bumps the term and forces
   a re-election on heal. The split-brain test asserts *eventual* convergence to one
   leader, not disruption-free heal. Pre-vote / leader-lease is a future hardening.
2. **No `next_index` backoff loop.** A node restarted with an empty log is not
   guaranteed to re-sync a divergent suffix. `test_restarted_node_rejoins…`
   therefore asserts re-integration of **leadership/availability**, not full log
   convergence on the restarted node.
3. **Raft runs in-process (asyncio, transport-injected), not yet over the gRPC
   bus.** The failure injection is real (partitionable transport, crash/restart),
   but the nodes share one process. Cross-**process** Raft over the Sprint 2 gRPC
   bus remains the next step; this sprint proves the *algorithm* survives the
   failure modes, Sprint 2 proved the *transport* is physical.
4. **Checkpoint durability uses `FileCheckpointStore` (test scaffolding), not
   `RedisCheckpointStore`.** This is intentional: it exercises the *unchanged*
   production `CheckpointEngine` two-phase protocol across a real process death
   without standing up Redis. Production durability is Redis-backed; the store
   adapter mirrors its committed/uncommitted semantics verbatim so the engine
   cannot tell them apart.
5. **Federation `dispatch_fn` routes into the local scheduler registry**; it does
   not yet drive a remote worker to *execute* the federated task end-to-end. The
   trust boundary, admission, and fail-closed rejection are proven; remote
   execution of a federated task is a follow-up.
6. **All numbers are dev-workstation measurements.** A Linux production-host run
   (real NICs, TLS, production timers) is required before any published RTO/latency
   SLA — same caveat as 029 §3.1.

---

## 5. What this converts

| Dimension | After Sprint 2 | After Sprint 3 |
|---|---|---|
| Consensus | "has Raft" | Raft survives leader crash, quorum loss, partition, and split-brain — with measured RTO and no term collision |
| Domain services | protos only | all five servicers real over a socket, fail-closed on every trust check |
| Recovery | in-process checkpoints | a fresh OS process resumes a dead one exactly-once over the committed prefix |
| Federation | single cluster | two clusters, separate trust roots, cross-dispatch that is fail-closed against foreign tokens |

Sprint 2 made distribution **physical**. Sprint 3 makes it **reliable under
failure** — the difference between "distributed system with cross-process proof"
and "distributed system whose failure modes are tested and whose limits are
documented."

---

## 6. Artifact index

| Artifact | Path |
|---|---|
| Five domain servicers | `app/distributed/grpc/services/` (`governance_service.py`, `scheduler_service.py`, `worker_service.py`, `observability_service.py`, `federation_service.py`, `server.py`) |
| Raft reliability tests | `tests/integration/distributed/test_raft_reliability.py` |
| Domain-service tests | `tests/integration/distributed/test_domain_services.py` |
| Two-cluster federation tests | `tests/integration/distributed/test_federation_two_cluster.py` |
| Cross-process checkpoint tests | `tests/integration/distributed/test_checkpoint_recovery.py` |
| Durable file checkpoint store (scaffolding) | `app/distributed/testbed/file_checkpoint_store.py` |
| Runnable checkpoint worker node | `app/distributed/testbed/checkpoint_worker_node.py` |
| Raft core (under test) | `app/distributed/consensus/raft.py` |
| Checkpoint engine (under test) | `app/distributed/execution/checkpoint.py` |
