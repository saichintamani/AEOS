"""
Software Intelligence Platform — Report Builder
================================================
Aggregates all analysis outputs into structured, deliverable reports.

Report types:
  RepositoryHealthReport  — full-repo snapshot (metrics + debt + security + review)
  SecurityAuditReport     — security findings export (SARIF-compatible summary)
  TechnicalDebtReport     — debt items with remediation roadmap
  PRIntelligenceReport    — per-PR analysis summaries
  IssueIntelligenceReport — issue analysis with duplicate clusters
  ExecutiveSummary        — C-level 1-pager (scores + top risks + trends)

Output formats:
  - Python dataclasses (default, serializable to JSON)
  - Markdown (via to_markdown())
  - JSON (via to_json())
  - SARIF 2.1.0 (security findings — for GitHub Code Scanning integration)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from software_intelligence.schemas import (
    CodeReviewReport, FileMetrics, IssueAnalysis, IssueRecord,
    PRAnalysis, PullRequestRecord, RepositoryMetrics,
    SecurityReport, TechnicalDebtReport,
)


# ── Report domain types ────────────────────────────────────────────────────────

@dataclass
class HealthScore:
    """Normalised 0–100 score per dimension."""
    security:      float = 0.0
    maintainability: float = 0.0
    test_coverage: float = 0.0
    documentation: float = 0.0
    complexity:    float = 0.0
    overall:       float = 0.0

    def to_grade(self, score: float) -> str:
        if score >= 90: return "A"
        if score >= 75: return "B"
        if score >= 60: return "C"
        if score >= 45: return "D"
        return "F"

    @property
    def overall_grade(self) -> str:
        return self.to_grade(self.overall)


@dataclass
class TopRisk:
    category: str
    title: str
    severity: str
    file_path: str
    remediation: str


@dataclass
class RepositoryHealthReport:
    repo_id:     str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    scores:      HealthScore = field(default_factory=HealthScore)
    top_risks:   list[TopRisk] = field(default_factory=list)
    metrics:     RepositoryMetrics | None = None
    security:    SecurityReport | None = None
    debt:        TechnicalDebtReport | None = None
    review:      CodeReviewReport | None = None
    trend_summary: str = ""
    recommendations: list[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        lines = [
            f"# Repository Health Report: `{self.repo_id}`",
            f"> Generated: {self.generated_at}",
            "",
            "## Overall Score",
            f"**{self.scores.overall:.1f}/100** — Grade: **{self.scores.overall_grade}**",
            "",
            "| Dimension | Score | Grade |",
            "|-----------|-------|-------|",
        ]
        dims = [
            ("Security", self.scores.security),
            ("Maintainability", self.scores.maintainability),
            ("Test Coverage", self.scores.test_coverage),
            ("Documentation", self.scores.documentation),
            ("Complexity", self.scores.complexity),
        ]
        for name, score in dims:
            grade = self.scores.to_grade(score)
            lines.append(f"| {name} | {score:.1f} | {grade} |")

        if self.top_risks:
            lines += ["", "## Top Risks", ""]
            for i, risk in enumerate(self.top_risks[:10], 1):
                lines.append(f"{i}. **[{risk.severity}]** {risk.title}")
                lines.append(f"   - File: `{risk.file_path}`")
                lines.append(f"   - Remediation: {risk.remediation}")

        if self.recommendations:
            lines += ["", "## Recommendations", ""]
            for rec in self.recommendations:
                lines.append(f"- {rec}")

        if self.metrics:
            m = self.metrics
            lines += [
                "", "## Metrics Snapshot", "",
                f"- **Total files**: {m.total_files}",
                f"- **Total LOC**: {m.total_loc:,}",
                f"- **Functions**: {m.total_functions}",
                f"- **Classes**: {m.total_classes}",
                f"- **Avg cyclomatic complexity**: {m.avg_cyclomatic}",
                f"- **Avg maintainability index**: {m.avg_maintainability}",
                f"- **Test coverage ratio**: {m.test_coverage_ratio:.1%}",
                f"- **Doc coverage**: {m.doc_coverage:.1%}",
            ]

        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps({
            "repo_id":     self.repo_id,
            "generated_at": self.generated_at,
            "scores": {
                "overall": self.scores.overall,
                "security": self.scores.security,
                "maintainability": self.scores.maintainability,
                "test_coverage": self.scores.test_coverage,
                "documentation": self.scores.documentation,
                "complexity": self.scores.complexity,
                "grade": self.scores.overall_grade,
            },
            "top_risks": [
                {
                    "category": r.category,
                    "title": r.title,
                    "severity": r.severity,
                    "file_path": r.file_path,
                    "remediation": r.remediation,
                }
                for r in self.top_risks
            ],
            "recommendations": self.recommendations,
        }, indent=2)


@dataclass
class ExecutiveSummary:
    repo_id:    str
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    one_liner:  str = ""
    grade:      str = "N/A"
    overall_score: float = 0.0
    critical_issues: int = 0
    high_issues: int = 0
    debt_hours:  float = 0.0
    top_3_risks: list[str] = field(default_factory=list)
    top_3_wins:  list[str] = field(default_factory=list)
    trend:       str = "stable"

    def to_markdown(self) -> str:
        return "\n".join([
            f"# Executive Summary — `{self.repo_id}`",
            f"> {self.generated_at}",
            "",
            f"## Health: **{self.grade}** ({self.overall_score:.1f}/100)",
            "",
            self.one_liner,
            "",
            f"- **Critical issues**: {self.critical_issues}",
            f"- **High issues**: {self.high_issues}",
            f"- **Estimated remediation effort**: {self.debt_hours:.0f}h",
            f"- **Trend**: {self.trend}",
            "",
            "### Top Risks",
            *[f"- {r}" for r in self.top_3_risks],
            "",
            "### Quick Wins",
            *[f"- {w}" for w in self.top_3_wins],
        ])


# ── Report builder ─────────────────────────────────────────────────────────────

class ReportBuilder:
    """
    Builds structured reports from analysis outputs.

    Usage:
        builder = ReportBuilder()
        report = (
            builder
            .set_metrics(repo_metrics)
            .set_security(security_report)
            .set_debt(debt_report)
            .set_review(review_report)
            .build_health_report(repo_id="my-repo")
        )
        md = report.to_markdown()
    """

    def __init__(self) -> None:
        self._metrics:  RepositoryMetrics | None    = None
        self._security: SecurityReport | None       = None
        self._debt:     TechnicalDebtReport | None  = None
        self._review:   CodeReviewReport | None     = None
        self._issues:   list[IssueAnalysis]         = []
        self._prs:      list[PRAnalysis]            = []

    def set_metrics(self, m: RepositoryMetrics) -> "ReportBuilder":
        self._metrics = m
        return self

    def set_security(self, s: SecurityReport) -> "ReportBuilder":
        self._security = s
        return self

    def set_debt(self, d: TechnicalDebtReport) -> "ReportBuilder":
        self._debt = d
        return self

    def set_review(self, r: CodeReviewReport) -> "ReportBuilder":
        self._review = r
        return self

    def set_issue_analyses(self, analyses: list[IssueAnalysis]) -> "ReportBuilder":
        self._issues = analyses
        return self

    def set_pr_analyses(self, analyses: list[PRAnalysis]) -> "ReportBuilder":
        self._prs = analyses
        return self

    # ── Health report ──────────────────────────────────────────────────────────

    def build_health_report(self, repo_id: str) -> RepositoryHealthReport:
        scores = self._compute_scores()
        top_risks = self._collect_top_risks()
        recommendations = self._build_recommendations(scores, top_risks)

        report = RepositoryHealthReport(
            repo_id=repo_id,
            scores=scores,
            top_risks=top_risks,
            metrics=self._metrics,
            security=self._security,
            debt=self._debt,
            review=self._review,
            recommendations=recommendations,
        )
        return report

    def _compute_scores(self) -> HealthScore:
        security = 100.0
        if self._security:
            risk = self._security.risk_score   # 0–10
            security = max(0.0, 100.0 - risk * 10)

        maintainability = 100.0
        if self._metrics and self._metrics.avg_maintainability > 0:
            maintainability = min(self._metrics.avg_maintainability, 100.0)

        test_coverage = 0.0
        if self._metrics:
            test_coverage = self._metrics.test_coverage_ratio * 100

        documentation = 100.0
        if self._metrics:
            documentation = self._metrics.doc_coverage * 100

        complexity = 100.0
        if self._metrics and self._metrics.avg_cyclomatic > 0:
            # CC of 1 = perfect, CC of 20+ = 0
            complexity = max(0.0, 100.0 - (self._metrics.avg_cyclomatic - 1) * 5)

        overall = round(
            security * 0.25
            + maintainability * 0.20
            + test_coverage * 0.20
            + documentation * 0.15
            + complexity * 0.20,
            1,
        )

        return HealthScore(
            security=round(security, 1),
            maintainability=round(maintainability, 1),
            test_coverage=round(test_coverage, 1),
            documentation=round(documentation, 1),
            complexity=round(complexity, 1),
            overall=overall,
        )

    def _collect_top_risks(self) -> list[TopRisk]:
        risks: list[TopRisk] = []

        # Security findings (CRITICAL + HIGH)
        if self._security:
            from software_intelligence.schemas import SecuritySeverity
            for f in self._security.findings:
                if f.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH):
                    risks.append(TopRisk(
                        category="security",
                        title=f.title,
                        severity=f.severity.value,
                        file_path=f.file_path,
                        remediation=f.remediation,
                    ))

        # Debt items (CRITICAL)
        if self._debt:
            from software_intelligence.schemas import DebtSeverity
            for item in self._debt.items:
                if item.severity == DebtSeverity.CRITICAL:
                    risks.append(TopRisk(
                        category=item.category.value,
                        title=item.title,
                        severity=item.severity.value,
                        file_path=item.file_path,
                        remediation=f"~{item.effort_hours}h effort",
                    ))

        # Sort critical first, then by file
        risks.sort(key=lambda r: (0 if r.severity == "critical" else 1, r.file_path))
        return risks[:15]

    def _build_recommendations(
        self,
        scores: HealthScore,
        risks: list[TopRisk],
    ) -> list[str]:
        recs = []
        if scores.security < 60:
            recs.append("🔴 Address critical security findings — move secrets to environment variables or a secrets manager.")
        if scores.test_coverage < 30:
            recs.append("🔴 Test coverage is below 30% — add unit tests for the most critical modules first.")
        if scores.complexity < 50:
            recs.append("🟠 Refactor high-complexity functions (CC > 10) — target the top 10 identified hotspots.")
        if scores.documentation < 50:
            recs.append("🟠 Improve API documentation — add docstrings to all public functions.")
        if scores.maintainability < 60:
            recs.append("🟠 Maintainability index is low — reduce file size and coupling.")
        if scores.overall >= 80:
            recs.append("✅ Codebase is in good health — maintain current practices.")
        return recs

    # ── Executive summary ──────────────────────────────────────────────────────

    def build_executive_summary(self, repo_id: str) -> ExecutiveSummary:
        health = self.build_health_report(repo_id)
        scores = health.scores

        critical = sum(
            1 for r in health.top_risks if r.severity in ("critical", "CRITICAL")
        )
        high = sum(
            1 for r in health.top_risks if r.severity in ("high", "HIGH")
        )
        debt_hours = self._debt.total_debt_hours if self._debt else 0.0

        one_liner = (
            f"This repository scores {scores.overall:.0f}/100 ({scores.overall_grade}). "
            f"There are {critical} critical and {high} high-severity issues requiring attention. "
            f"Estimated remediation effort is {debt_hours:.0f} engineering hours."
        )
        top_risks = [r.title for r in health.top_risks[:3]]
        quick_wins = (
            [item.title for item in self._debt.items
             if item.effort_hours <= 2.0 and item.impact_score >= 3.0][:3]
            if self._debt else []
        )

        return ExecutiveSummary(
            repo_id=repo_id,
            one_liner=one_liner,
            grade=scores.overall_grade,
            overall_score=scores.overall,
            critical_issues=critical,
            high_issues=high,
            debt_hours=debt_hours,
            top_3_risks=top_risks,
            top_3_wins=quick_wins,
        )

    # ── SARIF export ───────────────────────────────────────────────────────────

    def build_sarif(self, repo_id: str) -> dict:
        """
        Produce a SARIF 2.1.0 document from security findings.
        Compatible with GitHub Code Scanning / GHAS upload.
        """
        if not self._security:
            return {}

        _LEVEL = {
            "critical": "error",
            "high":     "error",
            "medium":   "warning",
            "low":      "note",
            "info":     "note",
        }

        rules = {}
        results_sarif = []

        for f in self._security.findings:
            rule_id = f.cwe or f.kind.value
            if rule_id not in rules:
                rules[rule_id] = {
                    "id": rule_id,
                    "name": f.title,
                    "shortDescription": {"text": f.title},
                    "helpUri": f"https://cwe.mitre.org/data/definitions/{f.cwe.replace('CWE-', '')}.html"
                    if f.cwe.startswith("CWE-") else "",
                }
            level = _LEVEL.get(f.severity.value.lower(), "warning")
            results_sarif.append({
                "ruleId": rule_id,
                "level": level,
                "message": {"text": f.description},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": f.file_path.replace("\\", "/")},
                        "region": {"startLine": f.line or 1},
                    }
                }],
            })

        return {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{
                "tool": {
                    "driver": {
                        "name": "AEOS SecurityScanner",
                        "version": "0.1.0",
                        "rules": list(rules.values()),
                    }
                },
                "results": results_sarif,
            }],
        }
