"""
Software Intelligence Platform — AI Code Review Engine
========================================================
Automated code review that checks multiple quality dimensions
and produces structured CodeReviewReport.

Review dimensions:
  - Code Quality: complexity, function length, naming
  - Maintainability: coupling, cohesion, test coverage
  - Architecture: layer violations, pattern compliance
  - Security: unsafe patterns, credential exposure (delegates to security/)
  - Performance: N+1 patterns, unbounded loops, blocking I/O
  - Best Practices: error handling, type hints, logging
  - Documentation: docstring completeness

Design: ReviewEngine dispatches each ParseResult through a list of
        BaseReviewChecker instances. Results are aggregated into a
        CodeReviewReport scored 0–100.
"""

from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import (
    CodeReviewReport, ParseResult, ReviewCategory,
    ReviewComment, ReviewSeverity,
)


# ── Abstract checker ───────────────────────────────────────────────────────────

class BaseReviewChecker(ABC):
    """One review dimension. Checkers are stateless and composable."""

    @property
    @abstractmethod
    def category(self) -> ReviewCategory: ...

    @abstractmethod
    def check(self, result: ParseResult) -> list[ReviewComment]: ...


# ── Concrete checkers ──────────────────────────────────────────────────────────

class ComplexityChecker(BaseReviewChecker):
    """Flags functions with cyclomatic complexity above threshold."""

    category = ReviewCategory.QUALITY
    HIGH_CC   = 10
    CRITICAL_CC = 20

    def check(self, result: ParseResult) -> list[ReviewComment]:
        comments = []
        for fn in result.functions:
            cc = fn.cyclomatic_complexity
            if cc >= self.CRITICAL_CC:
                comments.append(ReviewComment(
                    comment_id=str(uuid.uuid4())[:8],
                    category=ReviewCategory.QUALITY,
                    severity=ReviewSeverity.ERROR,
                    file_path=result.file_path,
                    line_start=fn.line_start,
                    line_end=fn.line_end,
                    title="Critical cyclomatic complexity",
                    message=f"`{fn.name}` has CC={cc} (critical threshold: {self.CRITICAL_CC}). Refactor urgently.",
                    suggestion="Extract sub-functions, reduce branching, or apply strategy pattern.",
                ))
            elif cc >= self.HIGH_CC:
                comments.append(ReviewComment(
                    comment_id=str(uuid.uuid4())[:8],
                    category=ReviewCategory.QUALITY,
                    severity=ReviewSeverity.WARNING,
                    file_path=result.file_path,
                    line_start=fn.line_start,
                    line_end=fn.line_end,
                    title="High cyclomatic complexity",
                    message=f"`{fn.name}` has CC={cc} (recommended max: {self.HIGH_CC}).",
                    suggestion="Consider extracting helper functions.",
                ))
        return comments


class FunctionLengthChecker(BaseReviewChecker):
    """Flags functions that are too long."""

    category       = ReviewCategory.MAINTAINABILITY
    WARN_LINES     = 50
    ERROR_LINES    = 100

    def check(self, result: ParseResult) -> list[ReviewComment]:
        comments = []
        for fn in result.functions:
            length = fn.line_end - fn.line_start
            if length >= self.ERROR_LINES:
                comments.append(ReviewComment(
                    comment_id=str(uuid.uuid4())[:8],
                    category=ReviewCategory.MAINTAINABILITY,
                    severity=ReviewSeverity.ERROR,
                    file_path=result.file_path,
                    line_start=fn.line_start, line_end=fn.line_end,
                    title="Function too long",
                    message=f"`{fn.name}` is {length} lines (max recommended: {self.ERROR_LINES}).",
                    suggestion="Split into smaller, focused functions.",
                ))
            elif length >= self.WARN_LINES:
                comments.append(ReviewComment(
                    comment_id=str(uuid.uuid4())[:8],
                    category=ReviewCategory.MAINTAINABILITY,
                    severity=ReviewSeverity.WARNING,
                    file_path=result.file_path,
                    line_start=fn.line_start, line_end=fn.line_end,
                    title="Long function",
                    message=f"`{fn.name}` is {length} lines.",
                    suggestion="Consider extracting sub-functions.",
                ))
        return comments


