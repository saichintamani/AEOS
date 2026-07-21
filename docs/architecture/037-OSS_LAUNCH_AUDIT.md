# 037 — Open Source Launch Audit (Phase 13, Sprint 10)

**Sprint goal:** Audit the repository as if an external engineer discovered it
today. Verify installation, quick start, tutorials, examples, CI, documentation,
architecture docs, and contribution flow. **Goal: a new engineer becomes
productive in under 30 minutes.**

**Date:** 2026-07-21
**Method:** I followed the paths a first-time visitor actually takes — read the
README top-to-bottom, ran the documented install/run commands, hit the
documented endpoints, opened the linked docs, and ran the examples. Findings
below are backed by real command output, not inspection alone.

---

## 0. Verdict

**A new engineer canNOT become productive in under 30 minutes by following the
primary README, and would likely lose trust in the project before getting a
request to succeed.** The reason is not missing capability — the app boots and
works — it is that **the top of the README documents a different, aspirational
system than the one that ships.** The endpoints, project structure, `.env`
variables, and half the doc links describe software that does not exist in the
repo.

There *is* a fast, real path to productivity (the RAG Quickstart + `examples/`),
but it is buried below ~200 lines of fiction and undercut by a blocking
startup download. Fixing the launch is mostly **deletion and reconciliation**,
not new engineering.

Launch readiness: **BLOCKED** — 3 blockers must clear before any public link.

---

## 1. Blockers (must fix before launch)

### B1 — No LICENSE file, but MIT is claimed everywhere. *(Legal)*
`README` badge, `README` "License" section, and `pyproject.toml` all assert MIT
and link `[LICENSE](LICENSE)`. **The file does not exist:**
```
$ ls LICENSE*
ls: cannot access 'LICENSE*': No such file or directory
```
Without a LICENSE file the code is, by default, **all-rights-reserved** — no one
may legally use it. This is the single hardest blocker for an OSS launch.
Fix: add the MIT `LICENSE` text the rest of the repo already promises.

### B2 — The README's primary API surface is fictional.
The "Available Endpoints" table and every "Example Requests" curl target paths
that **return 404** because they are not registered. Verified against
`app/main.py`:

