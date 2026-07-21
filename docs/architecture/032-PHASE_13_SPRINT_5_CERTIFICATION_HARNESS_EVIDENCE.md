# Phase 13 Sprint 5 — Certification Harness & Operational Validation: Evidence

**Status:** Framework complete; dev-scale operational validation run. **No tier
certified** (correctly — see the gate).
**Date:** 2026-07-20
**Scope:** A reusable certification framework that measures REAL AEOS operations
against Bronze/Silver/Gold/Platinum thresholds, with a structural gate that
prevents any production certification claim from a non-production environment.
**Environment:** Single Windows developer workstation (Anaconda CPython 3.13,
28 logical CPUs, 16 GB RAM), all components in one process over loopback
`grpc.aio`. **Dev-box numbers; not an SLA.**

---

## 1. What this sprint delivers

The prior sprints produced *architecture evidence* (does it work?) and
*reliability evidence* (does it survive failure?). Sprint 5 builds the machinery
for *operational evidence* (how does it perform, and can that claim be trusted?)
— and, critically, builds it so that a good-looking number on a laptop can never
be mistaken for a production certification.

Deliverables:
- `app/certification/` — reusable framework (environment, profiles,
  measurements, runner, reports).
- `scripts/certify.py` — one CLI that runs identically on a laptop, a Linux
  server, and Kubernetes.
- Real dev-scale measurements for Bronze and Silver, emitted as structured JSON
  + human-readable markdown under `reports/certification/`.
- Regression tests protecting the honesty gate.

**Explicitly NOT delivered:** real Gold/Platinum certification. Those require
production infrastructure and are gated off on this host by design (§4).

---

## 2. The measurements are real (not simulated)

The repository already contained `app/testing/scale/certification.py`, whose
`_simulated_submit` draws latencies from `random.gauss(...)`. That is a
framework self-test, not evidence — a report built from it would be fabricated.
Sprint 5's harness does **not** use it. Every number comes from a wall-clock
observation of an actual operation:

| Dimension | Real seam driven | What is measured |
|-----------|------------------|------------------|
| throughput + latency | governance-gated `ScheduleTask` RPCs through a live `SchedulerServiceServicer` over loopback gRPC | per-call latency percentiles + aggregate TPS; real ES256 token verification on every call |
| failover | a real `RaftNode` cluster (Sprint 3 core) with a controllable transport | time from leader crash to a NEW *stable* leader, over N trials — real elections |
| recovery | the real `CheckpointEngine` over a durable `FileCheckpointStore` | cold read-back latency of committed checkpoints using a FRESH engine (models a new process resuming) |
| federation overhead | the real Sprint 4 A→B path | full round-trip dispatch → execute → poll → verify-evidence |

`app/certification/measurements.py` contains no synthetic distribution and no
`random`. Each function returns the raw samples so the report layer never
trusts a pre-aggregated number.

---

## 3. Profiles (Bronze / Silver / Gold / Platinum)

Each tier defines two scales for the identical measurement code:

- **full-scale** — the real target load a tier certifies at (e.g. Gold =
  1,000,000 tasks, 128-way concurrency). Only runs on production-grade infra
  with `--allow-full-scale`.
- **dev-scale** — a small, fast subset (e.g. 500–2,000 tasks) that exercises the
  same code paths on a workstation and yields real numbers.

Per-tier thresholds (throughput floor, P99 latency ceiling, failover/recovery
RTO, error-rate ceiling) are checked against whatever scale actually ran; the
report records which scale it was. See `app/certification/profiles.py`.

---

## 4. The honesty gate (the load-bearing property)

`certified == True` **iff** all four hold, evaluated in one place
(`runner.CertificationHarness.run_tier`):

1. the environment class is production-grade
   (`linux-server` / `kubernetes` / `cloud`), **and**
2. the run executed at **full scale**, **and**
3. `--allow-full-scale` was explicitly set, **and**
4. every applicable threshold was met.

On a developer workstation, (1) and (3) are false, so `certified` is **always
False** regardless of the numbers. The run still produces real measurements and
a `thresholds_met` boolean; the report labels it an *operational validation at
dev-scale*, never a certification. The environment classifier
(`environment.py`) errs toward the weakest claim and never promotes a Windows/
macOS desktop or CI runner to a production class.

This makes "no fabricated production claims" **structural**, not a promise:
- `test_scale_gate_never_selects_full_scale_off_production` — a dev host with
  `allow_full_scale=True` still runs dev-scale.
- `test_real_bronze_dev_scale_run_is_honest_and_measured` — a real Bronze run on
  a dev host is `certified=False` with an explicit "not production-grade"
  blocking reason, yet carries real, error-free measurements.

Reports carry a machine-readable `certified`, `certified_blocked_reasons`,
`thresholds_met`, and a plain-language `disclaimer`, so no downstream consumer
can confuse a dev-scale validation for a certification.

---

