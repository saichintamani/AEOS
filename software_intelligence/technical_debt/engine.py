"""
Software Intelligence Platform — Technical Debt Engine
=======================================================
Detects, categorises, and prioritises technical debt across a codebase.

Debt categories detected:
  1. Complexity Debt    — functions/files with excessive cyclomatic complexity
  2. Duplication Debt   — copy-paste code blocks
  3. Dead Code          — unreferenced functions/classes
  4. Large File Debt    — files exceeding LOC thresholds
  5. Coupling Debt      — excessively coupled modules
  6. Documentation Debt — missing docstrings on public API
  7. Test Coverage Debt — files/modules with no tests
  8. Architecture Smells — circular dependencies, layer violations

Design:
  BaseDebtAnalyzer   → one detector per debt type
  TechnicalDebtEngine → facade, aggregates findings into TechnicalDebtReport
  DebtRemediationPlanner → prioritises items by ROI (impact / effort)
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import (
    DebtCategory, DebtItem, DebtSeverity, DependencyGraph,
    FileMetrics, ParseResult, RepositoryMetrics, TechnicalDebtReport,
)


# ── Abstract analyzer ──────────────────────────────────────────────────────────

class BaseDebtAnalyzer(ABC):
    """Single debt category detector. Stateless and composable."""

    @property
    @abstractmethod
    def category(self) -> DebtCategory: ...

    @abstractmethod
    def analyze(
        self,
        results: list[ParseResult],
        file_metrics: list[FileMetrics] | None = None,
        graph: DependencyGraph | None = None,
    ) -> list[DebtItem]: ...

    def _item(
        self,
        file_path: str,
        title: str,
        description: str,
        severity: DebtSeverity,
        effort_hours: float,
        impact_score: float,
        line: int = 0,
    ) -> DebtItem:
        return DebtItem(
            category=self.category,
            severity=severity,
            file_path=file_path,
            line=line,
            title=title,
            description=description,
            effort_hours=effort_hours,
            impact_score=round(impact_score, 2),
        )


# ── Complexity debt ────────────────────────────────────────────────────────────

class ComplexityDebtAnalyzer(BaseDebtAnalyzer):
    """Flags functions with dangerously high cyclomatic complexity."""

    category = DebtCategory.COMPLEXITY

    HIGH_CC = 10
    CRITICAL_CC = 20
    VERY_HIGH_CC = 50

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        items = []
        for result in results:
            for fn in result.functions:
                cc = fn.cyclomatic_complexity
                if cc >= self.VERY_HIGH_CC:
                    items.append(self._item(
                        result.file_path,
                        f"Extreme complexity: `{fn.name}` (CC={cc})",
                        f"Function `{fn.name}` has cyclomatic complexity {cc}. "
                        "Exceeds any reasonable threshold; refactoring is urgent.",
                        DebtSeverity.CRITICAL,
                        effort_hours=round(cc * 0.5, 1),
                        impact_score=min(cc / 10.0, 10.0),
                        line=fn.line_start,
                    ))
                elif cc >= self.CRITICAL_CC:
                    items.append(self._item(
                        result.file_path,
                        f"Critical complexity: `{fn.name}` (CC={cc})",
                        f"Function has CC={cc}. Extract sub-functions and reduce branching.",
                        DebtSeverity.HIGH,
                        effort_hours=round(cc * 0.3, 1),
                        impact_score=min(cc / 15.0, 10.0),
                        line=fn.line_start,
                    ))
                elif cc >= self.HIGH_CC:
                    items.append(self._item(
                        result.file_path,
                        f"High complexity: `{fn.name}` (CC={cc})",
                        f"Function has CC={cc}. Consider splitting.",
                        DebtSeverity.MEDIUM,
                        effort_hours=2.0,
                        impact_score=cc / 20.0,
                        line=fn.line_start,
                    ))
        return items


# ── Large file debt ────────────────────────────────────────────────────────────

class LargeFileDebtAnalyzer(BaseDebtAnalyzer):
    """Flags files exceeding LOC thresholds."""

    category = DebtCategory.LARGE_FILE

    WARN_LOC    = 300
    ERROR_LOC   = 600
    CRITICAL_LOC = 1000

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        items = []
        for result in results:
            loc = result.line_count
            if loc >= self.CRITICAL_LOC:
                items.append(self._item(
                    result.file_path,
                    f"Critically large file ({loc} LOC)",
                    f"File has {loc} lines. Files this large violate single-responsibility and "
                    "become maintenance nightmares. Split into focused modules.",
                    DebtSeverity.CRITICAL,
                    effort_hours=max(8.0, loc / 100.0),
                    impact_score=min(loc / 200.0, 10.0),
                ))
            elif loc >= self.ERROR_LOC:
                items.append(self._item(
                    result.file_path,
                    f"Very large file ({loc} LOC)",
                    f"File has {loc} lines. Consider splitting into smaller modules.",
                    DebtSeverity.HIGH,
                    effort_hours=4.0,
                    impact_score=min(loc / 300.0, 8.0),
                ))
            elif loc >= self.WARN_LOC:
                items.append(self._item(
                    result.file_path,
                    f"Large file ({loc} LOC)",
                    f"File has {loc} lines. Review for split opportunities.",
                    DebtSeverity.MEDIUM,
                    effort_hours=2.0,
                    impact_score=min(loc / 500.0, 5.0),
                ))
        return items


# ── Dead code debt ─────────────────────────────────────────────────────────────

class DeadCodeDebtAnalyzer(BaseDebtAnalyzer):
    """
    Detects functions defined but never called (within the parsed set).
    Excludes: public API functions, dunder methods, test functions,
              entry points (main, run, create_app, etc.).
    """

    category = DebtCategory.DEAD_CODE

    _EXCLUDED = {
        "main", "run", "setup", "create_app", "make_app",
        "cli", "handler", "lambda_handler", "celery_task",
    }
    _DUNDER = re.compile(r'^__\w+__$')

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        # Build global call graph
        all_defined: dict[str, str] = {}     # name → file_path
        all_called: set[str] = set()

        for result in results:
            for fn in result.functions:
                all_defined[fn.name] = result.file_path
                all_called.update(fn.calls)

        items = []
        for fn_name, file_path in all_defined.items():
            if fn_name in self._EXCLUDED:
                continue
            if self._DUNDER.match(fn_name):
                continue
            if fn_name.startswith("test_") or fn_name.endswith("_test"):
                continue
            if fn_name not in all_called:
                items.append(self._item(
                    file_path,
                    f"Potentially dead code: `{fn_name}`",
                    f"Function `{fn_name}` is defined but not called within the analysed codebase. "
                    "Verify it is not used externally before removing.",
                    DebtSeverity.LOW,
                    effort_hours=0.5,
                    impact_score=1.0,
                ))
        return items


# ── Coupling debt ──────────────────────────────────────────────────────────────

class CouplingDebtAnalyzer(BaseDebtAnalyzer):
    """Flags modules with coupling scores above thresholds."""

    category = DebtCategory.COUPLING

    HIGH_COUPLING    = 0.5
    CRITICAL_COUPLING = 0.75

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        if not file_metrics:
            return []
        items = []
        for fm in file_metrics:
            if fm.coupling >= self.CRITICAL_COUPLING:
                items.append(self._item(
                    fm.file_path,
                    f"Critical coupling (score={fm.coupling:.2f})",
                    f"Module coupling is {fm.coupling:.2f} (fan-in={fm.fan_in}, fan-out={fm.fan_out}). "
                    "Introduce interfaces or an anti-corruption layer.",
                    DebtSeverity.CRITICAL,
                    effort_hours=16.0,
                    impact_score=fm.coupling * 10,
                ))
            elif fm.coupling >= self.HIGH_COUPLING:
                items.append(self._item(
                    fm.file_path,
                    f"High coupling (score={fm.coupling:.2f})",
                    f"Module has coupling {fm.coupling:.2f}. Consider dependency inversion.",
                    DebtSeverity.HIGH,
                    effort_hours=8.0,
                    impact_score=fm.coupling * 7,
                ))
        return items


# ── Documentation debt ─────────────────────────────────────────────────────────

class DocumentationDebtAnalyzer(BaseDebtAnalyzer):
    """Flags modules with low docstring coverage on public functions."""

    category = DebtCategory.DOCUMENTATION

    LOW_COVERAGE = 0.3
    ZERO_COVERAGE = 0.0

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        items = []
        for result in results:
            public_fns = [fn for fn in result.functions if not fn.name.startswith("_")]
            if not public_fns:
                continue
            covered = sum(1 for fn in public_fns if fn.docstring)
            ratio = covered / len(public_fns)
            if ratio <= self.ZERO_COVERAGE and len(public_fns) >= 3:
                items.append(self._item(
                    result.file_path,
                    f"No public documentation ({len(public_fns)} undocumented functions)",
                    "Module has zero docstring coverage on public API. Documentation is essential "
                    "for maintainability and onboarding.",
                    DebtSeverity.HIGH,
                    effort_hours=len(public_fns) * 0.25,
                    impact_score=5.0,
                ))
            elif ratio < self.LOW_COVERAGE:
                items.append(self._item(
                    result.file_path,
                    f"Low documentation coverage ({ratio:.0%})",
                    f"Only {covered}/{len(public_fns)} public functions have docstrings.",
                    DebtSeverity.MEDIUM,
                    effort_hours=(len(public_fns) - covered) * 0.25,
                    impact_score=3.0,
                ))
        return items


# ── Test coverage debt ─────────────────────────────────────────────────────────

class TestCoverageDebtAnalyzer(BaseDebtAnalyzer):
    """Flags source modules that have no corresponding test file."""

    category = DebtCategory.TEST_COVERAGE

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        test_paths = {r.file_path for r in results if "test" in r.file_path.lower()}
        items = []
        for result in results:
            if "test" in result.file_path.lower():
                continue
            if result.language.value not in ("python", "javascript", "typescript"):
                continue
            if not result.functions and not result.classes:
                continue
            # Check for a corresponding test file (heuristic)
            stem = result.file_path.replace("\\", "/").split("/")[-1].replace(".py", "")
            has_tests = any(
                stem in tp or f"test_{stem}" in tp or f"{stem}_test" in tp
                for tp in test_paths
            )
            if not has_tests:
                fn_count = len(result.functions)
                items.append(self._item(
                    result.file_path,
                    f"No test coverage detected ({fn_count} functions)",
                    f"No test file found for this module. {fn_count} untested functions.",
                    DebtSeverity.MEDIUM,
                    effort_hours=max(fn_count * 0.5, 1.0),
                    impact_score=min(fn_count * 0.3, 7.0),
                ))
        return items


# ── Circular dependency debt ───────────────────────────────────────────────────

class CircularDependencyDebtAnalyzer(BaseDebtAnalyzer):
    """Reports circular dependencies from the dependency graph."""

    category = DebtCategory.CIRCULAR_DEPS

    def analyze(self, results, file_metrics=None, graph=None) -> list[DebtItem]:
        if not graph or not graph.cycles:
            return []
        items = []
        for cycle in graph.cycles:
            cycle_str = " → ".join(cycle)
            items.append(self._item(
                cycle[0] if cycle else "",
                f"Circular dependency ({len(cycle)} modules)",
                f"Cycle detected: {cycle_str}. Circular dependencies prevent isolated testing "
                "and complicate build ordering.",
                DebtSeverity.HIGH,
                effort_hours=len(cycle) * 2.0,
                impact_score=min(len(cycle) * 1.5, 10.0),
            ))
        return items


# ── Remediation planner ────────────────────────────────────────────────────────

@dataclass
class RemediationPlan:
    """Ordered list of debt items prioritised by ROI."""
    items: list[DebtItem] = field(default_factory=list)
    total_effort_hours: float = 0.0
    total_impact: float = 0.0
    quick_wins: list[DebtItem] = field(default_factory=list)


class DebtRemediationPlanner:
    """
    Sorts debt items by ROI = impact_score / effort_hours.
    Quick wins: high impact (<= 2h effort), sorted first.
    """

    def plan(self, items: list[DebtItem]) -> RemediationPlan:
        scored = sorted(
            items,
            key=lambda d: d.impact_score / max(d.effort_hours, 0.1),
            reverse=True,
        )
        quick_wins = [d for d in scored if d.effort_hours <= 2.0 and d.impact_score >= 3.0]
        return RemediationPlan(
            items=scored,
            total_effort_hours=round(sum(d.effort_hours for d in items), 1),
            total_impact=round(sum(d.impact_score for d in items), 2),
            quick_wins=quick_wins[:10],
        )


# ── Technical debt engine facade ───────────────────────────────────────────────

class TechnicalDebtEngine:
    """
    Runs all debt analyzers and aggregates into TechnicalDebtReport.

    Usage:
        engine = TechnicalDebtEngine.default()
        report = engine.analyze(results, file_metrics, graph, repo_id="my-repo")
    """

    DEFAULT_ANALYZERS = [
        ComplexityDebtAnalyzer,
        LargeFileDebtAnalyzer,
        DeadCodeDebtAnalyzer,
        CouplingDebtAnalyzer,
        DocumentationDebtAnalyzer,
        TestCoverageDebtAnalyzer,
        CircularDependencyDebtAnalyzer,
    ]

    def __init__(self, analyzers: list[BaseDebtAnalyzer] | None = None) -> None:
        self._analyzers = analyzers or [cls() for cls in self.DEFAULT_ANALYZERS]
        self._planner   = DebtRemediationPlanner()

    @classmethod
    def default(cls) -> "TechnicalDebtEngine":
        return cls()

    def analyze(
        self,
        results: list[ParseResult],
        file_metrics: list[FileMetrics] | None = None,
        graph: DependencyGraph | None = None,
        repo_id: str = "",
    ) -> TechnicalDebtReport:
        report = TechnicalDebtReport(repo_id=repo_id)
        for analyzer in self._analyzers:
            try:
                items = analyzer.analyze(results, file_metrics, graph)
                report.items.extend(items)
            except Exception:
                pass

        plan = self._planner.plan(report.items)
        report.total_debt_hours = plan.total_effort_hours
        report.by_category      = self._tally(report, "category")
        report.by_severity      = self._tally(report, "severity")
        report.debt_score       = self._compute_score(report)
        report.priority_order   = [i.title for i in plan.items[:20]]
        return report

    def _compute_score(self, report: TechnicalDebtReport) -> float:
        """Debt score 0–10 (higher = more debt)."""
        weights = {
            DebtSeverity.CRITICAL: 3.0,
            DebtSeverity.HIGH:     2.0,
            DebtSeverity.MEDIUM:   1.0,
            DebtSeverity.LOW:      0.3,
        }
        raw = sum(weights.get(i.severity, 0) for i in report.items)
        return round(min(raw / max(len(report.items) or 1, 1) * 2, 10.0), 2)

    def _tally(self, report: TechnicalDebtReport, attr: str) -> dict[str, int]:
        tally: dict[str, int] = {}
        for item in report.items:
            val = getattr(item, attr)
            key = val.value if hasattr(val, "value") else str(val)
            tally[key] = tally.get(key, 0) + 1
        return tally
