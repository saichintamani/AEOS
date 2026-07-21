"""
AEOS Example - Autonomous Research Organization (runner)

A self-governing research org: a question flows through planning, evidence
gathering, adversarial critique, revision, reviewer sign-off, and a governance
board gate before publication. Every decision is recorded in a tamper-evident,
hash-chained audit ledger.

Usage:
    # Against a running AEOS server (aeos start):
    python run.py --question "What are the trade-offs of Raft vs Paxos?"

    # Fully offline (deterministic stub, no server required) - demonstrates the
    # governance + traceability machinery end to end:
    python run.py --question "..." --offline

Artifacts are written to ./out/:
    publication.md       - final publication (or a WITHHELD notice if rejected)
    ledger.jsonl         - the full hash-chained decision ledger
    decision_trace.json  - provenance summary + chain-integrity result
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the repo root importable so `aeos.*` resolves when run in-tree.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from org import (  # noqa: E402
    AEOSApiExecutor,
    OfflineStubExecutor,
    ResearchOrganization,
    GovernanceBoard,
    build_publication,
    write_artifacts,
)

WORKFLOW_YAML = Path(__file__).parent / "workflow.yaml"
OUT_DIR = Path(__file__).parent / "out"
BORDER = "=" * 72


def load_tasks(question: str) -> dict[str, str]:
    """Compile the workflow YAML and return {stage_name: interpolated_task}."""
    import yaml
    from aeos.workflow.compiler import WorkflowCompiler

    raw = yaml.safe_load(WORKFLOW_YAML.read_text(encoding="utf-8"))
    compiled = WorkflowCompiler().compile(raw, variables={"question": question})
    return {s["name"]: s["task"] for s in compiled["steps"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="AEOS Autonomous Research Organization")
    parser.add_argument("--question", required=True, help="Research question to investigate")
    parser.add_argument("--host", default="http://localhost:8000", help="AEOS server base URL")
    parser.add_argument("--offline", action="store_true",
                        help="Use the deterministic offline stub (no server needed)")
    parser.add_argument("--rounds", type=int, default=2, help="Max critique/revise rounds")
    args = parser.parse_args()

    print(f"\n{BORDER}")
    print(" AEOS - Autonomous Research Organization")
    print(f"{BORDER}")
    print(f" Question: {args.question}")

    # Select the role executor: live AEOS API, or offline stub.
    if args.offline:
        executor = OfflineStubExecutor()
        print(" Mode:     OFFLINE (deterministic stub - outputs labelled [OFFLINE-STUB])")
    else:
        api = AEOSApiExecutor(host=args.host)
        if not api.available():
            print(f"\n Cannot reach AEOS at {args.host}. Start it with:  aeos start")
            print(" Or run this demo fully offline:  python run.py --question \"...\" --offline\n")
            return 2
        executor = api
        print(f" Mode:     LIVE (AEOS orchestrator at {args.host})")
    print(f"{BORDER}\n")

    try:
        tasks = load_tasks(args.question)
    except ImportError:
        print("Missing dependency. Install with:  pip install pyyaml httpx")
        return 1

    org = ResearchOrganization(
        executor=executor,
        board=GovernanceBoard(),
        max_rounds=max(1, args.rounds),
    )

    print(" Workflow trace (each line is a recorded, hash-chained decision):\n")
    dossier, governance = org.run(args.question, tasks)

    publication = build_publication(dossier, governance, org.ledger)
    paths = write_artifacts(OUT_DIR, publication, org.ledger, dossier, governance)

    ok, broken = org.ledger.verify()
    print(f"\n{BORDER}")
    print(f" OUTCOME: {'PUBLISHED OK' if governance.approved else 'WITHHELD X (governance rejected)'}")
    print(f" Governance policies - passed: {len(governance.passed)}, failed: {len(governance.failed)}")
    print(f" Decisions recorded: {len(org.ledger.entries)}")
    print(f" Audit chain integrity: {'VERIFIED OK' if ok else f'BROKEN at seq {broken} X'}")
    print(f" Chain head: {org.ledger.head()}")
    print(f"{BORDER}")
    print(" Artifacts:")
    for label, p in paths.items():
        print(f"   {label:<12} -> {p}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
