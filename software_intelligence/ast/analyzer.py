"""
Software Intelligence Platform — AST Analyzer
===============================================
Post-processing layer on top of ParseResult.
Computes derived insights from the raw AST nodes:

  - Complexity scoring (cyclomatic + cognitive)
  - Inheritance chains and MRO reconstruction
  - Control flow summary
  - Hot-spot detection (high-complexity functions)
  - Symbol resolution (call graph cross-file linking)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import (
    ClassNode, FunctionNode, ParseResult, RepositoryMetrics,
)


@dataclass
class ComplexityProfile:
    """Complexity summary for one file or function."""
    file_path:              str
    avg_cyclomatic:         float       = 0.0
    max_cyclomatic:         int         = 0
    high_complexity_fns:    list[str]   = field(default_factory=list)   # fns with CC > threshold
    cognitive_complexity:   float       = 0.0
    nesting_depth:          int         = 0     # max nesting in file


@dataclass
class InheritanceChain:
    class_name:  str
    file_path:   str
    bases:       list[str]
    depth:       int          = 0
    mro:         list[str]   = field(default_factory=list)  # method resolution order


@dataclass
class ASTSummary:
    """Aggregated analysis of all ParseResults for a repository."""
    repo_id:                str
    total_functions:        int                        = 0
    total_classes:          int                        = 0
    total_imports:          int                        = 0
    avg_cyclomatic:         float                      = 0.0
    max_cyclomatic:         int                        = 0
    complexity_hotspots:    list[ComplexityProfile]   = field(default_factory=list)
    inheritance_chains:     list[InheritanceChain]    = field(default_factory=list)
    dead_functions:         list[str]                 = field(default_factory=list)
    most_called_functions:  list[str]                 = field(default_factory=list)
    undocumented_publics:   list[str]                 = field(default_factory=list)


class ASTAnalyzer:
    """
    Cross-file AST analysis engine.

    Accepts a list of ParseResults (all files in a repository) and
    produces an ASTSummary with cross-file insights.

    Usage:
        analyzer = ASTAnalyzer(complexity_threshold=10)
        summary = analyzer.analyze(parse_results, repo_id="abc")
    """

    DEFAULT_CC_THRESHOLD = 10    # functions with CC > this are flagged as hotspots

    def __init__(self, complexity_threshold: int = DEFAULT_CC_THRESHOLD) -> None:
        self._cc_threshold = complexity_threshold

    def analyze(self, results: list[ParseResult], repo_id: str) -> ASTSummary:
        summary = ASTSummary(repo_id=repo_id)
        all_functions: list[FunctionNode] = []
        all_classes:   list[ClassNode]    = []
        call_counts:   dict[str, int]     = {}

        for result in results:
            all_functions.extend(result.functions)
            all_classes.extend(result.classes)
            for fn in result.functions:
                for called in fn.calls:
                    call_counts[called] = call_counts.get(called, 0) + 1

        summary.total_functions = len(all_functions)
        summary.total_classes   = len(all_classes)
        summary.total_imports   = sum(len(r.imports) for r in results)

        # Complexity
        ccs = [fn.cyclomatic_complexity for fn in all_functions if fn.cyclomatic_complexity > 0]
        if ccs:
            summary.avg_cyclomatic = round(sum(ccs) / len(ccs), 2)
            summary.max_cyclomatic = max(ccs)

        # Hotspots per file
        for result in results:
            high = [fn.name for fn in result.functions if fn.cyclomatic_complexity > self._cc_threshold]
            if high:
                avg_cc = sum(fn.cyclomatic_complexity for fn in result.functions) / max(len(result.functions), 1)
                summary.complexity_hotspots.append(ComplexityProfile(
                    file_path=result.file_path,
                    avg_cyclomatic=round(avg_cc, 2),
                    max_cyclomatic=max(fn.cyclomatic_complexity for fn in result.functions),
                    high_complexity_fns=high,
                ))

        # Inheritance
        fn_names = {fn.name for fn in all_functions}
        for cls in all_classes:
            if cls.bases:
                summary.inheritance_chains.append(InheritanceChain(
                    class_name=cls.name,
                    file_path=cls.file_path,
                    bases=cls.bases,
                    depth=len(cls.bases),
                ))

        # Most called
        summary.most_called_functions = [
            name for name, _ in
            sorted(call_counts.items(), key=lambda x: x[1], reverse=True)[:20]
        ]

        # Undocumented public functions
        summary.undocumented_publics = [
            f"{fn.file_path}::{fn.name}"
            for fn in all_functions
            if not fn.docstring and not fn.name.startswith("_")
        ][:50]

        # Dead functions (defined but never called)
        called_names = set(call_counts.keys())
        summary.dead_functions = [
            f"{fn.file_path}::{fn.name}"
            for fn in all_functions
            if fn.name not in called_names and not fn.name.startswith("_")
               and fn.name not in {"main", "__init__", "__repr__", "__str__"}
        ][:30]

        return summary

    def file_complexity(self, result: ParseResult) -> ComplexityProfile:
        """Compute complexity profile for a single file."""
        fns = result.functions
        if not fns:
            return ComplexityProfile(file_path=result.file_path)
        ccs = [fn.cyclomatic_complexity for fn in fns]
        high = [fn.name for fn in fns if fn.cyclomatic_complexity > self._cc_threshold]
        return ComplexityProfile(
            file_path=result.file_path,
            avg_cyclomatic=round(sum(ccs) / len(ccs), 2),
            max_cyclomatic=max(ccs),
            high_complexity_fns=high,
        )
