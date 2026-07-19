"""
AEOS Example 4 — Enterprise Approval Workflow

Simulates a governance-gated change request pipeline.

Usage:
    python run.py --request "Deploy ML model v2 to production"
    python run.py --request "Increase database connection pool from 20 to 100"
"""

import asyncio
import argparse
from pathlib import Path


WORKFLOW_YAML = Path(__file__).parent / "workflow.yaml"


async def run_approval(request: str, host: str = "http://localhost:8000") -> None:
    try:
        import httpx, yaml
    except ImportError:
        print("pip install httpx pyyaml")
        return

    from aeos.workflow.compiler import WorkflowCompiler

    BORDER = "=" * 65
    print(f"\n{BORDER}")
    print(" AEOS — Enterprise Approval Workflow")
    print(f"{BORDER}")
    print(f" Change Request: {request[:70]}")
    print(f"{BORDER}\n")

    raw = yaml.safe_load(WORKFLOW_YAML.read_text())
    compiled = WorkflowCompiler().compile(raw, variables={"request": request})

    outcomes: dict[str, dict] = {}

    async with httpx.AsyncClient(base_url=host, timeout=120.0) as client:
        try:
            await client.get("/health")
        except Exception:
            print(f"Cannot reach {host}. Start AEOS: aeos start")
            return

        for step in compiled["steps"]:
            name = step["name"]
            print(f"▶ {name.upper()}")
            resp = await client.post(
                "/api/v1/run",
                json={"task": step["task"], "mode": step["mode"]},
            )
            data = resp.json()
            result = str(data.get("result") or data.get("response") or "")
            outcomes[name] = {"status": data.get("status", "?"), "result": result}
            print(f"  {result[:250]}{'...' if len(result) > 250 else ''}")
            print()

            # Gate: if policy-check finds NON_COMPLIANT, stop the workflow
            if name == "policy-check" and "NON_COMPLIANT" in result.upper():
                print("⚠  POLICY GATE: Request is NON_COMPLIANT. Workflow stopped.")
                print(f"\n{BORDER}\n")
                return

    # Final summary
    risk_summary = outcomes.get("risk-summary", {}).get("result", "")
    impl_plan    = outcomes.get("implementation-plan", {}).get("result", "")
    policy_ok    = "NON_COMPLIANT" not in outcomes.get("policy-check", {}).get("result", "").upper()

    print(f"{BORDER}")
    print(f" APPROVAL STATUS: {'APPROVED ✓' if policy_ok else 'BLOCKED ✗'}")
    print(f"{BORDER}")
    if risk_summary:
        print(f"\n Risk Summary:\n  {risk_summary[:300]}")
    if impl_plan and policy_ok:
        print(f"\n Implementation Plan:\n  {impl_plan[:400]}")
    print(f"\n{BORDER}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", default="Deploy ML model v2 to production")
    parser.add_argument("--host", default="http://localhost:8000")
    args = parser.parse_args()
    asyncio.run(run_approval(args.request, host=args.host))
