"""
Runnable checkpointing workflow node — one OS process that executes a linear
N-step workflow through the REAL CheckpointEngine, backed by a durable
FileCheckpointStore shared with other processes.

Used by tests/integration/distributed/test_checkpoint_recovery.py to prove that
a workflow interrupted by a hard node crash *continues from its last committed
checkpoint* when a fresh process takes over — and that an UNCOMMITTED checkpoint
is never used as a resume point (INV-EXEC-002).

Two side-effect ledgers make the behaviour externally verifiable:
  - <root>/ledger.jsonl  — one line per step actually executed (append-only),
    tagged with the executing worker_id. Steps appearing twice across processes
    are re-executed (uncommitted) work; the committed prefix must appear once.
  - <root>/result.json   — written only when the workflow runs to completion.

Modes:
  --steps N                total steps in the workflow (0..N-1)
  --crash-after-commit K   execute+commit steps up to K, then hard-exit (crash)
  --crash-before-commit K  execute steps up to K, write step K's checkpoint but
                           hard-exit BEFORE committing it (uncommitted suffix)
  (neither)                run to completion, resuming from any committed state

Resume is automatic: on start the node loads the latest committed checkpoint for
the workflow and continues from the next step. A fresh workflow starts at 0.

Run:
    python -m app.distributed.testbed.checkpoint_worker_node \
        --root /tmp/aeos-ckpt --workflow-id wf-1 --worker-id w1 \
        --steps 5 --crash-after-commit 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from app.distributed.execution.checkpoint import CheckpointEngine
from app.distributed.execution.context import CheckpointData
from app.distributed.execution.states import ExecutionState
from app.distributed.testbed.file_checkpoint_store import FileCheckpointStore


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AEOS checkpointing workflow node")
    p.add_argument("--root", required=True, help="shared durable checkpoint dir")
    p.add_argument("--workflow-id", required=True)
    p.add_argument("--worker-id", required=True)
    p.add_argument("--steps", type=int, default=5)
    p.add_argument("--crash-after-commit", type=int, default=-1)
    p.add_argument("--crash-before-commit", type=int, default=-1)
    p.add_argument("--step-ms", type=int, default=5)
    return p.parse_args(argv)


def _append_ledger(root: Path, workflow_id: str, worker_id: str, step: int) -> None:
    line = json.dumps({"workflow_id": workflow_id, "worker_id": worker_id, "step": step})
    with (root / "ledger.jsonl").open("a") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


async def _run(args: argparse.Namespace) -> None:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)
    store = FileCheckpointStore(root)
    engine = CheckpointEngine(store)

    # ── determine the resume point from durable, COMMITTED state only ─────────
    latest = await store.latest_for_workflow(args.workflow_id, committed_only=True)
    if latest is not None and latest.data is not None:
        resume_from = latest.data.step_index + 1
        acc = list(latest.data.workflow_state.get("completed_steps", []))
        print(f"RESUMED workflow={args.workflow_id} from_step={resume_from} "
              f"(last_committed={latest.data.step_index} worker={latest.data.worker_id})",
              flush=True)
    else:
        resume_from = 0
        acc = []
        print(f"FRESH workflow={args.workflow_id} start_step=0", flush=True)

    # ── execute steps resume_from .. steps-1 ──────────────────────────────────
    for step in range(resume_from, args.steps):
        if args.step_ms > 0:
            await asyncio.sleep(args.step_ms / 1000.0)

        # The step's side effect (ledger append) is the observable "work".
        _append_ledger(root, args.workflow_id, args.worker_id, step)
        acc.append(step)
        print(f"STEP workflow={args.workflow_id} step={step} worker={args.worker_id}",
              flush=True)

        data = CheckpointData(
            task_id=f"{args.workflow_id}-task",
            workflow_id=args.workflow_id,
            step_id=f"s{step}",
            state=ExecutionState.RUNNING,
            step_index=step,
            total_steps=args.steps,
            sequence_number=step,
            workflow_state={"completed_steps": list(acc)},
            worker_id=args.worker_id,
        )

        # Phase 1: durable write (committed=False).
        entry = await engine.write_full(data)

        if step == args.crash_before_commit:
            # Hard crash AFTER Phase-1 write, BEFORE Phase-2 commit: the step's
            # checkpoint is durably present but UNCOMMITTED. Recovery must ignore it.
            print(f"CRASH_UNCOMMITTED workflow={args.workflow_id} step={step}", flush=True)
            os._exit(137)

        # Phase 2: commit.
        await engine.commit(entry)
        print(f"COMMIT workflow={args.workflow_id} step={step}", flush=True)

        if step == args.crash_after_commit:
            # Hard crash AFTER commit: steps 0..step are durably safe.
            print(f"CRASH_COMMITTED workflow={args.workflow_id} step={step}", flush=True)
            os._exit(137)

    # ── completion ────────────────────────────────────────────────────────────
    result = {
        "workflow_id": args.workflow_id,
        "completed_by": args.worker_id,
        "total_steps": args.steps,
        "completed_steps": acc,
    }
    (root / "result.json").write_text(json.dumps(result))
    print(f"WORKFLOW_COMPLETE workflow={args.workflow_id} steps={acc}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
