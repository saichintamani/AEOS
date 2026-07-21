"""
Phase 13 Sprint 3 — cross-PROCESS checkpoint recovery.

Proves that a workflow interrupted by a HARD node crash continues from its last
durably-committed checkpoint when a fresh OS process takes over — using the real
CheckpointEngine (app/distributed/execution/checkpoint.py) over a durable,
process-shared FileCheckpointStore. Two separate `python -m` invocations share
one checkpoint directory; the first is killed mid-workflow with os._exit(137).

What this proves that an in-process test cannot:
  - Committed checkpoints survive a real process death and are read back by an
    independently-started process (durability, not just in-memory state).
  - Recovery continues from the correct step: no committed work is lost and no
    committed step is re-executed (exactly-once over the committed prefix).
  - INV-EXEC-002: an UNCOMMITTED checkpoint (crash between Phase-1 write and
    Phase-2 commit) is INVISIBLE to recovery — the resumed process redoes that
    step rather than trusting the half-written state.

Marked slow — spawns real interpreters.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STEPS = 5


def _run_node(root: Path, *, worker_id: str, workflow_id: str,
              crash_after_commit: int = -1, crash_before_commit: int = -1,
              timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [
        sys.executable, "-m", "app.distributed.testbed.checkpoint_worker_node",
        "--root", str(root), "--workflow-id", workflow_id, "--worker-id", worker_id,
        "--steps", str(_STEPS),
    ]
    if crash_after_commit >= 0:
        cmd += ["--crash-after-commit", str(crash_after_commit)]
    if crash_before_commit >= 0:
        cmd += ["--crash-before-commit", str(crash_before_commit)]
    return subprocess.run(cmd, cwd=str(_REPO_ROOT), env=env, timeout=timeout,
                          capture_output=True, text=True)


def _ledger(root: Path) -> list[dict]:
    path = root / "ledger.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_workflow_resumes_from_last_committed_checkpoint(tmp_path):
    """Crash after committing step 2 of 5; a fresh process completes 3-4 with no
    lost or duplicated committed work, and records a measured recovery time."""
    root = tmp_path / "ckpt"
    wf = "wf-recover"

    # Process 1: run, commit steps 0-2, then hard-crash.
    p1 = _run_node(root, worker_id="w1", workflow_id=wf, crash_after_commit=2)
    assert p1.returncode == 137, f"expected hard crash; stdout={p1.stdout}"
    assert "CRASH_COMMITTED" in p1.stdout
    assert not (root / "result.json").exists(), "workflow completed despite crash"

    l1 = _ledger(root)
    assert [e["step"] for e in l1] == [0, 1, 2]
    assert all(e["worker_id"] == "w1" for e in l1)

    # Process 2: fresh interpreter, same dir → resumes and finishes.
    t0 = time.monotonic()
    p2 = _run_node(root, worker_id="w2", workflow_id=wf)
    recovery_s = time.monotonic() - t0
    assert p2.returncode == 0, f"resume failed; stderr={p2.stderr}"
    assert "RESUMED" in p2.stdout and "from_step=3" in p2.stdout, p2.stdout

    # The whole workflow ran exactly once end-to-end across the two processes.
    steps = [e["step"] for e in _ledger(root)]
    assert steps == [0, 1, 2, 3, 4], f"ledger={steps}"

    result = json.loads((root / "result.json").read_text())
    assert result["completed_by"] == "w2"
    assert result["completed_steps"] == [0, 1, 2, 3, 4]
    # In-process resume with a fresh interpreter is well under the RTO budget.
    assert recovery_s < 20.0, f"recovery took {recovery_s:.2f}s"


def test_uncommitted_checkpoint_is_not_resumed(tmp_path):
    """INV-EXEC-002: a checkpoint written but not committed (crash between the
    two phases) must be invisible to recovery — the resumed process redoes that
    step from the last COMMITTED point rather than trusting half-written state."""
    root = tmp_path / "ckpt"
    wf = "wf-uncommitted"

    # Process 1: commit steps 0-1, write step 2's checkpoint, crash BEFORE commit.
    p1 = _run_node(root, worker_id="w1", workflow_id=wf, crash_before_commit=2)
    assert p1.returncode == 137
    assert "CRASH_UNCOMMITTED" in p1.stdout

    # Process 2: must resume from step 2 (NOT step 3) — the uncommitted step-2
    # checkpoint is ignored.
    p2 = _run_node(root, worker_id="w2", workflow_id=wf)
    assert p2.returncode == 0
    assert "from_step=2" in p2.stdout, f"resumed from wrong step: {p2.stdout}"

    ledger = _ledger(root)
    # Committed prefix [0,1] executed once by w1; step 2 executed by BOTH workers
    # (w1's attempt was uncommitted → redone by w2). Steps 3-4 by w2.
    w1_steps = [e["step"] for e in ledger if e["worker_id"] == "w1"]
    w2_steps = [e["step"] for e in ledger if e["worker_id"] == "w2"]
    assert w1_steps == [0, 1, 2]
    assert w2_steps == [2, 3, 4], f"w2 steps={w2_steps}"

    result = json.loads((root / "result.json").read_text())
    assert result["completed_steps"] == [0, 1, 2, 3, 4]
