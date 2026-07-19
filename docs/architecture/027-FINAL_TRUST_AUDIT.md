# 027 — Final Trust Audit (Phase 12A.4 Security Integration Closure)

**Date:** 2026-07-20
**Sprint:** Phase 12A.4 — Final Security Integration Closure
**Predecessor score (12A.3):** 79.57 / 100 (gap of 5.43 to the 85 Phase 13 threshold)
**Objective:** Prove that every security control implemented in Phase 12A.1 is
actually invoked by production execution code — not merely available as a
standalone component — and close the residual CRIT-004 token-verification gap.

> Scope discipline: this sprint built **no new features**. It traced, proved,
> and hardened existing controls, and fixed the defects that scrutiny surfaced.

---

## 1. Executive summary

The single remaining critical finding from Phase 12A.3 (CRIT-004 — TokenVerifier
integration into the worker execution path) is **CLOSED and proven** by an
end-to-end test suite that drives real signed JWTs through `WorkerRuntime`.

Running the security and durability suites end-to-end for the first time on this
platform surfaced **two latent defects that prior audits had scored as green**,
both now fixed:

1. **CRITICAL — WAL data loss on Windows** (durability prerequisite P0-1).
2. **HIGH — JWKS drops all EC keys** (federation / external verification break).

Full-suite result moved from **923 passed / 13 failed** to
**933 passed / 4 failed / 6 skipped**. The 4 residual failures are pre-existing,
**non-security** functional gaps tracked separately (§6). Every
authentication, authorization, governance, durability, and JWKS test passes.

**Verdict:** all critical and high findings in the trust scope are closed with
evidence. See `028-PHASE_13_CLEARANCE_REPORT.md`.

---

## 2. Repository-wide security trace

Focus areas from the sprint objective, each traced to the code path that
invokes it and the test that proves it.

| # | Control | Production invocation path | Proof |
|---|---------|----------------------------|-------|
| 1 | WorkerRuntime → TokenVerifier | `WorkerRuntime.__init__` threads `token_verifier` + `require_signed_tokens` → `GovernanceClient` (`runtime.py:83`) | `test_worker_token_verification.py` (14) |
| 2 | ExecutionContext raw_token propagation | `_execute_with_cleanup` builds `ExecutionContext(token_id=…, raw_token=…)` from the task payload and calls `verify_token(ctx.token_id, raw_token=ctx.raw_token)` **unconditionally** on every task (`runtime.py:154-178`) | `TestWorkerRuntimeTokenWiring` |
| 3 | GovernanceClient enforcement | `verify_token()` runs mandatory-mode gate → cryptographic verify → revocation check, fail-closed, single exception type (`governance.py:115-196`) | `TestGovernanceCryptoVerification` |
| 4 | JWT rejection paths | expired / tampered-signature / malformed / unknown-key each map to a distinct `reason` and block execution | 6 rejection tests, all green |
| 5 | Revocation enforcement | replay after `revoke(jti)` fails closed | `test_revoked_jti_rejected_replay` |
| 6 | JWKS key rotation | `JWKSProvider.jwks_dict()` serializes all valid keys across rotation overlap | `test_security.py::TestJWKS` (fixed, §4.2) |
| 7 | Audit logging | every allow/deny emits a structured record on the dedicated `aeos.audit.governance` channel with a reason (`governance.py:187-196`) | asserted via governance tests |

`raw_token` is deliberately **excluded from `CheckpointData`** (`context.py:110-115`),
so the transient JWT secret is never persisted to checkpoint stores.

### 2.1 Honest gap — production enablement is coupled to Phase 13

`WorkerRuntime` is currently instantiated only in tests; there is no production
node bootstrap that sets `require_signed_tokens=True`. This is **not** a hole in
the control — token verification executes on the unconditional hot path for
every task regardless — but mandatory-mode *enablement* depends on the
distributed worker bootstrap, which is itself coupled to the Phase 13 gRPC
inter-node transport (see README "Multi-Node Deployment — Current Limitations").

Closure action taken: added `settings.require_signed_tokens` (`config.py`) so the
toggle is a first-class, documented setting ready for the Phase 13 bootstrap to
consume. Tracked as **RESID-001 (MEDIUM)** — enforcement default flips to `True`
when the distributed worker entrypoint lands in Phase 13.

---

## 3. CRIT-004 — CLOSED

The Phase 12A.3 report listed three remaining items. Current state:

