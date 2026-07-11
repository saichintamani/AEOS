"""
Software Intelligence Platform — Search Engine
===============================================
Multi-modal search across code, issues, PRs, documentation, and symbols.

Search modes:
  1. Semantic search   — natural language → embedding cosine similarity
  2. Symbol search     — exact / fuzzy function / class name lookup
  3. Dependency search — "what imports module X?" / "what does X import?"
  4. Pattern search    — regex over source content
  5. Documentation search — keyword search in docstrings
  6. Cross-entity search  — "find issues related to file X"

Architecture:
  BaseSearchIndex   → one index per search mode
  SearchEngine      → facade with unified search(), filtered by mode
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import (
    DependencyGraph, IssueRecord, ParseResult,
    PullRequestRecord, SearchResult,
)


# ── Result type already in schemas ────────────────────────────────────────────
# SearchResult(result_id, entity_type, file_path, label, score, snippet, metadata)


# ── Abstract index ─────────────────────────────────────────────────────────────

class BaseSearchIndex(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def search(self, query: str, top_k: int) -> list[SearchResult]: ...


# ── Symbol index ───────────────────────────────────────────────────────────────

class SymbolIndex(BaseSearchIndex):
    """
    Fast lookup for function and class names.
    Supports exact match and prefix / substring search.
    """

    name = "symbol"

    def __init__(self) -> None:
        # (name, file_path, entity_type, line) tuples
        self._symbols: list[tuple[str, str, str, int]] = []

    def build(self, results: list[ParseResult]) -> None:
        for result in results:
            for fn in result.functions:
                self._symbols.append((fn.name, result.file_path, "function", fn.line_start))
            for cls in result.classes:
                self._symbols.append((cls.name, result.file_path, "class", cls.line_start))

    def search(self, query: str, top_k: int = 20) -> list[SearchResult]:
        q_lower = query.lower()
        exact, prefix, contains = [], [], []
        for name, fp, kind, line in self._symbols:
            nl = name.lower()
            if nl == q_lower:
                exact.append((name, fp, kind, line, 1.0))
            elif nl.startswith(q_lower):
                prefix.append((name, fp, kind, line, 0.8))
            elif q_lower in nl:
                contains.append((name, fp, kind, line, 0.5))

        ranked = exact + prefix + contains
        return [
            SearchResult(
                result_id=f"{kind}:{fp}:{name}:{line}",
                entity_type=kind,
                file_path=fp,
                label=name,
                score=score,
                snippet=f"def {name}(...)  # line {line}",
                metadata={"line": line},
            )
            for name, fp, kind, line, score in ranked[:top_k]
        ]


# ── Docstring / documentation index ───────────────────────────────────────────

class DocstringIndex(BaseSearchIndex):
    """Keyword search across function and class docstrings."""

    name = "documentation"

    def __init__(self) -> None:
        self._docs: list[tuple[str, str, str, str, int]] = []
        # (docstring, name, file_path, entity_type, line)

    def build(self, results: list[ParseResult]) -> None:
        for result in results:
            for fn in result.functions:
                if fn.docstring:
                    self._docs.append((
                        fn.docstring.lower(), fn.name,
                        result.file_path, "function", fn.line_start,
                    ))
            for cls in result.classes:
                if cls.docstring:
                    self._docs.append((
                        cls.docstring.lower(), cls.name,
                        result.file_path, "class", cls.line_start,
                    ))

    def search(self, query: str, top_k: int = 20) -> list[SearchResult]:
        tokens = query.lower().split()
        scored = []
        for doc, name, fp, kind, line in self._docs:
            hits = sum(1 for t in tokens if t in doc)
            if hits:
                scored.append((hits / len(tokens), name, fp, kind, line, doc))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            SearchResult(
                result_id=f"{kind}:{fp}:{name}:{line}",
                entity_type=kind,
                file_path=fp,
                label=name,
                score=round(score, 4),
                snippet=doc[:120],
                metadata={"line": line},
            )
            for score, name, fp, kind, line, doc in scored[:top_k]
        ]


# ── Dependency index ───────────────────────────────────────────────────────────

class DependencySearchIndex(BaseSearchIndex):
    """
    Answers:
      - "who imports X?"  (reverse dependency lookup)
      - "what does X import?" (forward dependency lookup)
    Query format: "imports:<module>" or "imported_by:<module>"
    """

    name = "dependency"

    def __init__(self) -> None:
        self._forward:  dict[str, list[str]] = {}   # source → [targets]
        self._backward: dict[str, list[str]] = {}   # target → [sources]

    def build(self, graph: DependencyGraph) -> None:
        for edge in graph.edges:
            self._forward.setdefault(edge.source, []).append(edge.target)
            self._backward.setdefault(edge.target, []).append(edge.source)

    def search(self, query: str, top_k: int = 20) -> list[SearchResult]:
        results = []
        if query.startswith("imports:"):
            target = query[8:].strip()
            sources = self._backward.get(target, [])
            for src in sources[:top_k]:
                results.append(SearchResult(
                    result_id=f"dep:{src}:{target}",
                    entity_type="module",
                    file_path=src,
                    label=src,
                    score=1.0,
                    snippet=f"{src} imports {target}",
                    metadata={"relation": "imports", "target": target},
                ))
        elif query.startswith("imported_by:"):
            source = query[12:].strip()
            targets = self._forward.get(source, [])
            for tgt in targets[:top_k]:
                results.append(SearchResult(
                    result_id=f"dep:{source}:{tgt}",
                    entity_type="module",
                    file_path=tgt,
                    label=tgt,
                    score=1.0,
                    snippet=f"{source} is imported by {tgt}",
                    metadata={"relation": "imported_by", "source": source},
                ))
        else:
            # Fuzzy module name search
            q = query.lower()
            for fp in list(self._forward) + list(self._backward):
                if q in fp.lower():
                    results.append(SearchResult(
                        result_id=f"module:{fp}",
                        entity_type="module",
                        file_path=fp,
                        label=fp,
                        score=0.7,
                        snippet=f"Module: {fp}",
                    ))
        return results[:top_k]


# ── Regex pattern index ────────────────────────────────────────────────────────

class PatternSearchIndex(BaseSearchIndex):
    """Regex search over stored raw source content."""

    name = "pattern"

    def __init__(self) -> None:
        self._files: list[tuple[str, str]] = []   # (file_path, content)

    def build(self, files: list[tuple[str, str]]) -> None:
        self._files.extend(files)

    def search(self, query: str, top_k: int = 20) -> list[SearchResult]:
        try:
            pattern = re.compile(query, re.MULTILINE)
        except re.error:
            pattern = re.compile(re.escape(query), re.MULTILINE)

        results = []
        for fp, content in self._files:
            for m in pattern.finditer(content):
                lineno = content[:m.start()].count("\n") + 1
                results.append(SearchResult(
                    result_id=f"pattern:{fp}:{lineno}",
                    entity_type="code_pattern",
                    file_path=fp,
                    label=f"{fp}:{lineno}",
                    score=1.0,
                    snippet=m.group()[:120],
                    metadata={"line": lineno},
                ))
                if len(results) >= top_k:
                    return results
        return results


# ── Issue & PR text index ──────────────────────────────────────────────────────

class IssueTextIndex(BaseSearchIndex):
    """Full-text keyword search over issues and PRs."""

    name = "issue_text"

    def __init__(self) -> None:
        self._docs: list[tuple[str, str, str, str]] = []
        # (text, label, entity_type, entity_id)

    def build(self, issues: list[IssueRecord], prs: list[PullRequestRecord]) -> None:
        for issue in issues:
            self._docs.append((
                f"{issue.title} {issue.body}".lower(),
                f"#{issue.number}: {issue.title}",
                "issue",
                issue.issue_id,
            ))
        for pr in prs:
            self._docs.append((
                f"{pr.title} {pr.body}".lower(),
                f"PR #{pr.number}: {pr.title}",
                "pr",
                pr.pr_id,
            ))

    def search(self, query: str, top_k: int = 20) -> list[SearchResult]:
        tokens = query.lower().split()
        scored = []
        for text, label, kind, eid in self._docs:
            hits = sum(1 for t in tokens if t in text)
            if hits:
                scored.append((hits / len(tokens), label, kind, eid))
        scored.sort(reverse=True)
        return [
            SearchResult(
                result_id=f"{kind}:{eid}",
                entity_type=kind,
                file_path="",
                label=label,
                score=round(score, 4),
                snippet=label,
            )
            for score, label, kind, eid in scored[:top_k]
        ]


# ── Search engine facade ────────────────────────────────────────────────────────

class SearchEngine:
    """
    Unified search across all indices.

    Usage:
        engine = SearchEngine()
        engine.build(results=parse_results, graph=dep_graph, issues=issues, prs=prs)
        results = engine.search("authentication token validation", top_k=10)
        results = engine.search("imports:auth/middleware.py", mode="dependency")
    """

    def __init__(self) -> None:
        self._symbol   = SymbolIndex()
        self._docs     = DocstringIndex()
        self._deps     = DependencySearchIndex()
        self._pattern  = PatternSearchIndex()
        self._issues   = IssueTextIndex()
        self._semantic_engine: Any = None   # CodeEmbeddingEngine, injected

        self._indices: dict[str, BaseSearchIndex] = {
            "symbol":       self._symbol,
            "documentation": self._docs,
            "dependency":   self._deps,
            "pattern":      self._pattern,
            "issue_text":   self._issues,
        }

    def build(
        self,
        results: list[ParseResult] | None = None,
        graph: DependencyGraph | None = None,
        issues: list[IssueRecord] | None = None,
        prs: list[PullRequestRecord] | None = None,
        raw_files: list[tuple[str, str]] | None = None,
    ) -> "SearchEngine":
        if results:
            self._symbol.build(results)
            self._docs.build(results)
        if graph:
            self._deps.build(graph)
        if raw_files:
            self._pattern.build(raw_files)
        if issues or prs:
            self._issues.build(issues or [], prs or [])
        return self

    def attach_semantic(self, engine: Any) -> "SearchEngine":
        """Attach a CodeEmbeddingEngine for semantic search mode."""
        self._semantic_engine = engine
        return self

    def search(
        self,
        query: str,
        top_k: int = 10,
        mode: str = "all",
    ) -> list[SearchResult]:
        """
        mode: "all" | "symbol" | "documentation" | "dependency" |
              "pattern" | "issue_text" | "semantic"
        """
        if mode == "semantic" and self._semantic_engine:
            return self._semantic_engine.search(query, top_k=top_k)

        if mode != "all" and mode in self._indices:
            return self._indices[mode].search(query, top_k=top_k)

        # Merge all indices
        raw: list[SearchResult] = []
        for index in self._indices.values():
            try:
                raw.extend(index.search(query, top_k=top_k))
            except Exception:
                pass
        if self._semantic_engine:
            try:
                raw.extend(self._semantic_engine.search(query, top_k=top_k))
            except Exception:
                pass

        # Deduplicate by result_id, keep highest score
        seen: dict[str, SearchResult] = {}
        for r in raw:
            if r.result_id not in seen or r.score > seen[r.result_id].score:
                seen[r.result_id] = r
        ranked = sorted(seen.values(), key=lambda r: r.score, reverse=True)
        return ranked[:top_k]

    def search_by_file(self, file_path: str, top_k: int = 20) -> list[SearchResult]:
        """Return all indexed entities belonging to the given file."""
        stem = file_path.replace("\\", "/").split("/")[-1]
        return self.search(stem, top_k=top_k, mode="symbol")

    def related_issues(self, file_path: str, top_k: int = 10) -> list[SearchResult]:
        """Issues that mention the given file or its module name."""
        stem = file_path.replace("\\", "/").split("/")[-1].replace(".py", "")
        return self.search(stem, top_k=top_k, mode="issue_text")
