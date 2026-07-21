# 038 — Open Source Launch Remediation (Phase 13, Sprint 11)

**Sprint goal:** Clear the launch blockers found in the Sprint 10 audit
(`037-OSS_LAUNCH_AUDIT.md`) so that a first-time engineer can install AEOS, start
it, run a workflow, and get a result in **under 30 minutes without consulting
source code.**

**Date:** 2026-07-21
**Method:** Every fix below was verified by real execution — including a full
**clean-room run** in a fresh virtual environment following only the README. The
clean-room run itself surfaced three additional blockers that inspection had
missed; those were fixed and re-verified.

---

## 0. Outcome

**Launch blockers cleared. The documented golden path now works end to end.**

Verified golden path (fresh venv, README only):

```
pip install -e ".[rag]"                                 # installs the aeos CLI + RAG deps
aeos start                                              # or: uvicorn app.main:app
curl -X POST /api/v1/run  -d '{"task":"...","mode":"single-agent"}'   # -> 200 success
```

Real result from the clean-room server:

```json
{"task":"Summarize the trade-offs of RAG versus fine-tuning.",
 "agent":"simple_agent","status":"success",
 "result":{"summary":"Analyzed task: ...","domain":"research","confidence":0.75,
           "recommendation":"Single-agent execution sufficient."},
 "metadata":{"latency_ms":153.45,"trace_id":"f0bae5b4-...","...":"..."}}
[http 200 in 0.18s]
```

Time budget (Anaconda CPython 3.13, Windows 11, warm HF cache): install ~21 min
(dominated by torch/CUDA-less wheels), first server boot 48 s, warm boot 13–22 s,
first `/run` 0.18 s, RAG cited answer 0.17 ms. **Total well under 30 minutes.**

---

## 1. The eight objectives — status

| # | Objective | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Add canonical MIT LICENSE | DONE | `LICENSE` (MIT) created; `pyproject` license + classifier changed Apache-2.0 → MIT to match |
| 2 | Rewrite README to real API surface | DONE | README endpoints/table now generated from `app/main.py` ground truth |
| 3 | Remove all fictional endpoints | DONE | `/agent/execute`, `/workflow/execute`, `/knowledge/*`, `/software-intelligence/*`, `/ml/predict`, `/governance/*` removed; real routes listed |
| 4 | Verify every README command executes | DONE | All README commands run in the clean-room venv (below); found + fixed 3 blockers |
| 5 | Align `.env.example` with runtime | DONE (already aligned) | `.env.example` already matches `app/core/config.py`; README env section rewritten to match both (removed fictional `OPENAI_API_KEY`/`DATABASE_URL`/`SECRET_KEY` as *required*) |
| 6 | Documented install yields the `aeos` CLI | DONE | `pip install -e ".[rag]"` installs `aeos`; `aeos --help`/`version` verified. `requirements.txt` also gained `typer`/`rich`/`PyYAML` so that path works too |
| 7 | Eliminate startup-time blocking model download | DONE | `app/rag/embeddings.py` now tries `SentenceTransformer(name, local_files_only=True)` first, falling back to a one-time download only if uncached — kills the ~80 s HF retry storm on the cached path |
| 8 | Clean-room onboarding test | DONE | Fresh venv, README only, reached a successful `/api/v1/run` and a 4-stage example workflow (below) |

Produced: this document (`038-OSS_LAUNCH_REMEDIATION.md`).

---

## 2. Blockers from doc 037 — how each was cleared

### B1 — No LICENSE file (legal blocker). **CLEARED.**
Added the MIT `LICENSE` text the repo already promised. Also removed the
**contradiction inspection missed**: `pyproject.toml` had declared
`license = "Apache-2.0"` and the Apache classifier while the README badge/section
claimed MIT. Standardized on MIT everywhere (per the sprint's "canonical MIT"
directive).

### B2 — Fictional README API surface. **CLEARED.**
The "Available Endpoints" table and every example now use only routes registered
in `app/main.py`: `/`, `/health`, `/metrics`, `/api/v1/run`, `/api/v1/execute`,
`/api/v1/rag/{ingest,query,answer,upload}`, `/api/v1/github/analyze`,
`/api/v1/ml/{train,models}`, `/api/v1/execution/{graph,introspect,metrics}`,
`/api/v1/kernel/{health,introspect}`, `/api/v1/debug/state`,
`/api/v1/{docs,redoc,openapi.json}`. The first example a reader copies now
returns `200`, not `404`.

### B3 — Broken install + 80 s startup hang. **CLEARED (two parts).**
- **Install:** README now leads with `pip install -e ".[rag]"`, which installs
  the `aeos` CLI (`typer`+`rich` are core deps). `requirements.txt` also gained
  `typer[all]`, `rich`, and `PyYAML`, so `pip install -r requirements.txt` +
  `uvicorn app.main:app` is a valid second path. Both are stated explicitly.
- **Startup hang:** `embeddings.py` offline-first load (objective 7). Cold boot
  in the clean-room venv reached ready in **48 s** (model load + full app init),
  warm boot **13–22 s** — no 80 s network retry storm.

---

## 3. Blockers the clean-room run *itself* found (inspection had missed)

Running the README verbatim in a fresh venv exposed three genuine install/boot
blockers that a by-inspection audit could not have caught. All three were fixed
and re-verified.

