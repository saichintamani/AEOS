"""
AEOS Project Scaffolding — `aeos init <template>`

Creates a new project directory with starter code, config, and Dockerfile
from the named template. Each template is a set of files written to the
target directory.
"""

from __future__ import annotations

from pathlib import Path


# ── Template file generators ──────────────────────────────────────────────────

def _common_files(name: str) -> dict[str, str]:
    """Files included in every template."""
    return {
        ".env": f"""\
# AEOS Configuration — {name}
# Copy this file to .env and fill in your secrets.
ENVIRONMENT=development
LOG_LEVEL=INFO
LOG_JSON=false
DEBUG=true
AGENT_TIMEOUT_SECONDS=60
# Optional: set a GitHub token for the GitHub Analyzer
# GITHUB_TOKEN=ghp_...
""",
        ".gitignore": """\
__pycache__/
*.py[cod]
*.egg-info/
.env
.venv/
venv/
dist/
build/
*.so
*.dylib
.pytest_cache/
.mypy_cache/
benchmark_results/
data/model_registry/
""",
        "README.md": f"""\
# {name}

Built with [AEOS](https://github.com/anthropics/aeos) — AI Engineering Orchestration System.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start the runtime
aeos start

# Submit a task
aeos workflow submit --task "Your task here"
```

## API

- `POST /api/v1/run` — Submit a task
- `GET  /health`     — Health check
- `GET  /metrics`    — Prometheus metrics
- `GET  /api/v1/docs` — Interactive API docs
""",
        "requirements.txt": """\
# Add AEOS as a dependency once published:
# aeos>=0.1.0

fastapi==0.115.5
uvicorn[standard]==0.32.1
pydantic==2.10.3
pydantic-settings==2.6.1
anyio==4.7.0
httpx==0.28.0
pytest==8.3.4
pytest-asyncio==0.24.0
""",
    }


def _env_example() -> str:
    return """\
ENVIRONMENT=development
LOG_LEVEL=INFO
DEBUG=true
"""


# ── Template: minimal ─────────────────────────────────────────────────────────

def _template_minimal(name: str) -> dict[str, str]:
    files = _common_files(name)
    files["workflow.yaml"] = """\
# Minimal AEOS workflow
workflow:
  name: hello-aeos
  agents:
    - simple
  steps:
    - task: "Explain what AEOS is in one sentence."
      mode: single-agent
"""
    files["main.py"] = """\
\"\"\"
Minimal AEOS application.
Run: uvicorn main:app --port 8000
Or:  aeos start
\"\"\"
# This project uses the AEOS runtime directly.
# Configure via .env, then run: aeos start
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import app  # noqa: F401 — re-export for uvicorn
"""
    return files


# ── Template: research-assistant ─────────────────────────────────────────────

def _template_research(name: str) -> dict[str, str]:
    files = _common_files(name)
    files["workflow.yaml"] = """\
# Research Assistant workflow
workflow:
  name: research-assistant
  description: |
    Research a topic using the AEOS agent network.
    Planner decomposes the query; Researcher gathers context;
    Analyst synthesises findings; Reviewer validates the output.
  agents:
    - planner
    - researcher
    - analyst
    - reviewer
  steps:
    - name: plan
      task: "Break down this research question into 3 sub-questions: {query}"
      mode: single-agent
      agent: planner
    - name: research
      task: "Research and summarise findings for: {query}"
      mode: multi-agent
    - name: review
      task: "Review and validate the research output for accuracy and completeness."
      mode: single-agent
      agent: reviewer
"""
    files["run_research.py"] = """\
\"\"\"
Run a research workflow from the command line.
Usage: python run_research.py "What is Raft consensus?"
\"\"\"
import asyncio, sys

async def main(query: str):
    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        print(f"Researching: {query}")
        resp = await client.post("/api/v1/run", json={"task": query, "mode": "multi-agent"}, timeout=60)
        data = resp.json()
        print(f"Status: {data.get('status')}")
        print(f"Agent:  {data.get('agent_id', data.get('agent', '?'))}")
        print(f"Result: {data.get('result', data.get('response', ''))}")

if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "What is Raft consensus?"
    asyncio.run(main(q))
"""
    return files


