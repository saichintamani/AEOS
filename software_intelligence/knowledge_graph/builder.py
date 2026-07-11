"""
Software Intelligence Platform — Knowledge Graph Builder
=========================================================
Constructs the KnowledgeGraph from heterogeneous analysis outputs.

Build stages (all optional, composable):
  1. Module nodes      — one node per ParseResult file
  2. Function nodes    — one node per FunctionNode
  3. Class nodes       — one node per ClassNode
  4. Import edges      — IMPORTS between modules
  5. Call edges        — CALLS between functions (cross-file)
  6. Inheritance edges — INHERITS between classes
  7. DEFINED_IN edges  — function/class → module
  8. Issue nodes       — one node per IssueRecord + REFERENCES edges
  9. PR nodes          — one node per PullRequestRecord + FIXES + REFERENCES
 10. Concept nodes     — extracted from comments/docstrings
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import (
    DependencyGraph, GraphEdge, GraphEdgeKind, GraphNode, GraphNodeKind,
    IssueRecord, ParseResult, PullRequestRecord,
)
from software_intelligence.knowledge_graph.graph import KnowledgeGraph


def _nid(*parts: str) -> str:
    """Stable, compact node ID from concatenated parts."""
    raw = ":".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()[:12]


class KnowledgeGraphBuilder:
    """
    Incrementally builds a KnowledgeGraph.

    Usage:
        builder = KnowledgeGraphBuilder()
        graph = (
            builder
            .add_parse_results(results)
            .add_dependency_graph(dep_graph)
            .add_issues(issues)
            .add_pull_requests(prs)
            .build(repo_id="my-repo")
        )
    """

    def __init__(self) -> None:
        self._results: list[ParseResult]        = []
        self._dep_graph: DependencyGraph | None  = None
        self._issues: list[IssueRecord]          = []
        self._prs: list[PullRequestRecord]       = []

    def add_parse_results(self, results: list[ParseResult]) -> "KnowledgeGraphBuilder":
        self._results.extend(results)
        return self

    def add_dependency_graph(self, graph: DependencyGraph) -> "KnowledgeGraphBuilder":
        self._dep_graph = graph
        return self

    def add_issues(self, issues: list[IssueRecord]) -> "KnowledgeGraphBuilder":
        self._issues.extend(issues)
        return self

    def add_pull_requests(self, prs: list[PullRequestRecord]) -> "KnowledgeGraphBuilder":
        self._prs.extend(prs)
        return self

    def build(self, repo_id: str = "") -> KnowledgeGraph:
        g = KnowledgeGraph(repo_id=repo_id)

        # Maps for cross-stage lookups
        module_id:   dict[str, str] = {}   # file_path → node_id
        function_id: dict[str, str] = {}   # fn.name → node_id (last-wins)
        class_id:    dict[str, str] = {}   # cls.name → node_id (last-wins)

        # ── Stage 1–4: parse results ──────────────────────────────────────────
        for result in self._results:
            fp = result.file_path

            # Module node
            mid = _nid("module", fp)
            module_id[fp] = mid
            g.add_node(GraphNode(
                node_id=mid,
                kind=GraphNodeKind.MODULE,
                label=fp.replace("\\", "/").split("/")[-1],
                file_path=fp,
                metadata={"language": result.language.value, "loc": result.line_count},
            ))

            # Function nodes + DEFINED_IN edges
            for fn in result.functions:
                fid = _nid("fn", fp, fn.name)
                function_id[fn.name] = fid
                g.add_node(GraphNode(
                    node_id=fid,
                    kind=GraphNodeKind.FUNCTION,
                    label=fn.name,
                    file_path=fp,
                    metadata={
                        "cc": fn.cyclomatic_complexity,
                        "async": fn.is_async,
                        "line": fn.line_start,
                    },
                ))
                g.add_edge(GraphEdge(
                    source_id=fid, target_id=mid,
                    kind=GraphEdgeKind.DEFINED_IN,
                ))

            # Class nodes + DEFINED_IN edges
            for cls in result.classes:
                cid = _nid("cls", fp, cls.name)
                class_id[cls.name] = cid
                g.add_node(GraphNode(
                    node_id=cid,
                    kind=GraphNodeKind.CLASS,
                    label=cls.name,
                    file_path=fp,
                    metadata={"bases": cls.bases, "line": cls.line_start},
                ))
                g.add_edge(GraphEdge(
                    source_id=cid, target_id=mid,
                    kind=GraphEdgeKind.DEFINED_IN,
                ))

        # ── Stage 5: IMPORTS edges ────────────────────────────────────────────
        if self._dep_graph:
            for edge in self._dep_graph.edges:
                src = module_id.get(edge.source)
                tgt = module_id.get(edge.target)
                if src and tgt:
                    g.add_edge(GraphEdge(
                        source_id=src, target_id=tgt,
                        kind=GraphEdgeKind.IMPORTS,
                    ))

        # ── Stage 6: CALLS edges ──────────────────────────────────────────────
        for result in self._results:
            for fn in result.functions:
                caller_id = function_id.get(fn.name)
                if not caller_id:
                    continue
                for callee_name in fn.calls:
                    callee_id = function_id.get(callee_name)
                    if callee_id and callee_id != caller_id:
                        g.add_edge(GraphEdge(
                            source_id=caller_id, target_id=callee_id,
                            kind=GraphEdgeKind.CALLS,
                        ))

        # ── Stage 7: INHERITS edges ───────────────────────────────────────────
        for result in self._results:
            for cls in result.classes:
                child_id = class_id.get(cls.name)
                if not child_id:
                    continue
                for base in cls.bases:
                    parent_id = class_id.get(base)
                    if parent_id and parent_id != child_id:
                        g.add_edge(GraphEdge(
                            source_id=child_id, target_id=parent_id,
                            kind=GraphEdgeKind.INHERITS,
                        ))

        # ── Stage 8: Issue nodes + REFERENCES ─────────────────────────────────
        for issue in self._issues:
            iid = _nid("issue", issue.issue_id)
            g.add_node(GraphNode(
                node_id=iid,
                kind=GraphNodeKind.ISSUE,
                label=f"#{issue.number}: {issue.title[:60]}",
                metadata={
                    "issue_id": issue.issue_id,
                    "state": issue.state,
                    "labels": issue.labels,
                },
            ))
            # Cross-reference to mentioned modules
            text = (issue.title + " " + issue.body).lower()
            for fp, mid in module_id.items():
                stem = fp.replace("\\", "/").split("/")[-1].lower()
                if stem.replace(".py", "") in text:
                    g.add_edge(GraphEdge(
                        source_id=iid, target_id=mid,
                        kind=GraphEdgeKind.REFERENCES,
                    ))

        # ── Stage 9: PR nodes ──────────────────────────────────────────────────
        issue_ids = {i.issue_id: _nid("issue", i.issue_id) for i in self._issues}
        for pr in self._prs:
            prid = _nid("pr", pr.pr_id)
            g.add_node(GraphNode(
                node_id=prid,
                kind=GraphNodeKind.PR,
                label=f"PR #{pr.number}: {pr.title[:60]}",
                metadata={"pr_id": pr.pr_id, "state": pr.state},
            ))
            # REFERENCES modified files
            for fp in pr.files_changed:
                mid = module_id.get(fp)
                if mid:
                    g.add_edge(GraphEdge(source_id=prid, target_id=mid, kind=GraphEdgeKind.REFERENCES))
            # FIXES issues (heuristic: "fixes #N" in body)
            for match in re.finditer(r'fixes?\s+#?(\d+)', pr.body, re.I):
                num = match.group(1)
                for issue in self._issues:
                    if str(issue.number) == num:
                        g.add_edge(GraphEdge(
                            source_id=prid,
                            target_id=_nid("issue", issue.issue_id),
                            kind=GraphEdgeKind.FIXES,
                        ))
                        break

        return g
