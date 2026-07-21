"""
AEOS - Autonomous Research Organization (engine)

Turns a research question into a governed, fully-traceable publication.

Design goals
------------
1. **Self-governing.** Five roles (planner, researcher, critic, reviewer,
   governance board) collaborate. Nothing is published until the governance
   board approves it against explicit, inspectable policies.

2. **Every decision traceable.** Each stage appends an entry to a *hash-chained*
   ledger. Entry N stores the SHA-256 of (entry N-1's hash + entry N's content),
   so the audit trail is tamper-evident: altering any past decision breaks the
   chain. The ledger is written as JSONL and the final provenance as JSON.

3. **Runs on real AEOS.** By default each role calls the AEOS orchestrator at
   ``POST /api/v1/run`` (same surface used by the other examples). If the server
   is unreachable you can pass ``offline=True`` to use a clearly-labelled
   deterministic stub so the *governance + traceability mechanics* are
   demonstrable without a model backend. Offline outputs are prefixed with
   ``[OFFLINE-STUB]`` and are NEVER presented as model reasoning.

This module has no hard dependency on the rest of AEOS beyond the public
workflow compiler; ``httpx`` and ``pyyaml`` are imported lazily.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


# -----------------------------------------------------------------------------
# Tamper-evident decision ledger
# -----------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class LedgerEntry:
    """One recorded decision in the organization's audit trail."""
    seq: int                      # monotonic position in the chain
    stage: str                    # workflow stage name
    role: str                     # which role acted
    trace_id: str                 # correlates to the AEOS run trace
    timestamp: str                # ISO-8601 UTC
    inputs: dict[str, str]        # digests of the inputs this decision consumed
    output: str                   # the produced artifact / decision text
    decision: str                 # machine-readable verdict (e.g. APPROVE)
    rationale: str                # why this decision was reached
    source: str                   # "aeos-api" or "offline-stub"
    prev_hash: str                # hash of the previous entry (chain link)
    entry_hash: str = ""          # SHA-256 over prev_hash + canonical content

    def content_digest(self) -> str:
        """Canonical serialization used for the chain hash (excludes entry_hash)."""
        payload = {
            "seq": self.seq,
            "stage": self.stage,
            "role": self.role,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "inputs": self.inputs,
            "output": self.output,
            "decision": self.decision,
            "rationale": self.rationale,
            "source": self.source,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    def seal(self) -> "LedgerEntry":
        self.entry_hash = _sha256(self.prev_hash + self.content_digest())
        return self


class DecisionLedger:
    """Append-only, hash-chained ledger of organizational decisions."""

    GENESIS = "0" * 64

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []

    @property
    def entries(self) -> list[LedgerEntry]:
        return list(self._entries)

    def record(
        self,
        *,
        stage: str,
        role: str,
        trace_id: str,
        inputs: dict[str, str],
        output: str,
        decision: str,
        rationale: str,
        source: str,
    ) -> LedgerEntry:
        prev_hash = self._entries[-1].entry_hash if self._entries else self.GENESIS
        # Store digests of inputs (not full text) to keep the ledger compact
        # while still proving which artifacts each decision consumed.
        input_digests = {k: _sha256(v)[:16] for k, v in inputs.items()}
        entry = LedgerEntry(
            seq=len(self._entries),
            stage=stage,
            role=role,
            trace_id=trace_id,
            timestamp=_now_iso(),
            inputs=input_digests,
            output=output,
            decision=decision,
            rationale=rationale,
            source=source,
            prev_hash=prev_hash,
        ).seal()
        self._entries.append(entry)
        return entry

    def verify(self) -> tuple[bool, Optional[int]]:
        """Re-derive the chain. Returns (ok, first_broken_seq_or_None)."""
        prev = self.GENESIS
        for e in self._entries:
            expected = _sha256(prev + e.content_digest())
            if expected != e.entry_hash:
                return False, e.seq
            prev = e.entry_hash
        return True, None

    def to_jsonl(self) -> str:
        return "\n".join(json.dumps(asdict(e), sort_keys=True) for e in self._entries)

    def head(self) -> str:
        """Final chain hash - a single fingerprint of the entire decision history."""
        return self._entries[-1].entry_hash if self._entries else self.GENESIS


# -----------------------------------------------------------------------------
# Role executors - how a role "thinks"
# -----------------------------------------------------------------------------

# A RoleExecutor takes (task_text) and returns (output_text, trace_id, source).
RoleExecutor = Callable[[str], "RoleResult"]


@dataclass
class RoleResult:
    output: str
    trace_id: str
    source: str


class AEOSApiExecutor:
    """Executes a role by calling the live AEOS orchestrator."""

    def __init__(self, host: str, timeout_s: float = 120.0) -> None:
        self.host = host
        self.timeout_s = timeout_s

    def available(self) -> bool:
        try:
            import httpx
        except ImportError:
            return False
        try:
            with httpx.Client(base_url=self.host, timeout=5.0) as c:
                c.get("/health")
            return True
        except Exception:
            return False

    def __call__(self, task: str, mode: str = "single-agent") -> RoleResult:
        import httpx
        with httpx.Client(base_url=self.host, timeout=self.timeout_s) as c:
            resp = c.post("/api/v1/run", json={"task": task, "mode": mode})
            data = resp.json()
        output = str(data.get("result") or data.get("response") or "")
        trace_id = str(data.get("trace_id") or data.get("task_id") or uuid.uuid4())
        return RoleResult(output=output, trace_id=trace_id, source="aeos-api")


class OfflineStubExecutor:
    """
    Deterministic, clearly-labelled stand-in used when no AEOS server is
    reachable. It does NOT reason; it produces structured placeholder text so the
    governance + traceability machinery can be demonstrated end-to-end. Every
    output is prefixed with [OFFLINE-STUB] so it can never be mistaken for a real
    model answer.
    """

    def __call__(self, task: str, mode: str = "single-agent") -> RoleResult:
        trace_id = f"offline-{uuid.uuid4().hex[:12]}"
        head = task.strip().splitlines()[0][:80] if task.strip() else "(empty)"
        body = (
            f"[OFFLINE-STUB] Deterministic placeholder for role task.\n"
            f"Task begins: {head}\n"
            f"(No model was called. This text exists only to exercise the "
            f"governance and audit-trail mechanics.)"
        )
        # Give the reviewer/critic stubs deterministic verdicts so the gate runs.
        lower = task.lower()
        if "verdict:" in lower or "reviewer" in lower or "you are the reviewer" in lower:
            body += "\nVERDICT: APPROVE"
        if "severity:" in lower or "you are the critic" in lower:
            body += "\nSEVERITY: LOW"
        return RoleResult(output=body, trace_id=trace_id, source="offline-stub")


# -----------------------------------------------------------------------------
# Governance board - the publication gate
# -----------------------------------------------------------------------------

@dataclass
class GovernancePolicy:
    """An inspectable rule the board evaluates. Pure function of the dossier."""
    id: str
    description: str
    check: Callable[["ResearchDossier"], bool]


@dataclass
class GovernanceDecision:
    approved: bool
    passed: list[str]
    failed: list[str]
    rationale: str


class GovernanceBoard:
    """
    Final gate. Evaluates the assembled dossier against explicit policies and
    approves publication only if ALL policies pass. The policy set is data, not
    hidden logic, so the basis of every approval/rejection is auditable.
    """

    def __init__(self, policies: Optional[list[GovernancePolicy]] = None) -> None:
        self.policies = policies if policies is not None else self._default_policies()

    @staticmethod
    def _default_policies() -> list[GovernancePolicy]:
        return [
            GovernancePolicy(
                id="GOV-1-plan-present",
                description="An investigation plan was produced.",
                check=lambda d: bool(d.plan.strip()),
            ),
            GovernancePolicy(
                id="GOV-2-evidence-present",
                description="Evidence/findings were gathered.",
                check=lambda d: len(d.revised_findings.strip()) >= 20,
            ),
            GovernancePolicy(
                id="GOV-3-critique-addressed",
                description="A critique was performed and a revision followed it.",
                check=lambda d: bool(d.critique.strip()) and bool(d.revised_findings.strip()),
            ),
            GovernancePolicy(
                id="GOV-4-critique-not-high",
                description="Critic severity is not HIGH (unresolved major flaws).",
                check=lambda d: "SEVERITY: HIGH" not in d.critique.upper(),
            ),
            GovernancePolicy(
                id="GOV-5-reviewer-approved",
                description="Reviewer returned VERDICT: APPROVE.",
                check=lambda d: "VERDICT: APPROVE" in d.review.upper(),
            ),
        ]

    def evaluate(self, dossier: "ResearchDossier") -> GovernanceDecision:
        passed, failed = [], []
        for p in self.policies:
            try:
                ok = bool(p.check(dossier))
            except Exception:
                ok = False
            (passed if ok else failed).append(p.id)
        approved = not failed
        if approved:
            rationale = f"All {len(passed)} governance policies passed."
        else:
            rationale = (
                f"Blocked by {len(failed)} policy failure(s): {', '.join(failed)}. "
                f"Passed: {', '.join(passed) or 'none'}."
            )
        return GovernanceDecision(approved, passed, failed, rationale)


# -----------------------------------------------------------------------------
# The organization
# -----------------------------------------------------------------------------

@dataclass
class ResearchDossier:
    question: str = ""
    plan: str = ""
    initial_findings: str = ""
    critique: str = ""
    revised_findings: str = ""
    review: str = ""
    revision_rounds: int = 0


class ResearchOrganization:
    """
    Orchestrates the five roles through the governed workflow:

        question -> plan -> research -> critique -> revise -> review
                 -> governance board -> (approve -> publish) | (reject -> revise loop)

    Bounded revision loop: if the reviewer says REVISE or the board rejects, the
    work goes back to a critique+revise cycle up to ``max_rounds`` times.
    """

    def __init__(
        self,
        executor: RoleExecutor,
        board: Optional[GovernanceBoard] = None,
        max_rounds: int = 2,
        printer: Callable[[str], None] = print,
    ) -> None:
        self.executor = executor
        self.board = board or GovernanceBoard()
        self.max_rounds = max_rounds
        self.ledger = DecisionLedger()
        self._print = printer

    def _run_role(
        self,
        *,
        stage: str,
        role: str,
        task: str,
        inputs: dict[str, str],
        decision: str,
        rationale: str,
    ) -> str:
        t0 = time.monotonic()
        result = self.executor(task)
        dt = time.monotonic() - t0
        self.ledger.record(
            stage=stage,
            role=role,
            trace_id=result.trace_id,
            inputs=inputs,
            output=result.output,
            decision=decision,
            rationale=rationale,
            source=result.source,
        )
        self._print(f"  [{role:<16}] {stage:<10} -> {len(result.output):>5} chars "
                    f"({dt:4.1f}s, {result.source}, trace={result.trace_id[:12]})")
        return result.output

    def run(self, question: str, tasks: dict[str, str]) -> tuple[ResearchDossier, GovernanceDecision]:
        """
        Execute the full governed workflow. ``tasks`` maps stage name -> task text
        (already interpolated with the question). Returns the final dossier and
        the governance decision.
        """
        d = ResearchDossier(question=question)

        # 1. PLAN
        d.plan = self._run_role(
            stage="plan", role="planner", task=tasks["plan"],
            inputs={"question": question},
            decision="PLAN_PRODUCED", rationale="Decomposed question into sub-questions.",
        )

        # 2. RESEARCH
        d.initial_findings = self._run_role(
            stage="research", role="researcher",
            task=tasks["research"].replace("{plan}", d.plan),
            inputs={"question": question, "plan": d.plan},
            decision="EVIDENCE_GATHERED", rationale="Gathered evidence per plan.",
        )
        d.revised_findings = d.initial_findings

        # 3..N. CRITIQUE -> REVISE -> REVIEW, then GOVERNANCE. Bounded loop.
        governance = None
        for round_i in range(1, self.max_rounds + 1):
            d.revision_rounds = round_i

            d.critique = self._run_role(
                stage="critique", role="critic",
                task=tasks["critique"].replace("{research}", d.revised_findings),
                inputs={"findings": d.revised_findings},
                decision="CRITIQUE_ISSUED", rationale=f"Adversarial review round {round_i}.",
            )

            d.revised_findings = self._run_role(
                stage="revise", role="researcher",
                task=tasks["revise"].replace("{research}", d.revised_findings)
                                    .replace("{critique}", d.critique),
                inputs={"findings": d.revised_findings, "critique": d.critique},
                decision="REVISED", rationale=f"Addressed critique, round {round_i}.",
            )

            d.review = self._run_role(
                stage="review", role="reviewer",
                task=tasks["review"].replace("{revise}", d.revised_findings),
                inputs={"revised": d.revised_findings},
                decision="REVIEWED", rationale=f"Quality/policy review round {round_i}.",
            )

            # GOVERNANCE BOARD gate
            governance = self.board.evaluate(d)
            self.ledger.record(
                stage="governance", role="governance-board",
                trace_id=f"gov-{uuid.uuid4().hex[:12]}",
                inputs={"dossier": d.revised_findings + d.review + d.critique},
                output=json.dumps({
                    "approved": governance.approved,
                    "passed": governance.passed,
                    "failed": governance.failed,
                }, indent=2),
                decision="APPROVE_PUBLICATION" if governance.approved else "REJECT_PUBLICATION",
                rationale=governance.rationale,
                source="governance-board",
            )
            verdict = "APPROVED" if governance.approved else "REJECTED"
            self._print(f"  [governance-board ] governance -> {verdict} "
                        f"(round {round_i}): {governance.rationale}")

            if governance.approved:
                break
            if round_i < self.max_rounds:
                self._print(f"  (loop) Governance rejected - returning to critique/revise "
                            f"(round {round_i + 1}/{self.max_rounds})")

        return d, governance  # type: ignore[return-value]


# -----------------------------------------------------------------------------
# Publication + provenance artifacts
# -----------------------------------------------------------------------------

def build_publication(d: ResearchDossier, gov: GovernanceDecision, ledger: DecisionLedger) -> str:
    status = "PUBLISHED" if gov.approved else "WITHHELD (governance rejected)"
    ok, broken = ledger.verify()
    lines = [
        f"# Research Publication - {status}",
        "",
        f"**Question:** {d.question}",
        f"**Revision rounds:** {d.revision_rounds}",
        f"**Governance:** {'APPROVED' if gov.approved else 'REJECTED'} - {gov.rationale}",
        f"**Audit chain head:** `{ledger.head()}`",
        f"**Audit chain integrity:** {'VERIFIED' if ok else f'BROKEN at seq {broken}'}",
        "",
        "## Investigation Plan",
        d.plan or "_(none)_",
        "",
        "## Findings (post-revision)",
        d.revised_findings or "_(none)_",
        "",
        "## Critic's Assessment",
        d.critique or "_(none)_",
        "",
        "## Reviewer Verdict",
        d.review or "_(none)_",
        "",
        "## Decision Provenance",
        "Every stage below is hash-chained; altering any entry breaks the chain.",
        "",
        "| seq | stage | role | decision | trace |",
        "|-----|-------|------|----------|-------|",
    ]
    for e in ledger.entries:
        lines.append(f"| {e.seq} | {e.stage} | {e.role} | {e.decision} | `{e.trace_id[:12]}` |")
    if not gov.approved:
        lines += ["", "> This dossier was NOT published because the governance board "
                  "rejected it. The content above is retained for audit only."]
    return "\n".join(lines) + "\n"


def write_artifacts(out_dir: Path, publication: str, ledger: DecisionLedger,
                    d: ResearchDossier, gov: GovernanceDecision) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pub_path = out_dir / "publication.md"
    ledger_path = out_dir / "ledger.jsonl"
    trace_path = out_dir / "decision_trace.json"

    pub_path.write_text(publication, encoding="utf-8")
    ledger_path.write_text(ledger.to_jsonl(), encoding="utf-8")

    ok, broken = ledger.verify()
    trace_path.write_text(json.dumps({
        "question": d.question,
        "approved": gov.approved,
        "governance": {
            "passed": gov.passed, "failed": gov.failed, "rationale": gov.rationale,
        },
        "revision_rounds": d.revision_rounds,
        "chain_head": ledger.head(),
        "chain_verified": ok,
        "chain_broken_at": broken,
        "num_decisions": len(ledger.entries),
        "generated_at": _now_iso(),
    }, indent=2), encoding="utf-8")

    return {"publication": pub_path, "ledger": ledger_path, "trace": trace_path}