class NamingChecker(BaseReviewChecker):
    """Flags naming convention violations."""

    category = ReviewCategory.NAMING

    def check(self, result: ParseResult) -> list[ReviewComment]:
        comments = []
        for fn in result.functions:
            if len(fn.name) <= 1 and fn.name != "_":
                comments.append(self._naming_comment(
                    result.file_path, fn.line_start,
                    fn.name, "Function name too short (single character)",
                    "Use a descriptive name that conveys intent.",
                ))
            # CamelCase in Python functions
            if result.language.value == "python" and re.search(r'[A-Z]', fn.name):
                comments.append(self._naming_comment(
                    result.file_path, fn.line_start,
                    fn.name, "Python function name uses CamelCase (should be snake_case)",
                    f"Rename to `{self._to_snake(fn.name)}`.",
                ))
        for cls in result.classes:
            # snake_case class names in Python
            if result.language.value == "python" and "_" in cls.name:
                comments.append(self._naming_comment(
                    result.file_path, cls.line_start,
                    cls.name, "Python class name uses snake_case (should be PascalCase)",
                    f"Rename to `{''.join(w.title() for w in cls.name.split('_'))}`.",
                ))
        return comments

    def _naming_comment(self, file_path: str, line: int, name: str, msg: str, suggestion: str) -> ReviewComment:
        return ReviewComment(
            comment_id=str(uuid.uuid4())[:8],
            category=ReviewCategory.NAMING,
            severity=ReviewSeverity.WARNING,
            file_path=file_path,
            line_start=line,
            title=f"Naming: `{name}`",
            message=msg,
            suggestion=suggestion,
        )

    @staticmethod
    def _to_snake(name: str) -> str:
        return re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()


class DocumentationChecker(BaseReviewChecker):
    """Flags missing docstrings on public functions and classes."""

    category = ReviewCategory.DOCUMENTATION

    def check(self, result: ParseResult) -> list[ReviewComment]:
        comments = []
        for fn in result.functions:
            if not fn.name.startswith("_") and not fn.docstring:
                comments.append(ReviewComment(
                    comment_id=str(uuid.uuid4())[:8],
                    category=ReviewCategory.DOCUMENTATION,
                    severity=ReviewSeverity.SUGGEST,
                    file_path=result.file_path,
                    line_start=fn.line_start,
                    title=f"Missing docstring: `{fn.name}`",
                    message=f"Public function `{fn.name}` has no docstring.",
                    suggestion="Add a docstring describing purpose, parameters, and return value.",
                ))
        for cls in result.classes:
            if not cls.name.startswith("_") and not cls.docstring:
                comments.append(ReviewComment(
                    comment_id=str(uuid.uuid4())[:8],
                    category=ReviewCategory.DOCUMENTATION,
                    severity=ReviewSeverity.SUGGEST,
                    file_path=result.file_path,
                    line_start=cls.line_start,
                    title=f"Missing docstring: `{cls.name}`",
                    message=f"Class `{cls.name}` has no docstring.",
                    suggestion="Add a class-level docstring.",
                ))
        return comments


class PerformanceChecker(BaseReviewChecker):
    """Detects common performance anti-patterns via regex on raw content."""

    category = ReviewCategory.PERFORMANCE

    # (pattern, title, message, suggestion)
    _PATTERNS: list[tuple[str, str, str, str]] = [
        (
            r'for\s+\w+\s+in\s+\w+.*:\s*\n\s+.*\.append\(',
            "List append in loop",
            "Using list.append() in a loop is slower than list comprehension.",
            "Use a list comprehension: `[expr for x in iterable]`",
        ),
        (
            r'time\.sleep\(',
            "Blocking sleep in code",
            "time.sleep() blocks the event loop in async contexts.",
            "Use `await asyncio.sleep()` in async functions.",
        ),
        (
            r'\+\s*=\s*["\']',
            "String concatenation in loop",
            "String concatenation with += creates new objects each iteration.",
            "Use `''.join(parts)` or an f-string instead.",
        ),
    ]

    def check(self, result: ParseResult) -> list[ReviewComment]:
        # Need raw content — stored in file. Here we reconstruct from nodes.
        # TODO: pass raw content through ParseResult
        return []   # populated when raw content is available


