# Phase 13 Sprint 4 — Remote Federated Execution: Reliability & Trust Evidence

**Status:** Complete
**Date:** 2026-07-20
**Scope:** End-to-end cross-cluster task execution with a cryptographically
verifiable trust chain, plus the six required failure scenarios.
**Environment:** Single Windows developer workstation (Anaconda CPython 3.13,
grpcio 1.82). All clusters run in one process over loopback `grpc.aio` channels
on ephemeral ports. **These are dev-box numbers, not production SLAs.**

---

## 1. What this sprint proves

Sprint 3 proved the federation *trust boundary*: cluster A can dispatch to
cluster B, B admits into its own registry, and foreign/invalid tokens are
rejected fail-closed. It stopped at admission.

Sprint 4 closes the loop that makes federation a *capability* rather than a
handshake:

```
A --Handshake-------------->  B     (B mints a signed federation session token)
A --DispatchFederatedTask-->  B     (B ADMITS into its own scheduler + EXECUTES
                                     on its own worker-runtime seam)
A --GetFederatedTaskResult-> B     (B returns TaskResult + SIGNED execution
                                     evidence)
A verifies B's evidence against B's PUBLISHED JWKS, checks the result hash,
  the governance jti A minted, and both cluster identities — fail-closed.
```

The load-bearing property is the **trust asymmetry**: B signs execution
evidence with a private key A never holds; A verifies with B's *public* JWKS,
reconstructed from B's published key set (`verifier_from_jwks` →
`JWKSKeyStore`) with no shared secret and no private-key transfer. A cannot
forge B's evidence, and B cannot swap a result after signing without breaking
the SHA-256 hash binding.

---

## 2. Architecture of the trust chain

### 2.1 Executing side (cluster B) — `FederatedExecutor`
- `dispatch(task, originating_cluster_id) -> remote_task_id`
  - Idempotent per `task_id` (remote id = `f"{cluster_id}-{task_id}"`); a
    duplicate dispatch returns the existing id and does **not** start a second
    execution.
  - Admits the task into B's **own** `SchedulerServiceServicer` registry (real
    local admission), then runs it on the injected `execute_fn` worker-runtime
    seam via a background `asyncio` task.
- `_sign_evidence(state, result)` mints a JWT (`audience=federation-evidence`,
  a distinct audience from the `federation` session token so evidence can never
  be replayed as a session credential) binding:
  `{executing_cluster, originating_cluster, worker_id, status, result_hash,
  governance_jti}`.
  - `result_hash = sha256(TaskResult.SerializeToString(deterministic=True))` —
    both sides hash identical bytes via protobuf deterministic serialization.
  - `governance_jti` is the **echoed** jti of A's governance token, extracted
    WITHOUT verification (B does not trust it; A, which minted it, checks the
    echo).
- `get_result(remote_task_id)` serves the `TaskResult` + `ExecutionEvidence`;
  identical bytes on every poll (safe to redeliver).

### 2.2 Originating side (cluster A) — `FederationClient`
- `handshake` / `dispatch` / `await_result` (polls `GetFederatedTaskResult`
  until `ready`, raising `TimeoutError` past the deadline).
