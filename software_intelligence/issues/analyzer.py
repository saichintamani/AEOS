"""
Software Intelligence Platform — Issue Intelligence
=====================================================
AI-driven analysis of GitHub/GitLab issues.

Capabilities:
  - Summarization: concise one-paragraph summary of issue + context
  - Category classification: bug / feature / enhancement / docs / question
  - Priority estimation: CRITICAL / HIGH / MEDIUM / LOW based on signals
  - Duplicate detection: cosine similarity on TF-IDF embeddings
  - Affected component detection: cross-reference issue text with code paths
  - Label suggestion: based on content and historical label patterns
  - Effort estimation: rough complexity estimate

Design: IssueAnalyzer is the facade.
        Each capability is a composable IssueStage.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import (
    IssueAnalysis, IssueRecord, IssuePriority, ParseResult,
)


# ── Signals for priority ──────────────────────────────────────────────────────

_CRITICAL_SIGNALS = {
    "crash", "data loss", "security", "outage", "down", "broken", "regression",
    "critical", "urgent", "blocker", "production", "exploit", "vulnerability",
}
_HIGH_SIGNALS = {"error", "fail", "exception", "panic", "wrong", "incorrect", "breaks"}
_LOW_SIGNALS = {"typo", "cosmetic", "minor", "style", "enhancement", "would be nice"}

_CATEGORY_SIGNALS: dict[str, list[str]] = {
    "bug":         ["bug", "error", "crash", "fail", "broken", "regression", "wrong", "unexpected"],
    "feature":     ["feature", "request", "add", "implement", "support", "allow", "enable"],
    "enhancement": ["improve", "enhance", "optimize", "better", "performance", "refactor"],
    "docs":        ["documentation", "docs", "readme", "comment", "docstring", "wiki"],
    "question":    ["question", "how", "why", "what", "help", "understand", "confused"],
}


# ── Issue stage ABC ────────────────────────────────────────────────────────────

class BaseIssueStage(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def process(self, issue: IssueRecord, analysis: IssueAnalysis, context: dict) -> None:
        """Mutate analysis in-place."""
        ...


# ── Concrete stages ────────────────────────────────────────────────────────────

class CategorizationStage(BaseIssueStage):
    name = "categorization"

    def process(self, issue: IssueRecord, analysis: IssueAnalysis, context: dict) -> None:
        text = (issue.title + " " + issue.body).lower()
        best_cat, best_score = "question", 0
        for category, signals in _CATEGORY_SIGNALS.items():
            score = sum(1 for s in signals if s in text)
            if score > best_score:
                best_cat, best_score = category, score
        analysis.category = best_cat
        # Suggest labels based on existing labels + category
        suggested = list(issue.labels)
        if best_cat not in suggested:
            suggested.append(best_cat)
        analysis.suggested_labels = suggested[:10]


class PriorityEstimationStage(BaseIssueStage):
    name = "priority_estimation"

    def process(self, issue: IssueRecord, analysis: IssueAnalysis, context: dict) -> None:
        text = (issue.title + " " + issue.body).lower()
        if any(s in text for s in _CRITICAL_SIGNALS):
            analysis.priority = IssuePriority.CRITICAL
        elif any(s in text for s in _HIGH_SIGNALS):
            analysis.priority = IssuePriority.HIGH
        elif any(s in text for s in _LOW_SIGNALS):
            analysis.priority = IssuePriority.LOW
        else:
            analysis.priority = IssuePriority.MEDIUM
        # Boost to CRITICAL if "production" appears
        if "production" in text and analysis.priority not in (IssuePriority.CRITICAL,):
            analysis.priority = IssuePriority.HIGH


class SummarizationStage(BaseIssueStage):
    name = "summarization"

    def process(self, issue: IssueRecord, analysis: IssueAnalysis, context: dict) -> None:
        # Rule-based: take first 2 sentences of body + title
        sentences = re.split(r'(?<=[.!?])\s+', issue.body.strip())
        first_two = " ".join(sentences[:2])[:300]
        analysis.summary = f"**{issue.title}** — {first_two}" if first_two else issue.title

        # LLM override if available
        llm = context.get("llm")
        if llm:
            analysis.summary = llm.summarize(
                f"Issue #{issue.number}: {issue.title}\n\n{issue.body[:1000]}"
            )


class ComponentDetectionStage(BaseIssueStage):
    """Cross-references issue text with known file paths from parse results."""
    name = "component_detection"

    def process(self, issue: IssueRecord, analysis: IssueAnalysis, context: dict) -> None:
        results: list[ParseResult] = context.get("parse_results", [])
        text = (issue.title + " " + issue.body).lower()
        mentioned = []
        for result in results:
            stem = result.file_path.replace("/", ".").lower()
            parts = result.file_path.lower().replace("\\", "/").split("/")
            if any(part in text for part in parts if len(part) > 3):
                mentioned.append(result.file_path)
        analysis.affected_components = list(set(mentioned))[:10]


class EffortEstimationStage(BaseIssueStage):
    name = "effort_estimation"

    def process(self, issue: IssueRecord, analysis: IssueAnalysis, context: dict) -> None:
        text = issue.body.lower()
        word_count = len(text.split())
        component_count = len(analysis.affected_components)
        # Very rough heuristic
        if analysis.category == "bug" and analysis.priority in (IssuePriority.CRITICAL, IssuePriority.HIGH):
            analysis.effort_estimate = "1–3 days"
        elif word_count < 50:
            analysis.effort_estimate = "< 1 hour"
        elif component_count > 3:
            analysis.effort_estimate = "3–5 days"
        else:
            analysis.effort_estimate = "0.5–2 days"


# ── Duplicate detection ────────────────────────────────────────────────────────

class DuplicateDetector:
    """
    Detects duplicate issues using TF-IDF cosine similarity.
    For production: replace with semantic embeddings from the embeddings/ module.
    """

    SIMILARITY_THRESHOLD = 0.7

    def find_duplicates(
        self,
        new_issue: IssueRecord,
        existing_issues: list[IssueRecord],
    ) -> list[tuple[IssueRecord, float]]:
        """Return list of (issue, similarity_score) pairs above threshold."""
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.metrics.pairwise import cosine_similarity
            import numpy as np
        except ImportError:
            return []

        if not existing_issues:
            return []

        corpus = [f"{i.title} {i.body}" for i in existing_issues]
        new_text = f"{new_issue.title} {new_issue.body}"

        vectorizer = TfidfVectorizer(max_features=5000, stop_words="english")
        try:
            tfidf = vectorizer.fit_transform(corpus + [new_text])
            similarities = cosine_similarity(tfidf[-1:], tfidf[:-1])[0]
            return [
                (existing_issues[i], float(similarities[i]))
                for i in range(len(existing_issues))
                if similarities[i] >= self.SIMILARITY_THRESHOLD
            ]
        except Exception:
            return []


# ── Issue analyzer facade ──────────────────────────────────────────────────────

class IssueAnalyzer:
    """
    Facade: runs all issue analysis stages on a batch of issues.

    Usage:
        analyzer = IssueAnalyzer(parse_results=results)
        analyses = analyzer.analyze_batch(issues)
        duplicates = analyzer.find_duplicates(new_issue, existing_issues)
    """

    def __init__(
        self,
        parse_results: list[ParseResult] | None = None,
        llm_backend: Any = None,
        stages: list[BaseIssueStage] | None = None,
    ) -> None:
        self._context = {
            "parse_results": parse_results or [],
            "llm": llm_backend,
        }
        self._stages = stages or [
            CategorizationStage(),
            PriorityEstimationStage(),
            SummarizationStage(),
            ComponentDetectionStage(),
            EffortEstimationStage(),
        ]
        self._dup_detector = DuplicateDetector()

    def analyze(self, issue: IssueRecord) -> IssueAnalysis:
        analysis = IssueAnalysis(issue_id=issue.issue_id)
        for stage in self._stages:
            try:
                stage.process(issue, analysis, self._context)
            except Exception:
                pass
        return analysis

    def analyze_batch(self, issues: list[IssueRecord]) -> list[IssueAnalysis]:
        analyses = [self.analyze(issue) for issue in issues]
        # Cross-issue duplicate detection
        for i, (issue, analysis) in enumerate(zip(issues, analyses)):
            candidates = issues[:i]
            dups = self._dup_detector.find_duplicates(issue, candidates)
            if dups:
                best_match, score = max(dups, key=lambda x: x[1])
                analysis.duplicate_of = best_match.issue_id
                analysis.similarity_score = round(score, 4)
        return analyses

    def find_duplicates(
        self,
        new_issue: IssueRecord,
        existing_issues: list[IssueRecord],
    ) -> list[tuple[IssueRecord, float]]:
        return self._dup_detector.find_duplicates(new_issue, existing_issues)
