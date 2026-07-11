"""
AEOS GitHub Analyzer — Repo Summarizer
Aggregates FileStructure objects into a human-readable repo summary.
Rule-based — no LLM required.
"""

from __future__ import annotations
from app.github_analyzer.parser import FileStructure
from app.core.logger import get_logger

log = get_logger(__name__)


class RepoSummarizer:

    def summarize(
        self,
        repo_meta: dict,
        files: list[dict],
        structures: list[FileStructure],
    ) -> dict:
        lang_breakdown = self._language_breakdown(structures)
        entry_points = self._entry_points(structures)
        complexity = self._complexity_estimate(structures)
        has_tests = any(s.has_tests for s in structures)
        description = self._generate_description(repo_meta, structures)

        total_classes = sum(len(s.classes) for s in structures)
        total_functions = sum(len(s.functions) for s in structures)
        avg_doc_coverage = (
            round(sum(s.docstring_coverage for s in structures) / len(structures), 2)
            if structures else 0.0
        )

        return {
            "repo": repo_meta.get("full_name", "unknown"),
            "description": description,
            "language_breakdown": lang_breakdown,
            "total_files": len(structures),
            "total_classes": total_classes,
            "total_functions": total_functions,
            "entry_points": entry_points,
            "complexity": complexity,
            "has_tests": has_tests,
            "avg_docstring_coverage": avg_doc_coverage,
            "topics": repo_meta.get("topics", []),
            "stars": repo_meta.get("stars", 0),
            "primary_language": repo_meta.get("language", "unknown"),
        }

    def _language_breakdown(self, structures: list[FileStructure]) -> dict:
        counts: dict[str, int] = {}
        for s in structures:
            counts[s.language] = counts.get(s.language, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    def _entry_points(self, structures: list[FileStructure]) -> list[str]:
        patterns = ["main.py", "app.py", "run.py", "server.py", "index.py", "setup.py", "__main__.py"]
        found = [s.path for s in structures if any(s.path.endswith(ep) for ep in patterns)]
        return found[:5]

    def _complexity_estimate(self, structures: list[FileStructure]) -> str:
        total_lines = sum(s.line_count for s in structures)
        total_funcs = sum(len(s.functions) for s in structures)
        if total_lines < 500 or total_funcs < 20:
            return "low"
        if total_lines < 5000 or total_funcs < 100:
            return "medium"
        return "high"

    def _generate_description(self, repo_meta: dict, structures: list[FileStructure]) -> str:
        name = repo_meta.get("full_name", "this repository")
        desc = repo_meta.get("description", "")
        lang = repo_meta.get("language", "unknown")
        file_count = len(structures)
        total_funcs = sum(len(s.functions) for s in structures)
        base = desc if desc else f"A {lang} repository"
        return (
            f"{base}. Contains {file_count} analyzed files with "
            f"{total_funcs} functions across the codebase."
        )