- `verify_evidence(...)` — fail-closed, in order:
  1. evidence-token **signature** against B's JWKS + issuer + audience;
  2. `result_hash` recomputed from the returned result bytes matches the
     evidence, **and** matches the signed `result_hash` claim;
  3. plaintext envelope fields agree with the signed claims (task_id,
     executing_cluster, governance_jti) — no tampering of the unsigned wrapper;
  4. governance jti equals the token A minted (B ran A's authorized task);
  5. executing/originating cluster identities match expectations.
  Any mismatch raises `FederationTrustError`.

### 2.3 The JWKS import path (the real gap closed this sprint)
Before Sprint 4, `RemoteJWKSClient` fetched a peer's JWKS but returned raw JWK
dicts — nothing reconstructed a usable key, so cross-cluster *verification* was
never actually wired. Sprint 4 added `_public_key_from_jwk` (EC via
`EllipticCurvePublicNumbers`, RSA via `RSAPublicNumbers`), `JWKSKeyStore`
(verify-only; holds no private keys and cannot sign), and `verifier_from_jwks`.
This is the concrete realization of the trust asymmetry.

---

## 3. Test evidence

**Suite:** `tests/integration/distributed/test_federation_remote_execution.py`
**Result:** `10 passed in 6.04s` (grpc + cryptography required).

| # | Test | Property proven |
|---|------|-----------------|
| 1 | `test_a_executes_on_b_and_verifies_signed_result` | Full loop: A dispatches, B executes on its own scheduler+worker seam, signs evidence, A verifies signature/hash/jti/identities; task lands in B's registry as SUCCEEDED |
| 2 | `test_tampered_evidence_signature_is_rejected` | Flipping a byte of the evidence token → `FederationTrustError` (signature) |
| 3 | `test_tampered_result_breaks_hash_binding` | Rewriting the result after signing → `FederationTrustError` (hash) |
| 4 | `test_wrong_governance_jti_is_rejected` | Verifying against a different authorization than B executed under → rejected |
| 5 | `test_evidence_verified_with_wrong_peer_key_is_rejected` | Evidence signed by B does not verify against a different cluster's JWKS (kid absent) |
| 6 | `test_remote_cluster_unavailable` | B down before dispatch → gRPC `UNAVAILABLE`/`DEADLINE_EXCEEDED` |
| 7 | `test_expired_session_token_denied` | Expired session token → `PERMISSION_DENIED`, and the task never entered B's registry (`NOT_FOUND`) |
| 8 | `test_federation_result_timeout` | Slow execution + short client timeout → `TimeoutError` |
| 9 | `test_network_partition_mid_flight` | B stopped while result still producing → gRPC `UNAVAILABLE`/`DEADLINE_EXCEEDED` |
| 10 | `test_duplicate_result_delivery_is_idempotent` | Same task dispatched twice → same remote id, executed once, byte-identical signed evidence both polls |

### 3.1 Required failure-scenario coverage (per the sprint directive)

| Required scenario | Covered by | Behavior (all fail-closed / non-silent) |
|-------------------|-----------|------------------------------------------|
| Remote cluster unavailable | #6 | gRPC error surfaced to caller |
| Invalid signature | #2, #5 | `FederationTrustError` |
| Expired token | #7 | `PERMISSION_DENIED`; no execution |
| Federation timeout | #8 | `TimeoutError` |
| Network partition mid-flight | #9 | gRPC error on poll |
| Duplicate result delivery | #10 | idempotent; identical evidence; executed once |

---

## 4. Measured federation overhead

Full A-side round trip — **dispatch → B executes (echo) → poll until ready →
verify_evidence** — over 20 sequential samples, warm channel, single process,
loopback:

| metric | value |
|--------|-------|
| min | 11.4 ms |
| median | 14.7 ms |
| mean | 18.3 ms |
| p95 | 67.7 ms |
| max | 67.7 ms |
| first call (incl. warmup) | 22.6 ms |

**Interpretation.** The federation protocol adds low-tens-of-milliseconds of
overhead on top of the workload itself, dominated by the client poll interval
(`poll_interval=0.02s` = 20ms) plus one JWKS signature verification and one
SHA-256 recompute. The p95/max outlier is a single sample and consistent with
GC / scheduler jitter on a loaded dev box, not a systematic cost. **This
measures protocol overhead only** (echo executor does no real work) and is a
loopback, single-process figure — it is **not** a cross-datacenter latency
estimate and carries no SLA.

---

## 5. Honest gaps & limitations

- **In-process, loopback, single box.** All clusters share one Python process
  and OS. No real network, TLS, NAT, or geographic latency is exercised. The
  overhead numbers are a floor, not a WAN estimate.
- **Polling result delivery.** A polls `GetFederatedTaskResult`; there is no
  push/stream/webhook. Median overhead is therefore bounded below by
  `poll_interval`. A streaming result path is future work.
- **Echo worker-runtime seam.** `make_echo_executor` stands in for a real
  `WorkerRuntime`. The federation envelope (admission, evidence signing,
  verification) is identical regardless of `execute_fn`, but this suite does
  **not** prove real workload execution semantics — only the federation path
  around it.
- **Orphaned background task on teardown.** When a cluster is stopped
  mid-execution (partition/timeout tests), B's in-flight `_run` asyncio task is
  not cancelled, producing a benign "Task was destroyed but it is pending"
  warning. It does not affect correctness or results but is a real cleanup gap;
  cancelling `state.task_ref` on executor shutdown is a follow-up.
- **No evidence-token revocation / expiry enforcement across the wire beyond
  the JWT `exp`.** Evidence carries a TTL (`evidence_ttl_seconds=3600`);
  long-term archival verification (after key rotation retires the signing kid)
  depends on JWKS rotation-overlap policy and is not exercised here.
- **Governance jti is echoed, not independently re-derived.** A trusts the jti
  *because A minted it and checks the echo*; a third party auditing the
  evidence in isolation would need A's mint record to bind the jti to an
  authorization. This is by design (A is the verifier), but worth stating.
- **Session token is a bearer capability** signed and verified by the same
  cluster (B). Its theft within its TTL would allow replay against B; this is
  the Sprint 3 trust model, unchanged.
- **No fabricated scale claims.** No throughput, node-count, or availability
  numbers are asserted. Only the 20-sample overhead above and the 10-test pass
  are measured facts.

---

## 6. Artifact index

- Protocol: `proto/aeos/federation/v1/federation.proto`
  (`ExecutionEvidence`, `GetFederatedTaskResultRequest`,
  `FederatedTaskResultResponse`, `GetFederatedTaskResult` RPC).
- Executing/originating logic:
  `app/distributed/grpc/services/federation_executor.py`
  (`FederatedExecutor`, `FederationClient`, `verify_evidence`,
  `FederationTrustError`, `make_echo_executor`, `extract_jti`).
- Service wiring: `app/distributed/grpc/services/federation_service.py`
  (optional `executor`; `GetFederatedTaskResult`).
- JWKS import / cross-cluster verify: `app/security/jwks.py`
  (`_public_key_from_jwk`, `JWKSKeyStore`, `verifier_from_jwks`).
- Tests: `tests/integration/distributed/test_federation_remote_execution.py`
  (10 tests, all passing).
- Overhead harness: ad-hoc 20-sample measurement (Section 4), not committed as
  a regression test (it is a benchmark, not an assertion).

---

## 7. Position in Phase 13

Sprint 4 delivers **Remote Federation Execution** — the third Category A
must-have — with a real, verifiable trust chain and honest failure-mode
coverage. It does **not** deliver production Linux certification or scale
validation; those remain explicitly out of scope and cannot be legitimately
generated from this environment. The next planned work is a certification /
benchmarking harness (run on appropriate infrastructure) and the Autonomous
Research Org flagship demo.
