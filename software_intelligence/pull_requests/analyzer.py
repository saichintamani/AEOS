"""
Software Intelligence Platform — Pull Request Intelligence
===========================================================
AI-driven analysis of pull requests.

Capabilities:
  - Summarization: what does this PR do and why?
  - Risk estimation: 0–1 score based on size, complexity, coverage, history
  - Affected component detection: which modules/layers are touched?
  - Reviewer suggestion: based on file ownership and expertise
  - Review checklist generation: automated pre-merge checks
  - Breaking change detection: API surface changes, schema migrations
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from software_intelligence.schemas import (
    DependencyGraph, ParseResult, PRAnalysis, PullRequestRecord,
)


# ── Risk factor weights ────────────────────────────────────────────────────────

# Each factor contributes 0–1 to overall risk; weighted sum is normalised.
_RISK_WEIGHTS = {
    "large_diff":          0.20,    # > 500 lines changed
    "many_files":          0.15,    # > 20 files
    "no_tests":            0.20,    # no test files in changed set
    "touches_security":    0.25,    # auth/, crypto/, secrets
    "touches_data":        0.15,    # migrations/, db/, schema
    "no_description":      0.05,    # empty PR body
}

_SECURITY_PATHS = {"auth", "security", "crypto", "secret", "password", "token", "credential"}
_DATA_PATHS     = {"migration", "migrations", "schema", "db", "database", "seed"}


class PRAnalyzer:
    """
    Analyzes pull requests and produces PRAnalysis records.

    Usage:
        analyzer = PRAnalyzer(parse_results=results, graph=dep_graph)
        analysis = analyzer.analyze(pr_record)
    """

    def __init__(
        self,
        parse_results: list[ParseResult] | None = None,
        graph: DependencyGraph | None = None,
        llm_backend: Any = None,
    ) -> None:
        self._results  = {r.file_path: r for r in (parse_results or [])}
        self._graph    = graph
        self._llm      = llm_backend

    def analyze(self, pr: PullRequestRecord) -> PRAnalysis:
        analysis = PRAnalysis(pr_id=pr.pr_id)
        analysis.summary = self._summarize(pr)
        analysis.risk_score, analysis.risk_factors = self._estimate_risk(pr)
        analysis.affected_components = self._affected_components(pr.files_changed)
        analysis.suggested_reviewers = self._suggest_reviewers(pr.files_changed)
        analysis.review_checklist = self._build_checklist(pr, analysis)
        analysis.breaking_changes = self._detect_breaking_changes(pr)
        analysis.requires_migration = self._requires_migration(pr)
        return analysis

    def analyze_batch(self, prs: list[PullRequestRecord]) -> list[PRAnalysis]:
        return [self.analyze(pr) for pr in prs]

    # ── Summarization ──────────────────────────────────────────────────────────

    def _summarize(self, pr: PullRequestRecord) -> str:
        if self._llm:
            return self._llm.summarize(
                f"PR #{pr.number}: {pr.title}\n\n{pr.body[:1000]}\n\n"
                f"Files changed: {', '.join(pr.files_changed[:10])}"
            )
        # Rule-based fallback
        body_first = re.split(r'(?<=[.!?])\s+', pr.body.strip())[:2]
        summary = " ".join(body_first)[:300] if body_first else pr.title
        return (
            f"**{pr.title}** ({pr.additions}+ / {pr.deletions}-) "
            f"across {len(pr.files_changed)} files. {summary}"
        )

    # ── Risk estimation ────────────────────────────────────────────────────────

    def _estimate_risk(self, pr: PullRequestRecord) -> tuple[float, list[str]]:
        scores: dict[str, float] = {}
        factors: list[str] = []

        total_lines = pr.additions + pr.deletions
        if total_lines > 500:
            scores["large_diff"] = min(total_lines / 2000, 1.0)
            factors.append(f"Large diff: {total_lines} lines changed")

        if len(pr.files_changed) > 20:
            scores["many_files"] = min(len(pr.files_changed) / 50, 1.0)
            factors.append(f"Many files: {len(pr.files_changed)} files changed")

        test_files = [f for f in pr.files_changed if "test" in f.lower()]
        if not test_files:
            scores["no_tests"] = 1.0
            factors.append("No test files included in this PR")

        security_touched = [
            f for f in pr.files_changed
            if any(s in Path(f).parts for s in _SECURITY_PATHS)
        ]
        if security_touched:
            scores["touches_security"] = 1.0
            factors.append(f"Modifies security-sensitive paths: {', '.join(security_touched[:3])}")

        data_touched = [
            f for f in pr.files_changed
            if any(s in Path(f).parts or s in Path(f).stem.lower() for s in _DATA_PATHS)
        ]
        if data_touched:
            scores["touches_data"] = 1.0
            factors.append(f"Modifies data/schema paths: {', '.join(data_touched[:3])}")

        if not pr.body.strip():
            scores["no_description"] = 1.0
            factors.append("PR has no description")

        total_risk = sum(
            scores.get(k, 0) * w for k, w in _RISK_WEIGHTS.items()
        )
        return round(min(total_risk, 1.0), 4), factors

    # ── Affected components ────────────────────────────────────────────────────

    def _affected_components(self, files_changed: list[str]) -> list[str]:
        components: set[str] = set()
        for path in files_changed:
            parts = Path(path).parts
            if parts:
                components.add(parts[0])
        return sorted(components)[:15]

    # ── Reviewer suggestions ───────────────────────────────────────────────────

    def _suggest_reviewers(self, files_changed: list[str]) -> list[str]:
        # TODO: integrate with git blame / commit history to suggest
        # authors who have most recently touched the affected files.
        # Placeholder: return empty list until commit history is available.
        return []

    # ── Review checklist ───────────────────────────────────────────────────────

    def _build_checklist(self, pr: PullRequestRecord, analysis: PRAnalysis) -> list[str]:
        checklist = [
            "[ ] Code changes match the PR description",
            "[ ] Tests cover the new/changed functionality",
            "[ ] No new lint/type errors introduced",
        ]
        if analysis.risk_score > 0.5:
            checklist.insert(0, "[ ] **HIGH RISK** — extra review recommended")
        if "Modifies security-sensitive" in " ".join(analysis.risk_factors):
            checklist.append("[ ] Security review: check for credential exposure or unsafe patterns")
        if analysis.requires_migration:
            checklist.append("[ ] Database migration reviewed and tested")
        if analysis.breaking_changes:
            checklist.append("[ ] Breaking changes documented in CHANGELOG")
        return checklist

    # ── Breaking change detection ──────────────────────────────────────────────

    def _detect_breaking_changes(self, pr: PullRequestRecord) -> bool:
        text = (pr.title + " " + pr.body).lower()
        signals = ["breaking", "break change", "incompatible", "removes api", "deprecated", "rename"]
        return any(s in text for s in signals)

    def _requires_migration(self, pr: PullRequestRecord) -> bool:
        return any(
            any(s in Path(f).stem.lower() or s in str(Path(f).parent).lower() for s in _DATA_PATHS)
            for f in pr.files_changed
        )
