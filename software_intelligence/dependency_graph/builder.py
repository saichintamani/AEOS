"""
Software Intelligence Platform — Dependency Graph Builder
==========================================================
Constructs a directed dependency graph from all ParseResults.

Nodes: module file paths (internal) + package names (external)
Edges: import relationships with kind classification

Analyses:
  - Circular dependency detection (DFS)
  - Unused dependency detection (imported but never called)
  - Fan-in / Fan-out per module
  - Dependency clustering (strongly connected components)
  - External package inventory

The DependencyGraph schema is defined in schemas.py.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from software_intelligence.schemas import (
    Dependency, DependencyGraph, DependencyKind, ParseResult,
)


@dataclass
class DependencyStats:
    """Aggregated stats derived from a DependencyGraph."""
    repo_id:          str
    total_edges:      int              = 0
    circular_count:   int              = 0
    unused_count:     int              = 0
    external_packages: list[str]       = field(default_factory=list)
    most_imported:    list[str]        = field(default_factory=list)   # highest fan-in
    most_importing:   list[str]        = field(default_factory=list)   # highest fan-out
    fan_in:           dict[str, int]   = field(default_factory=dict)   # path → count
    fan_out:          dict[str, int]   = field(default_factory=dict)


class DependencyGraphBuilder:
    """
    Builds a DependencyGraph from a list of ParseResults.

    Usage:
        builder = DependencyGraphBuilder(repo_root="src")
        graph = builder.build(parse_results, repo_id="my-repo")
        stats = builder.compute_stats(graph)
    """

    def __init__(self, repo_root: str = "") -> None:
        self._repo_root = repo_root

    def build(self, results: list[ParseResult], repo_id: str) -> DependencyGraph:
        graph = DependencyGraph(repo_id=repo_id)
        path_set = {r.file_path for r in results}
        module_map = self._build_module_map(results)
        external_pkgs: set[str] = set()
        call_sets: dict[str, set[str]] = {}

        for result in results:
            graph.nodes.append(result.file_path)
            called = {fn_call for fn in result.functions for fn_call in fn.calls}
            call_sets[result.file_path] = called

            for imp in result.imports:
                target = self._resolve_target(imp.module, result.file_path, module_map)
                kind = self._classify_kind(imp, path_set, target)

                if kind == DependencyKind.EXTERNAL:
                    external_pkgs.add(imp.module.split(".")[0])

                edge = Dependency(
                    source=result.file_path,
                    target=target,
                    kind=kind,
                    symbol=", ".join(imp.symbols[:5]) if imp.symbols else "",
                    line=imp.line_start,
                )
                graph.edges.append(edge)

        # Circular dependency detection
        graph.circular = self._detect_cycles(graph)
        for edge in graph.edges:
            edge.is_circular = any(
                edge.source in cycle and edge.target in cycle
                for cycle in graph.circular
            )

        # Unused dependency detection (imported but none of its symbols called)
        for edge in graph.edges:
            if edge.kind == DependencyKind.EXTERNAL:
                continue
            source_calls = call_sets.get(edge.source, set())
            if edge.symbol:
                symbols = [s.strip() for s in edge.symbol.split(",")]
                edge.is_unused = not any(sym in source_calls for sym in symbols)
                if edge.is_unused:
                    graph.unused.append(f"{edge.source} → {edge.target}")

        return graph

    def compute_stats(self, graph: DependencyGraph) -> DependencyStats:
        fan_in:  dict[str, int] = defaultdict(int)
        fan_out: dict[str, int] = defaultdict(int)
        external: set[str] = set()

        for edge in graph.edges:
            fan_out[edge.source] += 1
            if edge.target in graph.nodes:
                fan_in[edge.target] += 1
            if edge.kind == DependencyKind.EXTERNAL:
                external.add(edge.target.split(".")[0])

        most_imported = sorted(fan_in.items(), key=lambda x: x[1], reverse=True)[:10]
        most_importing = sorted(fan_out.items(), key=lambda x: x[1], reverse=True)[:10]

        return DependencyStats(
            repo_id=graph.repo_id,
            total_edges=len(graph.edges),
            circular_count=len(graph.circular),
            unused_count=len(graph.unused),
            external_packages=sorted(external),
            most_imported=[p for p, _ in most_imported],
            most_importing=[p for p, _ in most_importing],
            fan_in=dict(fan_in),
            fan_out=dict(fan_out),
        )

    def export_dot(self, graph: DependencyGraph) -> str:
        """Export graph in DOT format for Graphviz rendering."""
        lines = ["digraph dependencies {"]
        lines.append('  rankdir=LR;')
        lines.append('  node [shape=box];')
        for edge in graph.edges:
            if edge.kind == DependencyKind.INTERNAL:
                src = Path(edge.source).stem
                tgt = Path(edge.target).stem
                color = "red" if edge.is_circular else "black"
                lines.append(f'  "{src}" -> "{tgt}" [color={color}];')
        lines.append("}")
        return "\n".join(lines)

    # ── Internals ──────────────────────────────────────────────────────────────

    def _build_module_map(self, results: list[ParseResult]) -> dict[str, str]:
        """Map Python module names to file paths."""
        mapping: dict[str, str] = {}
        for r in results:
            # e.g. "app/core/config.py" → "app.core.config"
            clean = r.file_path.replace("/", ".").replace("\\", ".").rstrip(".py")
            if clean.endswith(".__init__"):
                clean = clean[:-9]
            mapping[clean] = r.file_path
            mapping[Path(r.file_path).stem] = r.file_path
        return mapping

    def _resolve_target(self, module: str, source_path: str, module_map: dict) -> str:
        """Resolve an import module name to a file path if possible."""
        if module in module_map:
            return module_map[module]
        # Try parent package prefixes
        parts = module.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in module_map:
                return module_map[candidate]
        return module   # external package name

    def _classify_kind(self, imp: Any, path_set: set[str], resolved_target: str) -> DependencyKind:
        if imp.is_stdlib:
            return DependencyKind.STDLIB
        if resolved_target in path_set:
            return DependencyKind.INTERNAL
        if imp.is_relative:
            return DependencyKind.INTERNAL
        return DependencyKind.EXTERNAL

    def _detect_cycles(self, graph: DependencyGraph) -> list[list[str]]:
        """Johnson's algorithm simplified: DFS-based cycle detection."""
        # Build adjacency list for internal edges only
        adj: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            if edge.kind == DependencyKind.INTERNAL:
                adj[edge.source].append(edge.target)

        visited: set[str] = set()
        stack:   set[str] = set()
        cycles:  list[list[str]] = []

        def dfs(node: str, path: list[str]) -> None:
            visited.add(node)
            stack.add(node)
            path.append(node)
            for neighbor in adj.get(node, []):
                if neighbor not in visited:
                    dfs(neighbor, path)
                elif neighbor in stack:
                    # Found a cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:]
                    if cycle not in cycles:
                        cycles.append(list(cycle))
            path.pop()
            stack.discard(node)

        for node in list(adj.keys()):
            if node not in visited:
                dfs(node, [])

        return cycles[:50]   # cap at 50 cycles for large repos