# ── Template: rag-system ─────────────────────────────────────────────────────

def _template_rag(name: str) -> dict[str, str]:
    files = _common_files(name)
    files["workflow.yaml"] = """\
# RAG System workflow
workflow:
  name: rag-system
  description: Ingest documents and answer questions using RAG.
  agents:
    - researcher
  steps:
    - task: "Answer the following question using the knowledge base: {query}"
      mode: single-agent
      agent: researcher
"""
    files["ingest.py"] = """\
\"\"\"
Ingest a text file into the AEOS RAG knowledge base.
Usage: python ingest.py my_document.txt
\"\"\"
import asyncio, sys
from pathlib import Path

async def ingest(path: str):
    import httpx
    text = Path(path).read_text()
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        resp = await client.post(
            "/api/v1/rag/ingest",
            json={"text": text, "source": path},
            timeout=30,
        )
        data = resp.json()
        print(f"Ingested: {data.get('chunks_added', '?')} chunks from {path}")

if __name__ == "__main__":
    asyncio.run(ingest(sys.argv[1] if len(sys.argv) > 1 else "sample.txt"))
"""
    files["query.py"] = """\
\"\"\"
Query the RAG knowledge base.
Usage: python query.py "What is the main topic?"
\"\"\"
import asyncio, sys

async def query(q: str):
    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        resp = await client.post("/api/v1/rag/query", json={"query": q, "top_k": 5}, timeout=30)
        data = resp.json()
        for i, r in enumerate(data.get("results", []), 1):
            print(f"[{i}] score={r['score']:.3f}  {r['text'][:120]}")

if __name__ == "__main__":
    asyncio.run(query(" ".join(sys.argv[1:]) or "What is the main topic?"))
"""
    return files


# ── Template: multi-agent ────────────────────────────────────────────────────

def _template_multi_agent(name: str) -> dict[str, str]:
    files = _common_files(name)
    files["workflow.yaml"] = """\
# Multi-Agent Collaboration workflow
workflow:
  name: multi-agent-collaboration
  description: |
    Full multi-agent pipeline: Planner → Researcher → Analyst → Reviewer.
    Suitable for complex reasoning and report generation tasks.
  agents:
    - planner
    - researcher
    - analyst
    - reviewer
    - executor
  steps:
    - name: plan
      task: "Create a step-by-step plan for: {task}"
      agent: planner
    - name: research
      task: "Research and gather information for: {task}"
      agent: researcher
    - name: analyse
      task: "Analyse the research findings and identify key insights for: {task}"
      agent: analyst
    - name: review
      task: "Review the complete analysis for accuracy, completeness, and clarity."
      agent: reviewer
"""
    files["run_pipeline.py"] = """\
\"\"\"
Submit a task to the full multi-agent pipeline.
Usage: python run_pipeline.py "Design a REST API for a blog platform"
\"\"\"
import asyncio, sys, json

async def run(task: str):
    import httpx
    async with httpx.AsyncClient(base_url="http://localhost:8000") as client:
        resp = await client.post(
            "/api/v1/run",
            json={"task": task, "mode": "multi-agent"},
            timeout=120,
        )
        data = resp.json()
        print(json.dumps(data, indent=2))

if __name__ == "__main__":
    asyncio.run(run(" ".join(sys.argv[1:]) or "Design a REST API for a blog platform"))
"""
    return files


# ── Template registry ─────────────────────────────────────────────────────────

_TEMPLATE_BUILDERS = {
    "minimal":            _template_minimal,
    "research-assistant": _template_research,
    "rag-system":         _template_rag,
    "multi-agent":        _template_multi_agent,
}


def create_project(template: str, target: Path) -> None:
    """Write all template files into *target* directory."""
    builder = _TEMPLATE_BUILDERS.get(template)
    if builder is None:
        raise ValueError(f"Unknown template: {template}")

    name = target.name
    files = builder(name)

    target.mkdir(parents=True, exist_ok=False)
    for rel_path, content in files.items():
        dest = target / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
