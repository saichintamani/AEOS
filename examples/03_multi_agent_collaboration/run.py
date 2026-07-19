"""
AEOS Example 3 — Multi-Agent Collaboration

Runs a 5-agent pipeline for complex tasks.

Usage:
    python run.py "Design a REST API for a task management system"
    python run.py "Write a technical specification for a payment service"
"""

import asyncio
import sys
import time
from pathlib import Path


WORKFLOW_YAML = Path(__file__).parent / "workflow.yaml"
DEFAULT_TASK = "Design a REST API for a task management system with user authentication"


async def run(task: str, host: str = "http://localhost:8000") -> None:
    try:
        import httpx, yaml
    except ImportError:
        print("pip install httpx pyyaml")
        return

    from aeos.workflow.compiler import WorkflowCompiler

    print(f"\n{'='*65}")
    print(" AEOS — Multi-Agent Collaboration Pipeline")
    print(f"{'='*65}")
    print(f" Task: {task[:80]}")
    print(f"{'='*65}\n")

    raw = yaml.safe_load(WORKFLOW_YAML.read_text())
    compiled = WorkflowCompiler().compile(raw, variables={"task": task})
    total_start = time.monotonic()

    async with httpx.AsyncClient(base_url=host, timeout=120.0) as client:
        try:
            await client.get("/health")
        except Exception:
            print(f"Cannot reach {host}. Start AEOS: aeos start\n")
            return

        outputs: dict[str, str] = {}
        for i, step in enumerate(compiled["steps"], 1):
            t0 = time.monotonic()
            print(f"[{i}/{len(compiled['steps'])}] {step['name'].upper()}", end="  ", flush=True)
            resp = await client.post(
                "/api/v1/run",
                json={"task": step["task"], "mode": step["mode"]},
            )
            elapsed = time.monotonic() - t0
            data = resp.json()
            status = data.get("status", "?")
            result = data.get("result") or data.get("response") or ""
            outputs[step["name"]] = str(result)
            print(f"[{status}] ({elapsed:.1f}s)")
            if result:
                print(f"   → {str(result)[:180]}{'...' if len(str(result)) > 180 else ''}")
            print()

    total = time.monotonic() - total_start
    print(f"{'='*65}")
    print(f" Pipeline complete in {total:.1f}s")
    print(f"{'='*65}\n")

    # Print the final review verdict
    if "review" in outputs:
        verdict = outputs["review"]
        icon = "✓" if "APPROVED" in verdict.upper() else "⚠"
        print(f"Final verdict: {icon} {verdict[:200]}\n")


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or DEFAULT_TASK
    asyncio.run(run(task))
