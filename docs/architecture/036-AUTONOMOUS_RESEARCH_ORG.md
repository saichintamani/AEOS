# 036 — Autonomous Research Organization (Phase 13, Sprint 9)

**Sprint goal:** Build a public demonstration where AEOS operates as a
self-governing research organization — planner, researcher, critic, reviewer, and
governance board — where a question flows through planning, evidence gathering,
critique, revision, governance approval, and final publication, and **every
decision is traceable**.

**Date:** 2026-07-20
**Deliverable:** `examples/autonomous_research_org/` (`workflow.yaml`, `org.py`,
`run.py`, `README.md`)

---

## 1. What was built

A five-role research organization that turns a question into a **governed,
fully-traceable publication**:

| Role | Responsibility |
|------|----------------|
| planner | Decomposes the question into an investigation plan |
| researcher | Gathers evidence; revises in response to critique |
| critic | Adversarially attacks the draft, assigns `SEVERITY` |
| reviewer | Quality + policy check, returns `VERDICT: APPROVE/REVISE` |
| governance-board | Final gate — approves publication only if all policies pass |

Workflow:

```
question -> plan -> research -> [critique -> revise -> review] (bounded loop)
         -> governance board -> approve->PUBLISH | reject->WITHHOLD
```

The role prompts live in `workflow.yaml` and are compiled by the existing public
`aeos.workflow.compiler.WorkflowCompiler` — the same DSL used by the other
shipped examples. The orchestration engine, ledger, and governance gate live in
`org.py`; `run.py` is the CLI.

---

## 2. Traceability — the core requirement ("every decision must be traceable")

Traceability is implemented as a **tamper-evident, hash-chained decision
ledger** (`org.py: DecisionLedger`), not merely a log file.

- Each stage appends a `LedgerEntry` capturing: `seq`, `stage`, `role`,
  `trace_id` (correlates to the AEOS run), `timestamp`, digests of the inputs the
  decision consumed, the output, a machine-readable `decision`, a `rationale`,
  and the `source` (`aeos-api` or `offline-stub`).
- Entry *N* is sealed with `SHA-256(entry[N-1].hash + canonical(entry[N]))`.
  Because every entry commits to its predecessor, **mutating any past decision
  invalidates the chain from that point forward**.
- `DecisionLedger.verify()` re-derives every link and returns the exact `seq`
  where the chain first breaks. `head()` is a single fingerprint of the entire
  decision history.

This makes the provenance of a publication auditable and integrity-checkable, not
just human-readable.

---

## 3. Governance is data, not hidden logic

The governance board (`org.py: GovernanceBoard`) evaluates five **inspectable**
policies, each a pure function of the assembled dossier:

| Policy | Rule |
|--------|------|
| GOV-1 | An investigation plan was produced |
| GOV-2 | Evidence/findings were gathered (>= 20 chars) |
| GOV-3 | A critique was performed and a revision followed it |
| GOV-4 | Critic severity is not `HIGH` |
| GOV-5 | Reviewer returned `VERDICT: APPROVE` |

Publication is approved only if **all** policies pass; otherwise it is withheld
and the failing policy IDs are recorded in the ledger and the (withheld)
publication. The policy set is a list of `GovernancePolicy` objects, so the basis
of every approval/rejection is explicit and auditable.

---

## 4. Execution evidence (real runs in this environment)

### 4.1 End-to-end offline run

```
$ python run.py --question "What are the trade-offs of Raft versus Paxos for consensus?" --offline

  [planner         ] plan       ->   238 chars (offline-stub, trace=offline-8c73)
  [researcher      ] research   ->   240 chars (offline-stub, trace=offline-dee5)
  [critic          ] critique   ->   253 chars (offline-stub, trace=offline-3a15)
  [researcher      ] revise     ->   251 chars (offline-stub, trace=offline-82fe)
  [reviewer        ] review     ->   270 chars (offline-stub, trace=offline-84a8)
  [governance-board ] governance -> APPROVED (round 1): All 5 governance policies passed.

 OUTCOME: PUBLISHED
 Decisions recorded: 6
 Audit chain integrity: VERIFIED
 Chain head: e3aa0c230ce1e3245ccccde8cd36044158a289334dc7e2e84381be8dae392113
```

Artifacts produced: `out/publication.md`, `out/ledger.jsonl`,
`out/decision_trace.json`.

### 4.2 Tamper-detection and rejection paths (verified)

```
clean chain verifies: True   broken: None
after mutating entry 1: verifies False   broken at seq: 1
rejected dossier (SEVERITY: HIGH + VERDICT: REVISE) approved?: False
    failed policies: ['GOV-4-critique-not-high', 'GOV-5-reviewer-approved']
compliant dossier approved?: True   policies passed: 5
```

This demonstrates: (a) the audit chain detects tampering at the exact position;
(b) the governance board genuinely blocks a non-compliant dossier and cites the
specific failed policies; (c) a compliant dossier passes all five.

---

## 5. Honesty boundary — what is and is NOT demonstrated

This is a governance-and-provenance demonstration. Being explicit about the
boundary:

- **Demonstrated (real, executed):** the organizational workflow, the
  bounded critique/revise loop, the governance gate with its five explicit
  policies, and the tamper-evident audit trail — all run and verified above.
- **Model-dependent (NOT claimed here):** the *substantive quality* of the
  research. In `--offline` mode every role output is a deterministic
  `[OFFLINE-STUB]` placeholder that performs **no reasoning**; it exists solely
  to exercise the governance + traceability machinery. Real research quality
  requires running in **live** mode against a configured AEOS orchestrator
  (`aeos start`), where each role calls `POST /api/v1/run`. The quality of those
  answers depends on the orchestrator's agent/model configuration and is not
  asserted by this example.

No output is fabricated or presented as model reasoning when it is not.

---

## 6. Files

```
examples/autonomous_research_org/
  workflow.yaml   role prompts + stage graph (compiled by aeos.workflow.compiler)
  org.py          DecisionLedger (hash chain), role executors (AEOS API / offline
                  stub), GovernanceBoard (explicit policies), ResearchOrganization
  run.py          CLI: --question, --host, --offline, --rounds
  README.md       usage + design summary
```

Dependencies: standard library + optional `httpx`/`pyyaml`. The engine imports
only the public `aeos.workflow.compiler`, not internal `app.*` modules.