| README claims | Reality |
|---|---|
| `POST /api/v1/agent/execute` | **does not exist** |
| `POST /api/v1/workflow/execute` | **does not exist** |
| `POST /api/v1/knowledge/ingest` / `query` | **does not exist** (it's `/api/v1/rag/ingest`, `/rag/query`, `/rag/answer`) |
| `POST /api/v1/software-intelligence/analyze` | **does not exist** (it's `/api/v1/github/analyze`) |
| `POST /api/v1/ml/predict` | **does not exist** (only `/ml/train`, `/ml/models`) |
| `GET /api/v1/governance/audit` / `costs` | **does not exist** |

Actual registered routes (ground truth):
```
/  /health  /metrics
/api/v1/run  /api/v1/execute
/api/v1/rag/{ingest,query,answer,upload}
/api/v1/github/analyze  /api/v1/ml/{train,models}
/api/v1/execution/{graph,introspect,metrics}
/api/v1/kernel/{health,introspect}  /api/v1/debug/state
/api/v1/{docs,redoc,openapi.json}
```
A new engineer's *first* action — copy the example curl — fails immediately. This
is the most damaging finding: it makes the whole README untrustworthy.

### B3 — Documented install does not yield a working `aeos` command; startup blocks on a network download.
Two compounding problems on the golden path:

1. The README install is `pip install -r requirements.txt`. But the `aeos` CLI
   entrypoint (`aeos.cli.main`) needs `typer` + `rich`, which are **only** in the
   `pyproject` dev extra, **not** in `requirements.txt`:
   ```
   $ python -m aeos.cli.main --help
   AEOS CLI requires typer and rich: pip install typer[all] rich
   ```
   So the README says `aeos start` (RAG Quickstart references `aeos start`) but
   the README's own install command never installs `aeos`. The two quick-start
   entry points (`uvicorn app.main:app` vs `aeos start`) require different
   installs and this is never stated.

2. Server startup performs a **blocking, network-first embedding-model load** in
   the lifespan. On a first boot (or any restricted network) this spends ~80 s
   in HuggingFace connection-reset retries before falling back to the cached
   model — during which the server is not ready:
   ```
   Retrying in 8s [Retry 5/5]  ... (x5, ~80s) ... Embedding model ready
   ```
   The RAG Quickstart advertises "runs fully offline with zero API keys," yet
   startup tries the network first and hangs. A new user sees an apparently-frozen
   server. (Root cause is eager model init on startup; a real fix would set
   `HF_HUB_OFFLINE`/local-files-only or lazy-load — flagged, not fixed, per the
   audit scope.)

---

## 2. High-severity issues (fix before launch, not strictly blocking)

### H1 — "Project Structure" section is fictional.
README lists `app/api/v1`, `app/memory`, `app/knowledge`, `app/workflows`,
`app/tools`, `app/reasoning`, `app/plugins`, `app/models`, `app/governance`,
`app/core/kernel`. **None exist.** Actual top-level `app/` packages: `agents`,
`api`, `certification`, `cloud`, `core`, `distributed`, `execution`,
`github_analyzer`, `kernel`, `ml`, `ml_pipeline`, `observability`, `open_source`,
`rag`, `runtime`, `runtime_intelligence`, `security`, `static`, `testing`,
`verification`. A contributor cannot navigate the codebase from this map.

### H2 — Documentation Index links to missing files.
Of the linked architecture docs, these **do not exist**:
`docs/architecture/001-ARCHITECTURE.md`, `010-KERNEL.md`,
`docs/adr/README.md`, `docs/ops/RUNBOOK.md`. (`000-VISION.md` and
`ARCHITECTURE_CONSTITUTION.md` do exist.) Every dead link erodes trust; several
are the "read this first" links.

### H3 — `.env.example` does not match the README's "Minimum required .env".
README says the minimum env is `OPENAI_API_KEY`, `LLM_PRIMARY_MODEL`,
`DATABASE_URL`, `CHROMA_PERSIST_DIRECTORY`, `REDIS_URL`, `SECRET_KEY`. The actual
`.env.example` contains **none** of those names; it uses `EMBEDDING_MODEL`,
`CHROMA_HOST`, `DEFAULT_AGENT`, `GITHUB_TOKEN`, etc. A user copying `.env.example`
and a user reading the README get two disjoint configuration models.

### H4 — "Current Status" / "Roadmap" contradict the rest of the repo.
The status table says Kernel is "Specified / implementation in progress",
Governance "Planned", Workflow engine "In Progress", Memory "In Progress" — but
the app actually boots a working Kernel ("AEOS Kernel ready"), registers 6
agents, and the memory/workflow subsystems are present. The roadmap tops out at
"Phase 8 Planned" while the repo is on **Phase 13**. New readers will
mis-estimate maturity in both directions.

### H5 — Placeholder identity everywhere.
`your-org/aeos` in clone URLs, `pyproject` URLs, and badges; `platform@aeos.io`
maintainer. No git remote is configured. These must become real before launch or
the clone command and every link is dead.

---

## 3. What actually works (keep and promote)

These were exercised and are genuinely good:

- **The app imports and boots cleanly.** `import app.main` succeeds; full
  lifespan reaches `Orchestrator ready` with agents
  `[simple, planner, research, reviewer, analyst, executor]`.
- **The RAG Quickstart is real** (offline extractive generation, cited answers,
  on-disk persistence). Once the embedding model is cached, it is the fastest
  honest path to "it works."
- **`examples/` 01–04 are real and correct** — they POST to the *actual*
  `/api/v1/run` endpoint (not the fictional table), compile via
  `aeos.workflow.compiler`, and degrade gracefully when the server is down
  ("Cannot reach ... Start AEOS: aeos start"). Plus the new
  `examples/autonomous_research_org/` (Sprint 9) runs offline end-to-end.
- **CONTRIBUTING.md is accurate and useful** — `pip install -e ".[dev]"`,
  `make test`, `make lint`, branch/PR flow, code standards. Notably its install
  command is the *correct* one (installs the `aeos` CLI), unlike the README's.
- **CI exists and is plausible** — `.github/workflows/`: `ci.yml`, `deploy.yml`,
  `infra-validate.yml`, `proto-governance.yml`.
- **Packaging is present** — `pyproject.toml` (with `[project.scripts] aeos=...`),
  `Makefile`, `CHANGELOG.md`, `pytest.ini`, extras (`rag,ml,distributed,...`).

---

## 4. Medium / low issues

- **M1 — No `examples/README.md`.** Four example dirs exist but nothing indexes
  or orders them for a newcomer.
- **M2 — Two competing "Quick Start" sections** (RAG Quickstart vs generic Quick
  Start) with no signposting of which to follow first. The accurate one (RAG) is
  second and shorter.
- **M3 — Architecture diagram describes layers** (Intent Understanding, Tool
  Runtime, Reasoning Runtime) that don't map cleanly to shipped packages;
  reconcile with the real module list.
- **L1 — README badge "Python 3.11+" vs `.env`/docs elsewhere referencing 3.13**;
  pick one supported floor and state it.
- **L2 — `docs/` has rich real content** (`architecture/000–036`, `runbooks`,
  `sre`, `standards`, `verification`) that the Documentation Index never surfaces
  — the index points at missing files while real docs go unlinked.

---

## 5. The honest 30-minute path (today, as-is)

If a determined engineer ignored the fictional sections, the *actual* shortest
path to productive is:

1. `pip install -e ".[dev]"`  (CONTRIBUTING's command, **not** the README's) — gets the `aeos` CLI + deps.
2. Pre-cache the embedding model **before** first run, or expect an ~80 s stall.
3. `aeos start` (or `uvicorn app.main:app`).
4. Use the **real** endpoints: `POST /api/v1/run`, `/api/v1/rag/answer`.
5. Run `examples/01_research_workflow` or `examples/autonomous_research_org --offline`.

This works, but it requires the reader to already know which parts of the README
to distrust — the opposite of a good first-run experience.

---

## 6. Prioritized launch checklist

| # | Action | Severity | Effort |
|---|--------|----------|--------|
| 1 | Add the `LICENSE` file (MIT) | Blocker | trivial |
| 2 | Replace README "Available Endpoints" + "Example Requests" with the real routes | Blocker | small |
| 3 | Make one install command yield a working `aeos`; state uvicorn-vs-CLI clearly; either add `typer`/`rich` to `requirements.txt` or point the README at `pip install -e .` | Blocker | small |
| 4 | Fix/skip the network-first model load so startup doesn't hang offline | Blocker | small |
| 5 | Rewrite "Project Structure" from the real `app/` tree | High | small |
| 6 | Fix Documentation Index — remove dead links, add the docs that exist | High | small |
| 7 | Reconcile `.env.example` with the README config section | High | small |
| 8 | Update Current Status / Roadmap to reflect Phase 13 reality | High | small |
| 9 | Replace `your-org` / `aeos.io` placeholders with the real repo/identity | High | trivial |
| 10 | Add `examples/README.md`; signpost a single canonical Quick Start | Medium | trivial |

**Bottom line:** the software is in far better shape than its front door. Almost
every blocker is a documentation-truth problem — reconciling the README with the
system that already works — plus one missing legal file and one startup hang.
None require new features. Until items 1–4 clear, the repo is **not ready** for a
public launch link.
