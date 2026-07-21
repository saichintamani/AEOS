# Autonomous Research Organization

AEOS operating as a **self-governing research organization**. A question is not
simply answered — it is *investigated, critiqued, revised, reviewed, and gated by
a governance board* before anything is published. Every decision along the way is
recorded in a **tamper-evident, hash-chained audit ledger**, so the full
provenance of a publication is traceable end to end.

## The organization

| Role | Responsibility |
|------|----------------|
| **planner** | Decomposes the question into a 3-5 item investigation plan |
| **researcher** | Gathers evidence for each plan item; later revises in response to critique |
| **critic** | Adversarially attacks the draft; assigns a `SEVERITY` |
| **reviewer** | Checks the revised draft for quality + policy; returns `VERDICT: APPROVE/REVISE` |
| **governance-board** | Final gate. Approves publication only if *all* explicit policies pass |

## The workflow

```
question
   -> plan        (planner)
   -> research    (researcher)
   -> critique    (critic)          \
   -> revise      (researcher)       }  bounded loop (--rounds, default 2)
   -> review      (reviewer)        /
   -> GOVERNANCE BOARD
        approve -> PUBLISH
        reject  -> back to critique/revise, or WITHHOLD if rounds exhausted
```

The governance board evaluates five inspectable policies (see `org.py`
`GovernanceBoard._default_policies`):

- `GOV-1` an investigation plan exists
- `GOV-2` evidence was gathered
- `GOV-3` a critique was performed and a revision followed it
- `GOV-4` critic severity is not `HIGH`
- `GOV-5` reviewer returned `VERDICT: APPROVE`

If any policy fails, publication is **withheld** and the reason is recorded.

## Traceability: the hash-chained ledger

Every stage appends a `LedgerEntry`. Entry *N* stores
`SHA-256(entry[N-1].hash + canonical(entry[N]))`. Because each entry commits to
the previous one, **altering any past decision breaks the chain** — the
`ledger.verify()` method re-derives every link and reports the exact `seq` where
tampering occurred. The final `chain_head` is a single fingerprint of the entire
decision history.

## Run it

**Offline (no server needed)** — deterministic stub that exercises the full
governance + audit machinery. Stub outputs are labelled `[OFFLINE-STUB]` and are
never presented as model reasoning:

```bash
python run.py --question "What are the trade-offs of Raft vs Paxos?" --offline
```

**Live** — routes each role to the real AEOS orchestrator (`POST /api/v1/run`):

```bash
aeos start                      # in another terminal
python run.py --question "What are the trade-offs of Raft vs Paxos?"
```

Requires `pip install httpx pyyaml`.

## Artifacts (written to `./out/`)

| File | Contents |
|------|----------|
| `publication.md` | The final publication, or a WITHHELD notice if rejected |
| `ledger.jsonl` | The complete hash-chained decision ledger, one JSON object per line |
| `decision_trace.json` | Provenance summary: governance result, chain head, integrity check |

## Design notes

- The engine (`org.py`) depends only on the public `aeos.workflow.compiler` plus
  optional `httpx`/`pyyaml` — it does not reach into internal `app.*` modules.
- The offline stub exists so the *governance + traceability* logic can be
  demonstrated and tested without a model backend. It performs **no reasoning**.
- See `docs/architecture/036-AUTONOMOUS_RESEARCH_ORG.md` for the full design and
  the honesty boundary between demonstrated mechanics and model-dependent quality.
