# AEOS — AI Engineering Operating System

```
    ___   ___ ___  ____
   /   | / __/ _ \/ __/
  / /| |/ _// // /\ \
 /_/ |_/___/\___/___/
```

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?style=flat-square&logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104%2B-009688?style=flat-square&logo=fastapi)](https://fastapi.tiangolo.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow?style=flat-square)](LICENSE)
[![Status: Active Development](https://img.shields.io/badge/Status-Active%20Development-orange?style=flat-square)]()
[![Architecture: v2](https://img.shields.io/badge/Architecture-v2-purple?style=flat-square)]()

**AI Engineering Operating System — Production-grade infrastructure for autonomous AI systems**

---

## What Is AEOS?

AEOS (AI Engineering Operating System) is a **production-grade platform** that provides the runtime infrastructure for deploying, operating, and governing autonomous AI systems at scale. It is not an agent framework, not a chatbot toolkit, and not a prompt chaining library. AEOS is the operating environment that makes it possible to run AI agents reliably in production — the same way an operating system makes it possible to run processes reliably on hardware.

The core insight behind AEOS is that existing AI agent tools solve the *demo* problem: they let you string together LLM calls quickly and get something impressive running in an afternoon. AEOS solves the *production* problem: it provides the resource governance, execution isolation, observability, failure handling, memory management, and security enforcement that turn an AI proof-of-concept into a platform an engineering organization can operate with confidence.

AEOS provides a central Kernel that mediates all agent execution, a multi-tier Memory System that gives agents persistent context, a Knowledge Runtime for RAG and graph-based retrieval, an ML Platform for training and serving custom models, and a Software Intelligence Layer (OSIP) for code analysis and repository understanding — all unified under a single API surface, governed by a documented Architecture Constitution, and built to be extended through a typed Plugin SDK.

---

## RAG Quickstart — "Ask Your Documents" (run in 10 minutes)

The RAG Knowledge Runtime is the most self-contained part of AEOS and ships with
a browser UI. It runs **fully offline with zero API keys** — document ingestion,
embedding, retrieval, and grounded *cited* answer generation all work locally.

```bash
# 1. Install (CPU-only; first run downloads the ~90 MB embedding model)
pip install -r requirements.txt

# 2. Run the API + UI
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. Open the demo UI
#    → http://localhost:8000/
#    Drop a .txt/.md/.pdf/.html/.json file (or paste text), then ask a question.
#    You get an answer with inline [1][2] citations back to the source chunks.
```

Or drive it from the API:

```bash
BASE=http://localhost:8000/api/v1/rag

# Ingest
curl -s -X POST $BASE/ingest -H "Content-Type: application/json" \
  -d '{"text":"AEOS is an AI Engineering Orchestration System with a RAG layer.","source":"notes","namespace":"demo"}'

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

### Security scope — honest limitations

The hardening above covers the **RAG surface**. The following are **known,
unaddressed** issues elsewhere in the codebase, out of scope for this slice:

- `POST /ml/train` accepts an arbitrary `dataset_path` (unauthenticated file/URL
  read via pandas). Do not expose it publicly.
- The ML model registry uses `pickle.load` on `.pkl` files — loading a tampered
  model file is remote-code-execution-on-load. Treat `./data/model_registry/` as
  trusted-only.
- Only the RAG routes carry auth/rate-limit; other endpoints (`/run`, `/execute`,
  `/debug/state`, `/kernel/*`) are open. Run behind your own gateway in production.

---

## What AEOS Is NOT

| System | What it does | What AEOS does differently |
|---|---|---|
| **LangChain** | Composable chains of LLM calls; prompt templates and parsers; tool use via function calling | AEOS provides a full runtime with Kernel-mediated dispatch, governance policies, and production observability. LangChain is a library; AEOS is a platform. |
| **AutoGPT** | Autonomous goal-seeking agent that loops until done; long-running tasks via self-prompting | AEOS has explicit workflow DAGs, typed state, resource budgets, and multi-agent coordination. AutoGPT has no governance or observability layer. |
| **CrewAI** | Multi-agent role-playing with defined crew structures and task handoffs | AEOS mediation is done through a Kernel with a Service Registry; agents are typed and governed, not role-playing personas. AEOS supports CrewAI-style patterns as one workflow type. |
| **Vertex AI Agents** | Google Cloud's managed agent service with Dialogflow integration | AEOS is infrastructure you own and operate; it is cloud-agnostic, self-hostable, and extensible. Vertex AI Agents are a managed SaaS product. |
| **Semantic Kernel** | Microsoft SDK for integrating LLMs into .NET/Python apps; skill/plugin abstraction | AEOS provides the orchestration runtime, not just SDK primitives. Semantic Kernel is a library for application developers; AEOS is a platform for AI platform engineers. |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         API CLIENTS                             │
│              (curl, SDK, UI, CI/CD pipelines)                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP / WebSocket
┌──────────────────────────▼──────────────────────────────────────┐
│                      API GATEWAY LAYER                          │
│             FastAPI · Auth · Rate Limiting · Validation         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                  INTENT UNDERSTANDING LAYER                      │
│         Task Parsing · Intent Classification · Goal Extraction  │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    ╔═══ AEOS KERNEL ═══╗                        │
│                    ║  Central Bus      ║                        │
│                    ║  Policy Engine    ║                        │
│                    ║  Service Registry ║                        │
│                    ║  Task Scheduler   ║                        │
│                    ╚═══════════════════╝                        │
└───┬──────────┬──────────┬──────────┬──────────┬────────────────┘
    │          │          │          │          │
┌───▼───┐  ┌──▼───┐  ┌───▼──┐  ┌───▼───┐  ┌──▼──────┐
│Exec   │  │Work  │  │Agent │  │Memory │  │Knowledge│
│Engine │  │flow  │  │Runtime│  │System │  │Runtime  │
└───┬───┘  │Runtime│  └───┬──┘  └───────┘  └─────────┘
    │      └──┬───┘      │
    │         │      ┌───▼──────────┐
    │         │      │ Tool Runtime │
    │         │      │ Reasoning    │
    │         │      └──────────────┘
    │         │
┌───▼─────────▼──────────────────────────────────────────────────┐
│                   PLATFORM SERVICES                             │
│  ML Platform · Software Intelligence (OSIP) · Plugin System    │
└────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│               CROSS-CUTTING INFRASTRUCTURE                      │
│     Observability (OTel/Prometheus) · Governance · Security     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Capabilities

- **Kernel-Mediated Agent Orchestration** — All agent execution passes through the AEOS Kernel, which enforces policies, manages resource budgets, maintains the service registry, and provides the central event bus. No agent executes outside Kernel governance.

- **Multi-Agent Workflow Engine** — DAG-based workflow runtime that sequences, parallelizes, and manages state for complex multi-agent tasks. Workflows are typed data structures with explicit state machines, not procedural code.

- **6 Specialized Agent Types** — Research Agent, Code Agent, Data Analysis Agent, DevOps Agent, Security Agent, and Documentation Agent — each with domain-specific tools, memory configuration, and reasoning strategies.

- **4-Tier Memory System** — Working Memory (in-process ephemeral), Episodic Memory (session-scoped sequential), Semantic Memory (long-term vector-indexed with ChromaDB), and Procedural Memory (skill and pattern storage). Agents have persistent context across sessions.

- **RAG + Knowledge Graph** — Document ingestion, chunking, embedding, and semantic search through ChromaDB. Entity extraction, relationship modeling, and graph queries through Neo4j. Used together for hybrid retrieval.

- **ML Platform** — End-to-end pipeline for model training, evaluation, fine-tuning, and serving. A/B testing infrastructure for model comparison. Model registry for version management and rollback.

- **Software Intelligence Layer (OSIP)** — Code analysis, repository understanding, dependency graph construction, code quality metrics, and vulnerability scanning. Powers AI-assisted software engineering workflows.

- **Production Observability** — Distributed tracing with OpenTelemetry, metrics with Prometheus, structured logging, health check endpoints, and cost tracking across all LLM and API calls.

---

## Quick Start

### Prerequisites

- Python 3.11 or higher
- `pip` and `venv`
- (Optional) Docker and Docker Compose for full stack deployment
- API keys for your LLM provider (OpenAI, Anthropic, etc.)

### Installation

```bash
# Clone the repository
git clone https://github.com/your-org/aeos.git
cd aeos

# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .venv\Scripts\activate         # Windows

# Install dependencies
pip install -r requirements.txt
```

### Environment Setup

```bash
# Copy the example environment file
cp .env.example .env

# Edit .env with your configuration
```

Minimum required `.env` variables:

```env
# LLM Provider
OPENAI_API_KEY=sk-...
LLM_PRIMARY_MODEL=gpt-4o
LLM_FALLBACK_MODEL=gpt-4o-mini

# Database (SQLite for local dev, PostgreSQL for production)
DATABASE_URL=sqlite:///./aeos.db

# Vector Store (in-memory for local dev)
CHROMA_PERSIST_DIRECTORY=./data/chroma

# Redis (optional for local dev, required for production)
REDIS_URL=redis://localhost:6379

# Security
SECRET_KEY=your-secret-key-here
API_KEY_HEADER=X-API-Key

# Observability
LOG_LEVEL=INFO
ENABLE_TRACING=false
```

### Start the Server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

The API will be available at `http://localhost:8000`. OpenAPI documentation is at `http://localhost:8000/docs`.

### Example Requests

**Execute a single agent task:**

```bash
curl -X POST http://localhost:8000/api/v1/agent/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "agent_type": "research",
    "task": "Summarize the key differences between RAG and fine-tuning for production AI systems",
    "context": {},
    "options": {"max_steps": 5, "timeout": 60}
  }'
```

**Execute a multi-agent workflow:**

```bash
curl -X POST http://localhost:8000/api/v1/workflow/execute \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "workflow_id": "code-review",
    "inputs": {
      "repository_url": "https://github.com/your-org/your-repo",
      "pr_number": 42,
      "review_depth": "comprehensive"
    }
  }'
```

**Ingest documents into the Knowledge Runtime:**

```bash
curl -X POST http://localhost:8000/api/v1/knowledge/ingest \
  -H "Content-Type: multipart/form-data" \
  -H "X-API-Key: your-api-key" \
  -F "file=@architecture.pdf" \
  -F "metadata={\"tags\": [\"architecture\", \"design\"], \"source\": \"internal\"}"
```

**Query the Knowledge Runtime:**

```bash
curl -X POST http://localhost:8000/api/v1/knowledge/query \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "query": "What are the memory management strategies for long-running agents?",
    "top_k": 5,
    "filters": {"tags": ["architecture"]}
  }'
```

**Analyze a code repository:**

```bash
curl -X POST http://localhost:8000/api/v1/software-intelligence/analyze \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{
    "repository_path": "/path/to/repo",
    "analysis_type": "full",
    "include": ["complexity", "dependencies", "vulnerabilities"]
  }'
```

---

## Available Endpoints

| Method | Path | Description | Example Use Case |
|---|---|---|---|
| `GET` | `/health` | Platform health check with subsystem status | Load balancer probes, monitoring |
| `GET` | `/metrics` | Prometheus metrics endpoint | Scraping by Prometheus |
| `POST` | `/api/v1/agent/execute` | Execute a single agent task | Research, code generation, analysis |
| `GET` | `/api/v1/agent/status/{task_id}` | Get status of an agent task | Polling for async task results |
| `GET` | `/api/v1/agent/result/{task_id}` | Retrieve completed agent result | Fetching completed task output |
| `GET` | `/api/v1/agents` | List all registered agent types | Discovery, UI rendering |
| `POST` | `/api/v1/workflow/execute` | Execute a workflow by ID | Multi-agent pipeline execution |
| `GET` | `/api/v1/workflow/status/{workflow_id}` | Get workflow execution status | Polling workflow progress |
| `GET` | `/api/v1/workflow/result/{workflow_id}` | Retrieve workflow result | Fetching workflow output |
| `GET` | `/api/v1/workflows` | List available workflow definitions | Discovery |
| `POST` | `/api/v1/knowledge/ingest` | Ingest documents into the Knowledge Runtime | Adding docs, code, data |
| `POST` | `/api/v1/knowledge/query` | Query the knowledge base (RAG) | Semantic search |
| `DELETE` | `/api/v1/knowledge/document/{doc_id}` | Remove a document from knowledge base | Knowledge management |
| `POST` | `/api/v1/software-intelligence/analyze` | Analyze a code repository | OSIP code analysis |
| `GET` | `/api/v1/software-intelligence/report/{analysis_id}` | Get code analysis report | Fetching OSIP results |
| `POST` | `/api/v1/ml/train` | Trigger a model training job | ML Pipeline initiation |
| `GET` | `/api/v1/ml/models` | List registered models | Model registry browsing |
| `POST` | `/api/v1/ml/predict` | Get a prediction from a registered model | Model inference |
| `GET` | `/api/v1/governance/audit` | Query the audit log | Compliance, debugging |
| `GET` | `/api/v1/governance/costs` | Get cost breakdown by agent/workflow | Cost management |
| `GET` | `/docs` | OpenAPI interactive documentation | API exploration |
| `GET` | `/redoc` | ReDoc API documentation | API reference |

---

## Project Structure

```
aeos/
├── app/                          # Main application package
│   ├── main.py                   # FastAPI application entrypoint
│   ├── core/                     # Core platform infrastructure
│   │   ├── config.py             # Pydantic Settings (all config here)
│   │   ├── kernel/               # AEOS Kernel: dispatch, policy, registry
│   │   ├── llm/                  # LLM adapter interface + implementations
│   │   ├── database.py           # Database connection and session management
│   │   └── security.py           # Auth, API key validation, RBAC
│   ├── api/                      # FastAPI route definitions
│   │   ├── v1/                   # API v1 routes
│   │   │   ├── agent.py          # Agent execution endpoints
│   │   │   ├── workflow.py       # Workflow execution endpoints
│   │   │   ├── knowledge.py      # Knowledge Runtime endpoints
│   │   │   ├── ml.py             # ML Platform endpoints
│   │   │   ├── software_intelligence.py  # OSIP endpoints
│   │   │   └── governance.py     # Governance and audit endpoints
│   │   └── health.py             # Health check endpoint
│   ├── agents/                   # Agent Runtime implementation
│   │   ├── base.py               # Base Agent class and Cognitive Cycle
│   │   ├── research_agent.py     # Research Agent (web, knowledge retrieval)
│   │   ├── code_agent.py         # Code Agent (generation, analysis, execution)
│   │   ├── data_analysis_agent.py # Data Analysis Agent
│   │   ├── devops_agent.py       # DevOps Agent (deployment, monitoring)
│   │   ├── security_agent.py     # Security Agent (scanning, analysis)
│   │   └── documentation_agent.py # Documentation Agent
│   ├── workflows/                # Workflow Runtime
│   │   ├── engine.py             # DAG execution engine
│   │   ├── state_machine.py      # Workflow state management
│   │   └── definitions/          # Built-in workflow definitions (YAML)
│   ├── memory/                   # 4-Tier Memory System
│   │   ├── working.py            # Working memory (in-process)
│   │   ├── episodic.py           # Episodic memory (session-scoped)
│   │   ├── semantic.py           # Semantic memory (vector-indexed)
│   │   └── procedural.py         # Procedural memory (skills/patterns)
│   ├── knowledge/                # Knowledge Runtime
│   │   ├── rag_engine.py         # RAG pipeline (ChromaDB)
│   │   ├── knowledge_graph.py    # Knowledge Graph (Neo4j)
│   │   ├── ingestion/            # Document ingestion pipeline
│   │   └── retrieval/            # Hybrid retrieval strategies
│   ├── tools/                    # Tool Runtime
│   │   ├── registry.py           # Tool registration and discovery
│   │   ├── sandbox.py            # Tool execution sandbox
│   │   └── implementations/      # Built-in tool implementations
│   ├── reasoning/                # Reasoning Runtime
│   │   ├── chain_of_thought.py   # CoT reasoning strategy
│   │   ├── react.py              # ReAct reasoning strategy
│   │   └── reflection.py         # Reflection and self-critique
│   ├── ml/                       # ML Platform
│   │   ├── training/             # Training pipeline
│   │   ├── serving/              # Model serving
│   │   ├── evaluation/           # Model evaluation
│   │   └── registry/             # Model registry
│   ├── software_intelligence/    # OSIP — Software Intelligence Layer
│   │   ├── analyzer.py           # Repository analysis orchestrator
│   │   ├── code_quality.py       # Code quality metrics
│   │   ├── dependency_graph.py   # Dependency analysis
│   │   └── vulnerability.py      # Vulnerability scanning
│   ├── observability/            # Observability Layer
│   │   ├── tracing.py            # OpenTelemetry tracing setup
│   │   ├── metrics.py            # Prometheus metrics definitions
│   │   └── logging.py            # Structured logging configuration
│   ├── governance/               # Governance Layer
│   │   ├── policy_engine.py      # Policy enforcement
│   │   ├── audit_log.py          # Audit logging
│   │   └── cost_tracker.py       # Cost tracking and reporting
│   ├── plugins/                  # Plugin Architecture
│   │   ├── loader.py             # Plugin loader and registry
│   │   ├── sandbox.py            # Plugin execution sandbox
│   │   └── sdk/                  # Plugin SDK (for plugin developers)
│   └── models/                   # SQLAlchemy database models
│       ├── task.py               # Task model
│       ├── workflow.py           # Workflow execution model
│       ├── agent.py              # Agent registration model
│       └── audit.py              # Audit log model
├── docs/                         # Documentation
│   ├── architecture/             # Architecture specifications
│   │   ├── 000-VISION.md         # Vision and Mission
│   │   ├── 001-ARCHITECTURE.md   # Full architecture specification
│   │   └── ...                   # Component specifications
│   └── adr/                      # Architecture Decision Records
├── tests/                        # Test suite
│   ├── unit/                     # Unit tests (per component)
│   ├── integration/              # Integration tests
│   └── e2e/                      # End-to-end tests
├── scripts/                      # Operational scripts
│   ├── seed_knowledge.py         # Seed the knowledge base
│   └── migrate.py                # Database migrations
├── docker/                       # Docker configurations
│   ├── Dockerfile                # Main application image
│   └── docker-compose.yml        # Full stack composition
├── ARCHITECTURE_CONSTITUTION.md  # The governing architecture document
├── requirements.txt              # Python dependencies
├── .env.example                  # Environment variable template
└── README.md                     # This file
```

---

## Documentation Index

| Document | Path | Description |
|---|---|---|
| Architecture Constitution | `ARCHITECTURE_CONSTITUTION.md` | The 10 architectural laws that govern all decisions |
| Vision and Mission | `docs/architecture/000-VISION.md` | Why AEOS exists; the OS analogy explained |
| Full Architecture Spec | `docs/architecture/001-ARCHITECTURE.md` | Complete 15-layer runtime architecture |
| Kernel Design | `docs/architecture/010-KERNEL.md` | AEOS Kernel specification |
| Agent Runtime Spec | `docs/architecture/020-AGENT-RUNTIME.md` | Agent cognitive cycle and runtime |
| Tool Runtime Spec | `docs/architecture/030-TOOL-RUNTIME.md` | Tool registry, sandbox, cost tracking |
| Memory System Spec | `docs/architecture/040-MEMORY.md` | 4-tier memory architecture |
| Knowledge Runtime Spec | `docs/architecture/050-KNOWLEDGE.md` | RAG + Knowledge Graph specification |
| Observability Spec | `docs/architecture/060-OBSERVABILITY.md` | Tracing, metrics, logging |
| Governance Spec | `docs/architecture/070-GOVERNANCE.md` | Policy engine and audit |
| Plugin Architecture | `docs/architecture/080-PLUGINS.md` | Plugin SDK and loading |
| ADR Index | `docs/adr/README.md` | All architecture decision records |
| Operations Runbook | `docs/ops/RUNBOOK.md` | Production operations guide |

---

## Current Status

| Module | Status | Notes |
|---|---|---|
| FastAPI Application Shell | Complete | `app/main.py`, routes wired up |
| Core Configuration | Complete | Pydantic Settings, `.env` support |
| API Gateway Layer | Complete | All routes defined, auth middleware |
| Multi-Agent Orchestration | Complete | 6 agents, task dispatch, async execution |
| Knowledge Runtime (RAG) | Complete | ChromaDB integration, document ingestion, semantic search |
| ML Platform Pipeline | Complete | Training, evaluation, serving, model registry |
| Software Intelligence (OSIP) | Complete | Code analysis, dependency graphs, quality metrics |
| Multi-tier Memory System | In Progress | Working and Episodic complete; Semantic and Procedural in progress |
| Workflow DAG Engine | In Progress | Basic sequencing complete; parallel execution in progress |
| AEOS Kernel | Specified | Architecture defined; implementation in progress |
| Reasoning Runtime | In Progress | CoT and ReAct implemented; Reflection in progress |
| Tool Runtime Sandbox | Planned | Registry complete; sandboxing not yet implemented |
| Plugin Architecture | Planned | SDK design complete; loader in progress |
| Governance Layer | Planned | Cost tracking scaffolded; policy engine not yet implemented |
| Observability Layer | Partial | Structured logging complete; tracing and metrics in progress |
| Knowledge Graph (Neo4j) | Planned | Interface defined; integration not yet implemented |

---

## Roadmap

| Phase | Description | Status |
|---|---|---|
| **Phase 1** | Core platform foundation: FastAPI shell, LLM adapter, basic agent dispatch, RAG integration | Complete |
| **Phase 2** | AEOS Kernel implementation: service registry, policy enforcement, task lifecycle management | In Progress |
| **Phase 3** | Workflow Runtime: DAG execution, state machines, parallel agent coordination | In Progress |
| **Phase 4** | Memory System completion: all 4 tiers operational, memory consolidation, cross-session persistence | Planned |
| **Phase 5** | Tool Runtime and Plugin Architecture: sandboxed tool execution, plugin SDK, plugin marketplace | Planned |
| **Phase 6** | Governance Layer: cost enforcement, rate limiting, audit log, policy engine | Planned |
| **Phase 7** | Knowledge Graph integration: Neo4j, entity extraction, relationship modeling, hybrid retrieval | Planned |
| **Phase 8** | Production hardening: multi-node deployment, Kubernetes manifests, SLA enforcement, advanced observability | Planned |

---

## Architecture Constitution

All engineering decisions in AEOS must conform to the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md). The Constitution defines the 10 architectural laws that govern this platform — from Kernel-mediated dispatch to typed state requirements to observability mandates. Before making a significant architectural change, read the Constitution.

---

## Contributing

1. Read the [Architecture Constitution](ARCHITECTURE_CONSTITUTION.md) before writing any code.
2. For significant architectural changes, file an ADR in `docs/adr/` before implementation.
3. All code must have corresponding unit tests. Integration tests for cross-component features.
4. All new external I/O must go through an adapter interface (Constitutional Law 9).
5. All new configuration parameters must use the Pydantic Settings system (Constitutional Law 8).
6. PRs must cite which Constitutional Laws the change upholds or extends.

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

*AEOS is built by engineers, for engineers. It is infrastructure, not magic. If something breaks, there is always a reason — and the observability layer will help you find it.*
