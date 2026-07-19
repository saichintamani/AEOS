"""
AEOS Example 1 — Research Workflow Runner

Submits a research question through the 4-stage agent pipeline.

Usage:
    python run.py "What are the tradeoffs between Raft and Paxos?"
    python run.py  # uses default query
"""

import asyncio
import sys
import json
from pathlib import Path


WORKFLOW_YAML = Path(__file__).parent / "workflow.yaml"
DEFAULT_QUERY = "What are the tradeoffs between strong consistency and eventual consistency in distributed systems?"


async def run_research(query: str, host: str = "http://localhost:8000") -> None:
    try:
        import httpx
    except ImportError:
        print("Install httpx: pip install httpx")
        return

    try:
        import yaml
    except ImportError:
        print("Install pyyaml: pip install pyyaml")
        return

    from aeos.workflow.compiler import WorkflowCompiler

    print(f"\n{'='*60}")
    print(f" AEOS Research Workflow")
    print(f"{'='*60}")
    print(f" Query: {query[:80]}")
    print(f"{'='*60}\n")

    raw = yaml.safe_load(WORKFLOW_YAML.read_text())
    compiler = WorkflowCompiler()
    compiled = compiler.compile(raw, variables={"query": query})

    async with httpx.AsyncClient(base_url=host, timeout=120.0) as client:
        # Check server health
        try:
            health = (await client.get("/health")).json()
            print(f"Server: {health.get('status', '?')} (v{health.get('version', '?')})\n")
        except Exception:
            print(f"[WARNING] Cannot reach {host} — is AEOS running? (aeos start)\n")
            return

        for i, step in enumerate(compiled["steps"], 1):
            print(f"[{i}/{len(compiled['steps'])}] {step['name'].upper()}")
            print(f"  Agent: {step['agent'] or 'auto-route'}")
            resp = await client.post(
                "/api/v1/run",
                json={"task": step["task"], "mode": step["mode"]},
            )
            data = resp.json()
            status = data.get("status", "?")
            print(f"  Status: {status}")
            result = data.get("result") or data.get("response") or ""
            if result:
                print(f"  Output: {str(result)[:200]}{'...' if len(str(result)) > 200 else ''}")
            print()

    print(f"{'='*60}")
    print(" Research complete.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or DEFAULT_QUERY
    asyncio.run(run_research(query))
