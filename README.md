# AEOS — AI Engineering Operating System

```
    ___   ___ ___  ____
   /   | / __/ _ \/ __/
  / /| |/ _// // /\ \
 /_/ |_/___/\___/___/
```

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)
[![Phase 13](https://img.shields.io/badge/Phase-13-purple?style=flat-square)]()

**Production-grade runtime for orchestrating and governing multi-agent AI systems.**

---

## What Is AEOS?

AEOS is a FastAPI-based runtime that dispatches work to a set of specialized
agents through a governed Kernel, with a self-contained RAG knowledge engine, an
ML training/registry pipeline, a GitHub code-analysis service, and an execution
engine that records a traceable graph of every run. It is designed to be
**self-hostable, offline-capable, and honest about what it does** — every route
documented below is registered in `app/main.py` and can be exercised with the
commands in this README.

What ships and works today:

- **Orchestrator + 6 agents** — `simple`, `planner`, `research`, `reviewer`,
  `analyst`, `executor`, dispatched via `POST /api/v1/run`.
- **RAG Knowledge Engine** — offline document ingestion, embedding, retrieval,
  and *cited* extractive answer generation with a browser UI (zero API keys).
- **ML Pipeline** — model training and a versioned on-disk registry
  (`/api/v1/ml/train`, `/api/v1/ml/models`).
- **GitHub Analyzer** — repository analysis (`/api/v1/github/analyze`).
- **Execution Engine + HyperKernel** — introspectable run graph, metrics, and
  kernel health (`/api/v1/execution/*`, `/api/v1/kernel/*`).
- **In-process distributed layer** — Raft consensus, leader election, and
  capability federation (see *Multi-Node Deployment* for the honest limits).
- **`aeos` CLI + Workflow DSL** — `aeos start`, `aeos workflow submit`, and a
  YAML workflow compiler (`aeos.workflow.compiler`) used by every example.

---

## Quick Start (under 5 minutes)

One install command, one start command, one request.

```bash
# 1. Clone
git clone https://github.com/your-org/aeos.git
cd aeos

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# 3. Install AEOS + the RAG extra (this installs the `aeos` CLI too)
pip install -e ".[rag]"

# 4. Start the server
aeos start                       # equivalent to: uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The API is now at `http://localhost:8000`; interactive docs at
`http://localhost:8000/api/v1/docs`.

> **First-boot note:** the RAG engine loads a ~90 MB embedding model. If it is
> already cached, startup is instant and fully offline. If not, it downloads
> once. AEOS tries the local cache first, so a cached model never blocks on the
> network.

Run your first workflow (single agent, no API keys required):

```bash
curl -s -X POST http://localhost:8000/api/v1/run \
  -H "Content-Type: application/json" \
  -d '{"task":"Summarize the trade-offs of RAG versus fine-tuning.","mode":"single-agent"}'
```

You get back a JSON run result (status, output, and a trace you can inspect at
`/api/v1/execution/graph`).

### Alternative install

If you only want to run the server (no editable install / no CLI extras),
`pip install -r requirements.txt` also works and now includes `typer`, `rich`,
and `PyYAML`, so the `aeos` command and the examples are available either way.
Then start with `uvicorn app.main:app --host 0.0.0.0 --port 8000`.

---

## RAG Quickstart — "Ask Your Documents"

The RAG Knowledge Engine is the most self-contained part of AEOS and ships with a
browser UI. It runs **fully offline with zero API keys** — ingestion, embedding,
retrieval, and grounded *cited* answer generation all work locally.

```bash
# With the server running (aeos start), open the demo UI:
#   -> http://localhost:8000/
# Drop a .txt/.md/.pdf/.html/.json file (or paste text), then ask a question.
# You get an answer with inline [1][2] citations back to the source chunks.
```

Or drive it from the API:

```bash
BASE=http://localhost:8000/api/v1/rag

# Ingest
curl -s -X POST $BASE/ingest -H "Content-Type: application/json" \
  -d '{"text":"AEOS is an AI Engineering Operating System with a RAG layer.","source":"notes","namespace":"demo"}'

# Ask — returns a grounded, cited answer (the "G" in RAG)
curl -s -X POST $BASE/answer -H "Content-Type: application/json" \
  -d '{"query":"What is AEOS?","namespace":"demo"}'

# Upload a file
curl -s -X POST $BASE/upload -F "file=@./README.md" -F "namespace=demo"
```

**Docker (one line):**

```bash
docker build -t aeos . && docker run --rm -p 8000:8000 aeos
```

### What makes this "real" RAG

- **Generation, not just retrieval.** `/rag/answer` synthesizes an answer whose
  every claim carries a `[n]` citation traceable to a retrieved chunk. The default
  `ExtractiveGenerator` is deterministic and offline — it can only repeat text that
  was actually retrieved, so it cannot hallucinate. Set `OPENAI_API_KEY` to switch
  to LLM synthesis (`OpenAIGenerator`) with a prompt-injection-resistant system prompt.
- **Persistence.** Ingested documents survive restarts (per-namespace on-disk store
  under `./data/rag/`, saved as `npz` + `json` — never pickle).
- **Hardened by default.** Every RAG input is validated and bounded: namespace
  allow-list (`^[A-Za-z0-9_-]{1,64}$`), path-traversal confinement on file ingest,
  upload size cap + type allow-list + filename sanitisation, bounded `top_k`,
  per-client rate limiting, optional `X-API-Key` auth (set the `API_KEY` env var),
  and generic error responses that never leak internals.

---

## Examples

Runnable, real examples live in [`examples/`](examples/). Each POSTs to the
**actual** `/api/v1/run` endpoint and degrades gracefully when the server is down.

| Example | What it shows |
|---|---|
| `examples/01_research_workflow/` | Single research workflow via the Workflow DSL |
| `examples/02_rag_workflow/` | RAG ingest + query pipeline |
| `examples/03_multi_agent_collaboration/` | Multiple agents coordinating on one task |
| `examples/04_enterprise_approval_workflow/` | Governance/approval gating |
| `examples/autonomous_research_org/` | Self-governing research org with a tamper-evident, hash-chained audit ledger (runs offline: `python run.py --question "..." --offline`) |

```bash
# With the server running:
python examples/01_research_workflow/run.py
```

---

## Available Endpoints

These are the routes actually registered in `app/main.py` (ground truth).

| Method | Path | Description |
|---|---|---|
| `GET`  | `/` | Static demo UI (RAG "Ask Your Documents") |
| `GET`  | `/health` | Health check with subsystem status |
| `GET`  | `/metrics` | Prometheus metrics |
| `POST` | `/api/v1/run` | Run a task through the orchestrator (single- or multi-agent) |
| `POST` | `/api/v1/execute` | Low-level HyperKernel execute |
| `POST` | `/api/v1/rag/ingest` | Ingest text into a namespace |
| `POST` | `/api/v1/rag/query` | Retrieve relevant chunks |
| `POST` | `/api/v1/rag/answer` | Grounded, cited answer generation |
| `POST` | `/api/v1/rag/upload` | Upload a file for ingestion |
| `POST` | `/api/v1/github/analyze` | Analyze a GitHub repository |
| `POST` | `/api/v1/ml/train` | Train a model (registered in the model registry) |
| `GET`  | `/api/v1/ml/models` | List registered models |
| `GET`  | `/api/v1/execution/graph` | Introspect the execution run graph |
| `GET`  | `/api/v1/execution/introspect` | Execution engine introspection |
| `GET`  | `/api/v1/execution/metrics` | Execution engine metrics |
| `GET`  | `/api/v1/kernel/health` | HyperKernel health |
| `GET`  | `/api/v1/kernel/introspect` | Kernel introspection (DEBUG only) |
| `GET`  | `/api/v1/debug/state` | Orchestrator + kernel state (DEBUG only) |
| `GET`  | `/api/v1/docs` · `/api/v1/redoc` · `/api/v1/openapi.json` | API documentation |

`/api/v1/run` request body:

```json
{ "task": "string (1-4000 chars)", "mode": "single-agent" }
```

`mode` is a lowercase/hyphenated string (default `single-agent`); use
`multi-agent` to engage the collaboration path.

---

## Configuration

Copy the template and edit as needed — the defaults run offline with no keys:

```bash
cp .env.example .env
```

The variables AEOS actually reads (see `app/core/config.py` and `.env.example`):

```env
# Identity / API
APP_NAME=AEOS
ENVIRONMENT=development          # development | staging | production
API_PREFIX=/api/v1
API_HOST=0.0.0.0
API_PORT=8000
DEBUG=true                       # set false in production (disables /debug + /kernel/introspect)

# Orchestrator
DEFAULT_AGENT=simple_agent
AGENT_TIMEOUT_SECONDS=60
MAX_RETRIES=3

# RAG engine (offline by default)
CHROMA_HOST=                     # empty = in-memory, no Docker needed
EMBEDDING_MODEL=all-MiniLM-L6-v2
RAG_TOP_K=5

# Optional
API_KEY=                         # if set, RAG routes require X-API-Key
GITHUB_TOKEN=                    # optional; analyzer works unauthenticated at 60 req/hr
ML_MODEL_REGISTRY_PATH=./data/model_registry
```

There are **no** required `OPENAI_API_KEY`, `DATABASE_URL`, or `SECRET_KEY`
variables — AEOS runs fully offline out of the box. Setting `OPENAI_API_KEY` is
*optional* and only switches RAG answer generation to LLM synthesis.

---

## Project Structure

Real top-level layout of the `app/` package:

```
aeos/
├── app/
│   ├── main.py                 # FastAPI app: lifespan, routes, guards
│   ├── core/                   # Config (Pydantic Settings), shared infra
│   ├── api/                    # Validation + supporting route modules
│   ├── agents/                 # SimpleAgent, PlannerAgent, ResearchAgent,
│   │                           #   ReviewerAgent, AnalystAgent, ExecutorAgent
│   ├── kernel/                 # HyperKernel: dispatch, policy, registry
│   ├── execution/              # Execution engine + run graph
│   ├── runtime/ runtime_intelligence/  # Adaptive scheduling, pattern mining
│   ├── rag/                    # RAG engine: embeddings, retrieval, generation
│   ├── ml/ ml_pipeline/        # Training + versioned model registry
│   ├── github_analyzer/        # GitHub repository analysis
│   ├── distributed/            # Raft, gRPC, coordination, federation (in-process)
│   ├── observability/          # Metrics / tracing / logging
│   ├── security/               # AuthN, token/JWT, guards
│   ├── certification/          # Certification harness
│   ├── cloud/                  # Cloud helpers
│   ├── verification/ testing/  # Invariants, protocols, test support
│   ├── open_source/            # OSS packaging helpers
│   └── static/                 # RAG demo UI
├── aeos/                       # Public package: cli/, sdk/, workflow/
├── examples/                   # Runnable examples (see Examples)
├── docs/                       # architecture/, runbooks/, sre/, standards/, verification/
├── infrastructure/             # helm/, kubernetes/, terraform/
├── pyproject.toml  requirements.txt  Makefile  .env.example
└── README.md
```

---

## Multi-Node Deployment — Current Limitations

AEOS ships a working in-process multi-node cluster (`MultiNodeCluster`) that
demonstrates Raft consensus, leader election, and capability federation with two
or more nodes inside a single Python process. Cross-process gRPC transport exists
(Phase 13 Sprints 2–4) and is exercised by the distributed test suite; full
cloud-scale, multi-container clustering is validated by the certification harness
but **not yet certified on a real managed Kubernetes cluster** (no plan/apply run
against a live AWS account — see `docs/architecture/033`–`035`).

Treat the multi-container docker-compose (`docker-compose.cluster.yml`) as a
reference topology, and consult the cloud validation playbook
(`docs/runbooks/CLOUD_VALIDATION_PLAYBOOK.md`) before a real deployment.

---

## Security Posture

A dedicated hardening pass covered the whole API surface:

- **Rate limiting** — tiered, configurable token-bucket limits on every endpoint
  (expensive / rag / default tiers), with backoff and `Retry-After` on 429s.
- **Input validation** — every endpoint uses a strict Pydantic schema
  (`extra="forbid"`, bounded lengths, format patterns). `POST /ml/train`'s
  `dataset_path` is confined to the datasets directory.
- **Secrets** — no hardcoded credentials; `.env` is gitignored.
- **Dependencies** — `python-multipart` ≥ 0.0.18 (CVE-2024-53981), `pypdf` ≥ 5.4.
- **Error handling** — clients get generic messages + a `trace_id`; full detail is
  logged server-side. No stack traces or paths leak.
- **File uploads** — validated by size, extension allow-list, and content
  (magic-byte sniffing); stored outside the web root, never executed.

**Still open:** `X-API-Key` gates the RAG routes; `/run` and `/execute` are
rate-limited but unauthenticated — put them behind your own gateway/authN in
production. `/debug/state` and `/kernel/introspect` return 403 when `DEBUG=false`
(the default).

---

## What AEOS Is NOT

| System | What it does | What AEOS does differently |
|---|---|---|
| **LangChain** | Composable chains of LLM calls; prompt templates; tool use | AEOS is a runtime with Kernel-mediated dispatch, governance, and observability — a platform, not a library. |
| **AutoGPT** | Autonomous goal-seeking loop via self-prompting | AEOS uses explicit workflows, typed state, and multi-agent coordination with a governance gate. |
| **CrewAI** | Multi-agent role-play with task handoffs | AEOS mediates through a Kernel + service registry; agents are typed and governed, not personas. |
| **Vertex AI Agents** | Google Cloud managed agent SaaS | AEOS is infrastructure you own, self-host, and extend; cloud-agnostic. |
| **Semantic Kernel** | SDK for embedding LLMs into apps | AEOS provides the orchestration runtime, not just SDK primitives. |

---

## Documentation Index

| Document | Path |
|---|---|
| Architecture Constitution | `ARCHITECTURE_CONSTITUTION.md` |
| Vision and Mission | `docs/architecture/000-VISION.md` |
| Kernel | `docs/architecture/005-KERNEL.md` |
| Execution Engine | `docs/architecture/006-EXECUTION_ENGINE.md` |
| Agent Runtime | `docs/architecture/007-AGENT_RUNTIME.md` |
| Reasoning Runtime | `docs/architecture/008-REASONING_RUNTIME.md` |
| Memory System | `docs/architecture/009-MEMORY_SYSTEM.md` |
| Architecture Decision Records | `docs/architecture/014-ARCHITECTURE_DECISION_RECORDS.md` |
| Phase 13 clearance & evidence | `docs/architecture/028`–`037` |
| Runbooks (DR, secret rotation, cloud validation) | `docs/runbooks/` |
| SRE & standards | `docs/sre/`, `docs/standards/` |

The `docs/` tree also contains `reports/`, `verification/`, and `adr/`.

---

## Current Status

AEOS is on **Phase 13** (distributed runtime + cloud-readiness + OSS launch).

| Area | Status |
|---|---|
| FastAPI app + orchestrator + 6 agents | Working |
| RAG engine (offline, cited generation, UI) | Working |
| ML pipeline + model registry | Working |
| GitHub analyzer | Working |
| Execution engine + HyperKernel introspection | Working |
| `aeos` CLI + Workflow DSL + SDK | Working |
| In-process Raft / federation + gRPC transport | Working (test-validated) |
| Helm / Terraform / K8s artifacts | Present; validated offline (lint/template/validate) |
| Real managed-K8s certification (plan/apply on live cloud) | **Not yet** — no live AWS run |

See `docs/architecture/037-OSS_LAUNCH_AUDIT.md` and
`038-OSS_LAUNCH_REMEDIATION.md` for the launch-readiness trail.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). In short:

```bash
pip install -e ".[dev]"
make test
make lint
```

Read the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md) before
significant changes.

---

## License

MIT License — see [LICENSE](LICENSE).

---

*AEOS is infrastructure, not magic. Every endpoint in this README is real and
registered in `app/main.py`; if something breaks, the observability layer will
help you find out why.*
