# 029 — Phase 13 Sprint 2: Genuinely Distributed Runtime (gRPC) — Architecture Evidence

**Phase:** 13, Sprint 2
**Status:** Delivered
**Predecessor:** [028-PHASE_13_CLEARANCE_REPORT](028-PHASE_13_CLEARANCE_REPORT.md)
**Scope of this document:** Evidence that AEOS task dispatch and fail-closed
governance now cross a **real gRPC socket and OS-process boundary**, not an
in-process abstraction — plus the measured price of that distribution.

---

## 1. What changed

Prior to this sprint, AEOS had a distributed *architecture* (publisher → router →
serializer → transport → consumer → worker runtime) that in practice ran entirely
on `InMemoryTransport` — a single-process dict of handlers. The distribution was
structural, not physical.

Sprint 2 closes that gap by making the load-bearing seam — the
`MessageTransport` — real over the wire:

| Component | Before | After |
|---|---|---|
| Transport | `InMemoryTransport` (in-proc dict) | `GrpcEventBusTransport` (`grpc.aio` server + peer stubs per node) |
| Worker consumption | `WorkerRuntime.start()` did **not** subscribe to the transport | `start()` subscribes `_on_task_accepted` to `TASK_ACCEPTED` and starts the consumer |
| Cross-process proof | none | 3 worker OS processes + a scheduler process, real sockets, ephemeral ports |

The single most important product fix: `WorkerRuntime.start()` now actually
consumes `TASK_ACCEPTED` from the injected transport. Before, the runtime was
only ever driven by direct in-test method calls; the real dispatched-event path
was never exercised. Making the transport real surfaced and closed that gap.

### 1.1 gRPC event-bus semantics

`GrpcEventBusTransport` (`app/distributed/transport/grpc_bus.py`) preserves the
exact `InMemoryTransport` contract so it is a drop-in:

- **`publish()`** dispatches to local subscribers first (no self-hop), then
  broadcasts a `DeliverEvent` RPC to every registered peer.
- **`DeliverEvent`** on the receiving node performs local dispatch only — it
  never re-publishes, so there is no broadcast storm / loop.
- **Local dispatch** mirrors in-memory semantics: fan-out across `group_id`s,
  round-robin (competing consumer) within a group.

This "broadcast to peers + node-local round-robin" model is correct for AEOS:
tasks are **addressed** (`assigned_worker_id`), so a broadcast task is ignored by
every worker except its addressee (`WorkerRuntime._on_task_accepted` filters by
node id), while governance events that are genuinely broadcast reach all nodes.

---

## 2. Evidence — tests that in-process tests cannot substitute for

Full distributed integration suite: **33 passed** (`tests/integration/distributed/`).
The load-bearing additions:

### 2.1 Real-socket transport — `test_grpc_transport.py` (5 tests)
Two/three transports in one process communicating **strictly over real gRPC
sockets** (ephemeral localhost ports): remote delivery, fan-out across groups,
local self-delivery, unsubscribe stops delivery, `Ping` liveness. Proves the wire
path publish → `DeliverEvent` RPC → remote local-dispatch.

### 2.2 Signed dispatch over the wire — `test_grpc_worker_dispatch.py` (2 tests)
Scheduler + production-bootstrapped worker on two peered gRPC transports:
- a **signed** task is delivered cross-transport and executes; its
  `TASK_COMPLETED` result flows back and is observed by the scheduler;
- an **unsigned** task is rejected **fail-closed** by governance
  (`metrics.failed_tasks == 1`) and never runs.

### 2.3 Cross-**process** cluster — `test_grpc_cluster.py` (2 tests)
The definitive proof. Three AEOS workers launched as **separate OS processes**
(`python -m app.distributed.testbed.grpc_worker_node`), the test process acting as
the scheduler:

1. **`test_three_process_cluster_dispatch_and_governance`** — three governance-
   signed tasks, one addressed to each worker process, all complete
   (`{t1,t2,t3}`); then an **unsigned** task addressed to a production worker
   process **never completes** — fail-closed governance holds across the process
   boundary.
2. **`test_worker_process_crash_leaves_cluster_serving`** — one worker process is
   hard-killed (`proc.kill()`); the two survivors still execute their addressed
   signed tasks, and a task addressed to the **dead** node never phantom-completes.
   Proves crash isolation and that `publish` tolerates a dead RPC peer
   (`asyncio.gather(..., return_exceptions=True)`).

> A subtle, real correctness note captured in these tests: `TASK_ACCEPTED` and
> `TASK_COMPLETED` share the `aeos.events.execution` topic, and the consumer
> dispatches **by topic**. Observers must therefore self-filter by
> `event_type` — the tests do, and the assertion history proves the worker (not a
> topic artifact) is what accepts or rejects each task.