## 5. Dev-scale operational validation — observed numbers

Bronze and Silver, dev-scale, on the workstation above. **These are real but
load-sensitive** (single process, shared with the OS and whatever else the box
is doing); repeated runs vary materially. They are indicative of protocol/engine
overhead, **not** capacity claims.

| Tier (dev-scale) | Throughput (TPS) | Schedule P99 (ms) | Failover P99 (ms) | Recovery P50 (ms) | Federation P50 (ms) | Thresholds met? | Certified? |
|------------------|------------------|-------------------|-------------------|-------------------|---------------------|-----------------|------------|
| Bronze | ~67–177 (varies) | ~330–920 | ~110–265 | ~2 | ~11–33 | yes | **no** (dev-scale) |
| Silver | ~150 | ~330 | ~150 | ~2 | ~17 | **no** (< 200 TPS floor) | **no** (dev-scale) |

Reading these honestly:
- **Bronze thresholds are met** at dev-scale (throughput floor 50 TPS, P99 < 2 s,
  failover/recovery < 3 s) — but the run is **not** a Bronze certification,
  because the environment and scale gates fail. That is the intended outcome.
- **Silver is NOT met** at dev-scale: single-process loopback gRPC tops out
  around ~150 TPS here, below Silver's 200 TPS floor. The harness reports this
  plainly rather than inflating it — a working demonstration that the gate and
  thresholds report reality, not a desired result.
- Throughput variance between runs (67 vs 177 TPS for Bronze) is exactly why a
  workstation number cannot be a certification: it is dominated by local
  scheduling jitter, not by AEOS capacity.

---

## 6. Portability — same command, everywhere

`python scripts/certify.py <tier>` runs unchanged on a laptop, a Linux server,
or in Kubernetes. Only two things differ across environments, both automatic or
explicit:
- the **environment classification** (from local signals — k8s service account,
  container cgroup, cloud env markers, OS), which decides whether certification
  is even possible; and
- the **`--allow-full-scale`** opt-in, which a CI job on real infra would set to
  attempt a genuine certification (with `--require-certified` to gate the
  pipeline).

To produce real Gold/Platinum evidence later: run the identical command on
dedicated multi-node infrastructure with `--allow-full-scale`. No code change is
required — only a legitimate environment.

---

## 7. Honest gaps & limitations

- **No real certification exists yet.** Only dev-scale operational validations
  have been produced. Gold/Platinum full-scale numbers do not exist and are not
  claimed.
- **Single-process, loopback, one box.** No real network, TLS, NAT, multi-host
  scheduling, or resource contention across nodes. Failover uses the in-process
  Raft core (no cross-process transport); recovery is in-process cold-read (the
  cross-*process* death path is validated separately in
  `test_checkpoint_recovery.py` / doc 030). Federation is loopback (doc 031).
- **Echo workload.** Throughput measures the scheduler admission + governance
  path, not real task compute; the executor seam is an echo. Real workloads
  would add their own latency on top.
- **Platinum chaos not implemented here.** The Platinum profile defines a
  sustained-load + chaos target, but this sprint does not run a chaos harness;
  Platinum on a dev box runs the same dev-scale measurements as the others and
  is (correctly) never certified.
- **Resource metrics are best-effort.** Memory is read via `psutil` if present,
  else POSIX sysconf, else `null`; CPU utilization during a run is not yet
  captured.
- **Cosmetic Raft stderr noise.** Crashing a Raft leader leaves the crashed
  node's in-flight replication tasks to raise `ConnectionError` (unretrieved
  task exceptions). Benign; does not affect measurements. Same behavior noted in
  Sprint 3.
- **No fabricated numbers.** Every value in a report is a real measurement or a
  gate decision. The one pre-existing simulation module is deliberately unused.

---

## 8. Artifact index

- Framework: `app/certification/` — `environment.py` (classify + gate input),
  `profiles.py` (tiers, scales, thresholds), `measurements.py` (5 real
  measurements), `runner.py` (orchestration + honesty gate), `report.py`
  (JSON + markdown).
- CLI: `scripts/certify.py`.
- Tests: `tests/integration/distributed/test_certification_harness.py`
  (5 tests, all passing; protect the gate + real-measurement guarantees).
- Reports: `reports/certification/*.json` + `*.md` (dev-scale Bronze/Silver).
- Deliberately unused: `app/testing/scale/certification.py` (simulation
  self-test, retained but not a source of evidence).

---

## 9. Position in Phase 13

Sprint 5 closes the *tooling* gap for operational evidence: the framework to
produce throughput / latency / failover / recovery / federation reports now
exists and is honest by construction. It does **not** close the *evidence* gap —
real Gold/Platinum certification still requires production infrastructure and a
deliberate full-scale run. Per the directing plan, the next work is the
**Autonomous Research Organization** flagship demonstration; the certification
harness can be re-run on real infra whenever that infrastructure is available,
using the same command.