### C-A — `pip install -e .` failed: invalid build backend. **FIXED.**
```
pip._vendor.pyproject_hooks._impl.BackendUnavailable:
Cannot import 'setuptools.backends.legacy'
```
`pyproject.toml` declared `build-backend = "setuptools.backends.legacy:build"`,
which is not a real backend — **`pip install -e .` could never have worked.**
Changed to `build-backend = "setuptools.build_meta"`. Re-ran: editable wheel
built, `Successfully installed aeos-0.1.0`.

### C-B — Server crashed on boot: `.[rag]` extra missing `python-multipart`. **FIXED.**
```
RuntimeError: Form data requires "python-multipart" to be installed.
```
The `/api/v1/rag/upload` route uses `Form`/`File`, so FastAPI requires
`python-multipart` at route-registration time — but the `rag` extra in
`pyproject.toml` omitted it (and `beautifulsoup4`, used by the HTML loader). They
were present in `requirements.txt` but not the extra, so the *documented*
`pip install -e ".[rag]"` produced a server that died on startup. Added both to
the `rag` extra. Re-ran: server reached "AEOS ready to serve".

### C-C — `aeos start` broke on Windows paths with spaces. **FIXED.**
```
D:\My: can't open file 'D:\My projects\AEOS\projects\AEOS\.venv...\python.exe'
```
`cmd_start` used `os.execv(sys.executable, cmd)`. On Windows, `os.execv`
re-parses the command line and splits on spaces, mangling an interpreter path
like `D:\My projects\...`. Replaced with `subprocess.run(cmd)` +
`SystemExit(returncode)` (list-args are quoted correctly on every platform).
Re-ran: `aeos start` reached ready in 22 s and served `/api/v1/run` → `200`.

---

## 4. Clean-room transcript (abridged, real output)

Fresh `.venv-cleanroom`, following README only:

```
$ python -m venv .venv-cleanroom && source .venv-cleanroom/Scripts/activate
$ pip install -e ".[rag]"
  ... Successfully installed aeos-0.1.0 sentence-transformers-5.6.0 chromadb-1.5.9
      fastapi-0.139.2 uvicorn-0.51.0 typer-0.27.0 rich-15.0.0 python-multipart ...
$ aeos version
  AEOS v0.1.0
$ aeos start --host 127.0.0.1 --port 8141
  ... "message": "AEOS ready to serve", "kernel": "running"
  READY after 22s

$ curl -X POST /api/v1/run -d '{"task":"...","mode":"single-agent"}'
  {"status":"success", ... ,"latency_ms":153.45}          [http 200]

$ curl -X POST /api/v1/rag/ingest -d '{"text":"...","namespace":"demo"}'
  {"status":"success","chunks_added":1,"namespace":"demo"} [200]
$ curl -X POST /api/v1/rag/answer -d '{"query":"What is AEOS?","namespace":"demo"}'
  {"status":"success","answer":"AEOS is an AI Engineering Operating System
   with a RAG layer. [1]","citations":[{"marker":1,"score":0.6535,...}]} [200]

$ python examples/01_research_workflow/run.py
  Server: healthy (v0.1.0)
  [1/4] DECOMPOSE  planner   success
  [2/4] RESEARCH   auto-route success
  [3/4] ANALYSE    analyst   success
  [4/4] REVIEW     reviewer  success
  Research complete.                                       exit=0
```

Every command a first-time reader runs now succeeds.

---

## 5. Files changed

| File | Change |
|------|--------|
| `LICENSE` | **New** — canonical MIT text |
| `pyproject.toml` | license Apache-2.0 → MIT (+ classifier); **build-backend fixed** (`legacy` → `build_meta`); `rag` extra += `python-multipart`, `beautifulsoup4` |
| `requirements.txt` | += `typer[all]`, `rich`, `PyYAML` (so `-r requirements.txt` yields a working `aeos` CLI + examples) |
| `app/rag/embeddings.py` | Offline-first model load (`local_files_only=True` then fallback) |
| `aeos/cli/main.py` | `aeos start`: `os.execv` → `subprocess.run` (Windows spaced-path fix) |
| `README.md` | Full rewrite: real endpoints, real project structure, real config, real docs index, single canonical Quick Start, Phase 13 status |

No product features were added. Every change is truth-reconciliation, one legal
file, one dependency-metadata fix, and two small runtime bug fixes uncovered by
actually running the documented path.

---

## 6. Honesty boundary — what is NOT claimed

- **Install time (~21 min)** is dominated by downloading large ML wheels (torch,
  chromadb, transformers) on this machine/network. On a cached pip or a
  wheels-mirror it is far faster; on a slow link it could exceed the 30-min
  budget. The *interactive* time (boot + run) is seconds.
- The 30-minute success was measured with a **warm HuggingFace model cache**. A
  truly cold machine downloads the ~90 MB embedding model once (still one-time,
  no longer a retry storm thanks to objective 7).
- Remaining doc-037 items **not** in this sprint's blocker scope (H5 placeholder
  `your-org` identity, no configured git remote) are unchanged — they require a
  real hosting decision, not a code fix. The README still uses `your-org` in the
  clone URL and must be updated at the moment a real repo is published.
- Minor cosmetic: the `aeos --help` banner prints a `?` for an em-dash under the
  Windows cp1252 console. Cosmetic only; not a functional blocker.
```