---

## 3. Benchmark — the measured price of distribution

Runner: `scripts/benchmark_transport.py` (in-memory vs real gRPC, same workloads:
one-way throughput with a bounded in-flight window, and ping/pong round-trip
latency). Raw results: `benchmark_results/transport.json`.

**Representative run** (3,000 messages, 400 round trips, window 64):

| Transport | Throughput | RTT p50 | RTT p95 | RTT p99 |
|---|---:|---:|---:|---:|
| in-memory | 18,548 msg/s | 0.135 ms | 0.169 ms | 0.312 ms |
| gRPC (loopback) | 146 msg/s | 5.74 ms | 39.5 ms | 103.9 ms |

### 3.1 Honest interpretation

- **The delta is the point, not the absolute numbers.** localhost has no network
  latency, so the gap is pure protobuf framing + loopback socket + event-loop
  scheduling — a *lower bound on the shape* of real-network overhead, not a
  prediction of production latency.
- **These absolutes are a conservative dev-workstation floor.** On the measurement
  host (Windows + Anaconda CPython 3.13 + grpcio 1.82), raw `grpc.aio` per-call
  overhead is high and multiplexing is poor: an isolated `Ping` RTT p50 is ~9.5 ms
  and 640 concurrent `Ping`s reach only ~200 rpc/s. That ceiling is
  **environmental** (the async gRPC stack on this OS/build), confirmed independent
  of the AEOS wrapper. Linux production hosts routinely see sub-millisecond
  loopback and orders-of-magnitude higher throughput on the same code.
- **No SLA is claimed here.** The benchmark's job is to (a) prove the system runs
  over real sockets and (b) quantify the *relative* cost so operators understand
  the trade. Absolute throughput/latency SLAs belong to a Linux production-host
  benchmark run (tracked as a launch task), not this dev-box measurement.

Reproduce:
```bash
PYTHONPATH="$PWD" python scripts/benchmark_transport.py \
    --throughput-count 3000 --latency-count 400 --window 64 \
    --json benchmark_results/transport.json
```

---

## 4. Launch-readiness delta

| Dimension | Before Sprint 2 | After Sprint 2 |
|---|---|---|
| Distribution | architecture only (in-proc transport) | task dispatch + results cross real gRPC sockets |
| Process isolation | none proven | 3 worker processes + scheduler, cross-process E2E |
| Fail-closed governance | proven in-process | proven **across the process boundary** (signed executes, unsigned rejected) |
| Crash resilience | unproven | worker-process kill leaves cluster serving; no phantom completion |
| Perf visibility | none for transport | in-memory vs gRPC benchmark + raw JSON artifact |

This converts AEOS from "distributed architecture running on local abstractions"
to "distributed system with cross-process proof."

---

## 5. Known gaps (carried forward)

Honest accounting of what this sprint did **not** deliver, so the launch review
is not misled:

1. **Domain gRPC service facades not yet implemented.** The Sprint 2 directive
   named `SchedulerService`, `WorkerService`, `GovernanceService`,
   `FederationService`, `ObservabilityService`. The chosen strategy made the
   `MessageTransport` real first (the load-bearing seam that makes dispatch,
   governance, and results flow cross-process for free). The five domain-service
   facades over that transport remain to be built. Protos compile; servicers are
   the remaining work.
2. **Leader/Raft failover is not yet proven cross-process.** The crash test proves
   *worker* crash isolation. Cross-process Raft leader election/failover needs the
   coordination layer wired over the same gRPC bus — a follow-up.
3. **Checkpoint recovery cross-process** is covered in-process today; a
   cross-process recovery test is a follow-up once a worker can resume another's
   lease over the wire.
4. **Production-host benchmark** (Linux, TLS, real NICs) is required before any
   published throughput/latency SLA.

---

## 6. Artifact index

| Artifact | Path |
|---|---|
| gRPC event-bus transport | `app/distributed/transport/grpc_bus.py` |
| Runnable worker node (process) | `app/distributed/testbed/grpc_worker_node.py` |
| Production worker bootstrap | `app/distributed/worker/bootstrap.py` |
| Real-socket transport tests | `tests/integration/distributed/test_grpc_transport.py` |
| Signed-dispatch-over-gRPC tests | `tests/integration/distributed/test_grpc_worker_dispatch.py` |
| Cross-process cluster tests | `tests/integration/distributed/test_grpc_cluster.py` |
| Transport benchmark | `scripts/benchmark_transport.py` |
| Benchmark results | `benchmark_results/transport.json` |