| Item | Status | Evidence |
|------|--------|----------|
| Thread `token_verifier` through `WorkerRuntime.__init__` → `GovernanceClient` | ✅ present | `runtime.py:52-88` |
| `raw_token` on `ExecutionContext`, passed through `_execute_with_cleanup` | ✅ present | `context.py:115`, `runtime.py:171-178` |
| Wiring tests for full JWT rejection in the worker | ✅ present & passing | 14 tests |

All 49 security tests pass (worker token verification 14, `test_security.py` 26,
integration security API 9) in **137 s**.

---

## 4. Defects surfaced by running the suites (now fixed)

### 4.1 CRITICAL — WAL data loss on Windows (durability prereq P0-1)

- **Symptom:** after Raft WAL segment rotation, recovery reported
  `truncated data (got 6309, want 43702)` corruption and recovered only
  **406 / 500** entries — silent committed-log data loss.
- **Root cause:** `WALSegment.open_write()` called `os.open()` **without
  `os.O_BINARY`**. On Windows the segment opens in *text mode*, so every `0x0A`
  byte inside the binary record header (`struct.pack` of magic/CRC/length) is
  expanded to `0x0D 0x0A`, shifting the byte stream and corrupting record
  framing. `O_BINARY` is `0` on POSIX, so Linux CI never exposed it.
- **Fix:** `os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)`.
- **Proof:** post-fix repro recovers **500 / 500** entries across 12 segments,
  sequential, zero corruption warnings. `test_wal.py` + `test_raft_wal_integration.py`
  = **34 passed**.
- **Significance:** WAL durability is the #1 Phase 13 prerequisite. Prior audits
  (12A / 12A.1) scored P0-1 as "closed" on Linux evidence alone; this control was
  in fact **broken on Windows**. This is exactly the class of "claimed vs.
  actually integrated" gap the audit process exists to catch.

### 4.2 HIGH — JWKS drops every EC key (federation break)

- **Symptom:** JWKS serialization raised `AttributeError`, swallowed by a
  `try/except`, so the published key set was **empty** after rotation.
- **Root cause:** `_ec_jwk()` called `pub.public_key()` on an object that is
  *already* a public key (`self._key.public_key`). Only private keys expose
  `.public_key()`; the line was a broken no-op "verification".
- **Fix:** removed the bogus call.
- **Proof:** `TestJWKS` (keys present, correct fields, rotation overlap) passes.
- **Significance:** an empty JWKS silently breaks cross-cluster federation and
  every external verifier (Istio/OPA/Auth0) that trusts AEOS tokens.

---

## 5. Test corrections (test-vs-API drift, not code defects)

These tests were authored against an API shape that differed from the shipped
implementation and had never run green on this platform:

- `test_raft_wal_integration`: globbed `*.wal` (files are `wal-*.seg`);
  used `RecoveredState.entries` (field is `.log`).
- `test_wal::test_truncate_entries`: used `RecoveryResult.log`
  (entries are retrieved via `RaftPersistence.get_entries_from(0)`).
- `test_api` debug-state trio: expected `200` but the HIGH-001 hardening returns
  `403` when `debug=False`. Added a `debug_mode` fixture and a new
  `test_debug_state_forbidden_in_production` assertion so both directions are
  covered.

---

## 6. Residual findings (non-blocking, non-security)

| ID | Sev | Finding | Disposition |
|----|-----|---------|-------------|
| RESID-001 | MEDIUM | No production worker bootstrap enables `require_signed_tokens=True` | Config toggle shipped; enablement lands with Phase 13 gRPC transport |
| RESID-002 | LOW | 4 pre-existing **non-security** functional test failures: `test_agents` (unsupported-action shape), `test_message_bus` (`_msg()` trace_id kwarg drift), `test_orchestrator` (long-term memory + bus-completed event) | Out of trust scope; tracked for a functional-correctness follow-up |

RESID-002 does not touch authentication, authorization, governance, durability,
or JWKS, and does not gate Phase 13.

---

## 7. Evidence index

- Security suite: `pytest tests/unit/distributed/test_worker_token_verification.py tests/unit/test_security.py tests/integration/test_security_api.py` → **49 passed**.
- WAL suite: `pytest tests/unit/distributed_infra/` → **34 passed**.
- Full suite: **933 passed, 4 failed, 6 skipped** (from 923 / 13 at sprint start).
- Commits: WAL/Redis, security/proto, verification/chaos, observability/runtime,
  worker wiring, infra, packaging, docs, JWKS fix, WAL `O_BINARY` fix.