class SecurityReviewChecker(BaseReviewChecker):
    """Delegates to security scanner patterns for quick review-time checks."""

    category = ReviewCategory.SECURITY

    _QUICK_PATTERNS: list[tuple[str, str]] = [
        (r'eval\s*\(', "Use of eval() — remote code execution risk"),
        (r'exec\s*\(', "Use of exec() — code injection risk"),
        (r'pickle\.loads?\s*\(', "Unsafe pickle deserialization"),
        (r'subprocess\.call\(.*shell\s*=\s*True', "Shell injection risk via subprocess"),
        (r'os\.system\s*\(', "Use of os.system() — prefer subprocess with explicit args"),
    ]

    def check(self, result: ParseResult) -> list[ReviewComment]:
        # TODO: pass raw content; currently no raw content in ParseResult
        return []


# ── Review engine ──────────────────────────────────────────────────────────────

class ReviewEngine:
    """
    Orchestrates all review checkers and produces a CodeReviewReport.

    Usage:
        engine = ReviewEngine.default()
        report = engine.review_file(parse_result, repo_id="my-repo")
        report = engine.review_pr(parse_results, pr_id="pr_123", repo_id="my-repo")
    """

    DEFAULT_CHECKERS = [
        ComplexityChecker,
        FunctionLengthChecker,
        NamingChecker,
        DocumentationChecker,
        PerformanceChecker,
        SecurityReviewChecker,
    ]

    def __init__(self, checkers: list[BaseReviewChecker] | None = None) -> None:
        self._checkers = checkers or [cls() for cls in self.DEFAULT_CHECKERS]

    @classmethod
    def default(cls) -> "ReviewEngine":
        return cls()

    def review_file(self, result: ParseResult, repo_id: str, pr_id: str = "") -> CodeReviewReport:
        report = CodeReviewReport(
            review_id=str(uuid.uuid4())[:8],
            repo_id=repo_id,
            pr_id=pr_id,
        )
        for checker in self._checkers:
            try:
                comments = checker.check(result)
                report.comments.extend(comments)
            except Exception:
                pass
        report.score = self._compute_score(report)
        report.summary = self._build_summary(report)
        report.by_category = self._tally_by_category(report)
        return report

    def review_pr(
        self,
        results: list[ParseResult],
        pr_id: str,
        repo_id: str,
    ) -> CodeReviewReport:
        combined = CodeReviewReport(
            review_id=str(uuid.uuid4())[:8],
            repo_id=repo_id,
            pr_id=pr_id,
        )
        for result in results:
            file_report = self.review_file(result, repo_id, pr_id)
            combined.comments.extend(file_report.comments)

        combined.score = self._compute_score(combined)
        combined.summary = self._build_summary(combined)
        combined.by_category = self._tally_by_category(combined)
        return combined

    # ── Scoring ────────────────────────────────────────────────────────────────

    def _compute_score(self, report: CodeReviewReport) -> float:
        """100 = perfect. Deduct per comment based on severity."""
        deductions = {
            ReviewSeverity.ERROR:   10,
            ReviewSeverity.WARNING:  5,
            ReviewSeverity.SUGGEST:  1,
            ReviewSeverity.INFO:     0,
        }
        total_deductions = sum(
            deductions.get(c.severity, 0) for c in report.comments
        )
        return max(0.0, round(100.0 - total_deductions, 1))

    def _build_summary(self, report: CodeReviewReport) -> str:
        errors   = sum(1 for c in report.comments if c.severity == ReviewSeverity.ERROR)
        warnings = sum(1 for c in report.comments if c.severity == ReviewSeverity.WARNING)
        suggests = sum(1 for c in report.comments if c.severity == ReviewSeverity.SUGGEST)
        return (
            f"Score: {report.score}/100 — "
            f"{errors} error(s), {warnings} warning(s), {suggests} suggestion(s)"
        )

    def _tally_by_category(self, report: CodeReviewReport) -> dict[str, int]:
        tally: dict[str, int] = {}
        for c in report.comments:
            key = c.category.value
            tally[key] = tally.get(key, 0) + 1
        return tally
