"""
Software Intelligence Platform — Architecture Reconstructor
============================================================
Automatically infers architectural layers, components, patterns,
entry points, and system boundaries from static analysis.

Detection strategy:
  1. Path heuristics — directory names reveal layers (api/, routes/, db/, models/)
  2. Dependency flow — modules with highest fan-in are likely core/domain
  3. Entry point markers — main.py, app.py, server.py, __main__.py
  4. Pattern recognition — detect MVC, layered, microservice, hexagonal
  5. Boundary detection — externally-facing modules (HTTP, DB, queue, filesystem)

Output: ArchitectureReport (see schemas.py)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from software_intelligence.schemas import (
    ArchitecturalComponent, ArchitectureReport, DependencyGraph,
    LayerType, ParseResult,
)


# ── Layer classification heuristics ───────────────────────────────────────────

_LAYER_RULES: list[tuple[list[str], LayerType]] = [
    # path segments → layer
    (["api", "routes", "router", "endpoints", "views", "handlers", "controllers"], LayerType.API),
    (["ui", "frontend", "web", "templates", "static", "pages", "components"],       LayerType.PRESENTATION),
    (["service", "services", "usecases", "application", "app"],                      LayerType.APPLICATION),
    (["domain", "model", "models", "entity", "entities", "core"],                    LayerType.DOMAIN),
    (["infra", "infrastructure", "adapter", "adapters", "gateway", "gateways"],      LayerType.INFRASTRUCTURE),
    (["db", "database", "repository", "repositories", "store", "storage", "dao"],    LayerType.DATA),
    (["util", "utils", "helpers", "common", "shared", "lib"],                        LayerType.UTILITY),
    (["config", "configuration", "settings", "env"],                                 LayerType.CONFIGURATION),
    (["test", "tests", "spec", "specs", "__tests__", "fixtures"],                    LayerType.TEST),
]

_ENTRY_POINT_NAMES = {
    "main.py", "app.py", "server.py", "run.py", "index.py",
    "manage.py", "__main__.py", "wsgi.py", "asgi.py",
    "main.go", "main.rs", "index.js", "index.ts", "Program.cs",
}

_PATTERN_SIGNATURES = {
    "MVC": lambda paths: (
        any("model" in p.lower() for p in paths) and
        any("view" in p.lower() for p in paths) and
        any("controller" in p.lower() for p in paths)
    ),
    "Layered Architecture": lambda paths: (
        any("domain" in p.lower() for p in paths) and
        any("service" in p.lower() for p in paths) and
        any("repository" in p.lower() for p in paths)
    ),
    "Hexagonal (Ports & Adapters)": lambda paths: (
        any("port" in p.lower() for p in paths) and
        any("adapter" in p.lower() for p in paths)
    ),
    "REST API": lambda paths: (
        any(seg in p.lower() for p in paths for seg in ["routes", "endpoints", "api"])
    ),
    "Microservices": lambda paths: (
        len({p.split("/")[0] for p in paths if "/" in p}) > 5
    ),
}

_BOUNDARY_MARKERS = {
    "HTTP": ["fastapi", "flask", "django", "express", "gin", "actix", "http", "httpx", "requests"],
    "Database": ["sqlalchemy", "pymongo", "psycopg2", "redis", "sqlite3", "motor", "prisma", "diesel"],
    "Queue": ["celery", "kafka", "rabbitmq", "pika", "aio_pika", "nats", "pulsar"],
    "Filesystem": ["pathlib", "os.path", "shutil", "aiofiles"],
    "External API": ["boto3", "stripe", "twilio", "sendgrid", "openai", "anthropic"],
}


class ArchitectureReconstructor:
    """
    Infers software architecture from parse results and dependency graph.

    Usage:
        reconstructor = ArchitectureReconstructor()
        report = reconstructor.reconstruct(
            results=parse_results,
            graph=dependency_graph,
            repo_id="my-repo",
        )
    """

    def reconstruct(
        self,
        results: list[ParseResult],
        graph: DependencyGraph,
        repo_id: str,
    ) -> ArchitectureReport:
        report = ArchitectureReport(repo_id=repo_id)
        paths = [r.file_path for r in results]

        # Entry points
        report.entry_points = [
            p for p in paths
            if Path(p).name in _ENTRY_POINT_NAMES
        ]

        # Layer classification → component grouping
        components = self._classify_components(paths, graph)
        report.components = components

        # Pattern detection
        report.patterns = self._detect_patterns(paths)

        # Boundary detection
        report.boundaries = self._detect_boundaries(results)

        # Architectural smells
        report.smells = self._detect_smells(components, graph)

        return report

    def _classify_components(
        self,
        paths: list[str],
        graph: DependencyGraph,
    ) -> list[ArchitecturalComponent]:
        layer_buckets: dict[LayerType, list[str]] = {layer: [] for layer in LayerType}

        for path in paths:
            layer = self._classify_layer(path)
            layer_buckets[layer].append(path)

        # Fan-in from dependency graph
        fan_in: dict[str, int] = {}
        for edge in graph.edges:
            fan_in[edge.target] = fan_in.get(edge.target, 0) + 1

        components = []
        for layer, layer_paths in layer_buckets.items():
            if not layer_paths:
                continue
            # Group by top-level directory within the layer
            dir_groups: dict[str, list[str]] = {}
            for p in layer_paths:
                top_dir = p.split("/")[0] if "/" in p else "root"
                dir_groups.setdefault(top_dir, []).append(p)

            for dir_name, group_paths in dir_groups.items():
                outbound = list({
                    e.target for e in graph.edges
                    if e.source in group_paths and e.target not in group_paths
                })
                inbound = list({
                    e.source for e in graph.edges
                    if e.target in group_paths and e.source not in group_paths
                })
                entry_pts = [p for p in group_paths if Path(p).name in _ENTRY_POINT_NAMES]
                components.append(ArchitecturalComponent(
                    name=f"{layer.value}::{dir_name}",
                    layer=layer,
                    paths=group_paths,
                    entry_points=entry_pts,
                    outbound=outbound[:10],
                    inbound=inbound[:10],
                    description=f"{layer.value.title()} layer — {len(group_paths)} files",
                ))

        return components

    def _classify_layer(self, path: str) -> LayerType:
        path_lower = path.lower().replace("\\", "/")
        parts = path_lower.split("/")
        for rule_keywords, layer_type in _LAYER_RULES:
            if any(kw in parts for kw in rule_keywords):
                return layer_type
        return LayerType.UNKNOWN

    def _detect_patterns(self, paths: list[str]) -> list[str]:
        return [
            pattern for pattern, detector in _PATTERN_SIGNATURES.items()
            if detector(paths)
        ]

    def _detect_boundaries(self, results: list[ParseResult]) -> list[str]:
        all_imports = {
            imp.module.lower().split(".")[0]
            for r in results
            for imp in r.imports
        }
        boundaries = []
        for boundary_name, markers in _BOUNDARY_MARKERS.items():
            if any(marker in all_imports for marker in markers):
                boundaries.append(boundary_name)
        return boundaries

    def _detect_smells(
        self,
        components: list[ArchitecturalComponent],
        graph: DependencyGraph,
    ) -> list[str]:
        smells = []

        # Circular dependencies
        if graph.circular:
            smells.append(f"Circular dependencies detected: {len(graph.circular)} cycles")

        # God modules (high fan-in)
        fan_in: dict[str, int] = {}
        for edge in graph.edges:
            fan_in[edge.target] = fan_in.get(edge.target, 0) + 1
        god_modules = [p for p, count in fan_in.items() if count > 20]
        if god_modules:
            smells.append(f"God modules (fan-in > 20): {', '.join(god_modules[:3])}")

        # Unknown layer files
        unknown = [c for c in components if c.layer == LayerType.UNKNOWN]
        if len(unknown) > len(components) * 0.3:
            smells.append("More than 30% of files could not be classified into a layer")

        # Layer violations (data → api skipping domain)
        # TODO: detect upward dependency violations based on layer ordering

        return smells
