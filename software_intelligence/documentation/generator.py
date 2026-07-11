"""
Software Intelligence Platform — Documentation Generator
=========================================================
AI-assisted documentation generation engine.

Generates:
  - README improvements (structure gaps, missing sections)
  - API documentation (functions, classes, endpoints)
  - Architecture documentation (component descriptions)
  - Module summaries (per-file and per-package)
  - Developer onboarding guide (setup, patterns, entry points)

Strategy:
  1. Rule-based extraction builds the structural scaffold
  2. LLM (via AEOS RAG/agent layer) fills in descriptions
  3. Templates ensure consistent Markdown output
  4. Undocumented module detector flags gaps

The generator is LLM-agnostic — it builds a DocumentationRequest
and delegates to the LLMDocumentationBackend.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from software_intelligence.schemas import (
    ArchitectureReport, ParseResult, RepositoryRecord,
)


# ── Documentation request / result ────────────────────────────────────────────

@dataclass
class DocumentationSection:
    title:   str
    content: str
    level:   int     = 2        # markdown heading level (##, ###, ####)
    tags:    list[str] = field(default_factory=list)


@dataclass
class GeneratedDocument:
    kind:       str              # "readme", "api", "architecture", "module", "onboarding"
    repo_id:    str
    title:      str
    sections:   list[DocumentationSection] = field(default_factory=list)
    metadata:   dict[str, Any]             = field(default_factory=dict)

    def to_markdown(self) -> str:
        lines = [f"# {self.title}", ""]
        for section in self.sections:
            heading = "#" * section.level
            lines.append(f"{heading} {section.title}")
            lines.append("")
            lines.append(section.content)
            lines.append("")
        return "\n".join(lines)


@dataclass
class UndocumentedModule:
    file_path:     str
    missing_items: list[str]    # ["module docstring", "function: parse_file", ...]
    severity:      str          = "warning"


# ── LLM backend ABC ───────────────────────────────────────────────────────────

class BaseLLMDocumentationBackend(ABC):
    """
    Abstraction over the LLM used for description generation.
    Swap between AEOS agent, OpenAI, or Anthropic without changing the generator.
    """

    @abstractmethod
    def generate_description(self, prompt: str, context: str, max_tokens: int = 512) -> str: ...

    @abstractmethod
    def summarize_code(self, code: str, language: str) -> str: ...

    @abstractmethod
    def generate_docstring(self, signature: str, context: str) -> str: ...


class AEOSAgentBackend(BaseLLMDocumentationBackend):
    """Uses the AEOS orchestrator to route documentation generation requests."""

    def generate_description(self, prompt: str, context: str, max_tokens: int = 512) -> str:
        # TODO: call AEOS orchestrator → documentation agent
        return ""

    def summarize_code(self, code: str, language: str) -> str:
        # TODO: call AEOS orchestrator → code summarization agent
        return ""

    def generate_docstring(self, signature: str, context: str) -> str:
        # TODO: call AEOS orchestrator → docstring generation agent
        return ""


# ── Documentation generator ────────────────────────────────────────────────────

class DocumentationGenerator:
    """
    Produces documentation artifacts from static analysis + optional LLM.

    Usage:
        generator = DocumentationGenerator(llm_backend=AEOSAgentBackend())
        readme = generator.generate_readme(repo, parse_results, arch_report)
        api_doc = generator.generate_api_docs(parse_results)
        onboarding = generator.generate_onboarding_guide(repo, parse_results, arch_report)
    """

    def __init__(self, llm_backend: BaseLLMDocumentationBackend | None = None) -> None:
        self._llm = llm_backend

    # ── README generation ──────────────────────────────────────────────────────

    def generate_readme(
        self,
        repo: RepositoryRecord,
        results: list[ParseResult],
        arch_report: ArchitectureReport | None = None,
        existing_readme: str = "",
    ) -> GeneratedDocument:
        doc = GeneratedDocument(
            kind="readme",
            repo_id=repo.repo_id,
            title=repo.full_name,
        )

        # Overview
        desc = repo.description or (
            self._llm.generate_description(
                f"Describe the purpose of {repo.full_name}",
                self._code_context(results[:5]),
            ) if self._llm else "A software repository."
        )
        doc.sections.append(DocumentationSection("Overview", desc))

        # Features (from architecture)
        if arch_report:
            features = self._extract_features(arch_report)
            doc.sections.append(DocumentationSection("Features", features))

        # Installation
        install = self._generate_install_section(results)
        doc.sections.append(DocumentationSection("Installation", install))

        # Usage
        usage = self._generate_usage_section(results, arch_report)
        doc.sections.append(DocumentationSection("Usage", usage))

        # Architecture
        if arch_report:
            arch_desc = self._generate_arch_section(arch_report)
            doc.sections.append(DocumentationSection("Architecture", arch_desc))

        # Contributing
        doc.sections.append(DocumentationSection("Contributing", self._contributing_template()))

        # Missing sections from existing README
        if existing_readme:
            missing = self._detect_missing_sections(existing_readme)
            if missing:
                doc.metadata["missing_sections"] = missing

        return doc

    # ── API documentation ──────────────────────────────────────────────────────

    def generate_api_docs(self, results: list[ParseResult]) -> GeneratedDocument:
        doc = GeneratedDocument(
            kind="api",
            repo_id=results[0].file_path if results else "",
            title="API Reference",
        )
        for result in results:
            if not result.functions and not result.classes:
                continue
            module_section = self._module_api_section(result)
            doc.sections.append(module_section)
        return doc

    # ── Module summaries ───────────────────────────────────────────────────────

    def generate_module_summary(self, result: ParseResult) -> GeneratedDocument:
        doc = GeneratedDocument(
            kind="module",
            repo_id=result.file_path,
            title=f"Module: {Path(result.file_path).stem}",
        )
        # Classes
        for cls in result.classes:
            content = f"**Bases:** {', '.join(cls.bases) or 'object'}\n\n"
            if cls.docstring:
                content += cls.docstring + "\n\n"
            if cls.methods:
                content += "**Methods:** " + ", ".join(f"`{m}`" for m in cls.methods[:10])
            doc.sections.append(DocumentationSection(f"Class `{cls.name}`", content, level=3))

        # Functions
        for fn in result.functions:
            content = f"```\n{fn.signature}\n```\n\n"
            if fn.docstring:
                content += fn.docstring
            elif self._llm:
                content += self._llm.generate_docstring(fn.signature, "")
            doc.sections.append(DocumentationSection(f"`{fn.name}()`", content, level=3))

        return doc

    # ── Onboarding guide ───────────────────────────────────────────────────────

    def generate_onboarding_guide(
        self,
        repo: RepositoryRecord,
        results: list[ParseResult],
        arch_report: ArchitectureReport | None = None,
    ) -> GeneratedDocument:
        doc = GeneratedDocument(
            kind="onboarding",
            repo_id=repo.repo_id,
            title=f"Developer Onboarding Guide — {repo.full_name}",
        )
        doc.sections.extend([
            DocumentationSection("Repository Purpose", repo.description or ""),
            DocumentationSection("Prerequisites", self._prerequisites(results)),
            DocumentationSection("Getting Started", self._getting_started(results)),
            DocumentationSection("Codebase Structure", self._codebase_structure(results, arch_report)),
            DocumentationSection("Key Concepts", self._key_concepts(arch_report)),
            DocumentationSection("Development Workflow", self._dev_workflow()),
            DocumentationSection("Running Tests", self._test_instructions(results)),
        ])
        return doc

    # ── Undocumented module detection ──────────────────────────────────────────

    def detect_undocumented(self, results: list[ParseResult]) -> list[UndocumentedModule]:
        undocumented = []
        for result in results:
            missing = []
            # Check public functions
            for fn in result.functions:
                if not fn.name.startswith("_") and not fn.docstring:
                    missing.append(f"function: {fn.name}")
            # Check public classes
            for cls in result.classes:
                if not cls.name.startswith("_") and not cls.docstring:
                    missing.append(f"class: {cls.name}")
            if missing:
                severity = "error" if len(missing) > 5 else "warning"
                undocumented.append(UndocumentedModule(
                    file_path=result.file_path,
                    missing_items=missing[:20],
                    severity=severity,
                ))
        return sorted(undocumented, key=lambda u: len(u.missing_items), reverse=True)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _code_context(self, results: list[ParseResult]) -> str:
        lines = []
        for r in results:
            fns = ", ".join(fn.name for fn in r.functions[:5])
            lines.append(f"{r.file_path}: functions=[{fns}]")
        return "\n".join(lines)

    def _extract_features(self, arch_report: ArchitectureReport) -> str:
        lines = ["Key capabilities:"]
        for comp in arch_report.components[:8]:
            lines.append(f"- **{comp.name}**: {comp.description}")
        if arch_report.patterns:
            lines.append(f"\nArchitectural patterns: {', '.join(arch_report.patterns)}")
        return "\n".join(lines)

    def _generate_install_section(self, results: list[ParseResult]) -> str:
        has_requirements = any("requirements" in r.file_path for r in results)
        has_pyproject = any("pyproject" in r.file_path for r in results)
        if has_pyproject:
            return "```bash\npip install -e .\n```"
        if has_requirements:
            return "```bash\npip install -r requirements.txt\n```"
        return "```bash\n# See project documentation for installation steps\n```"

    def _generate_usage_section(self, results: list[ParseResult], arch: Any) -> str:
        entry_pts = (arch.entry_points if arch else []) or []
        if entry_pts:
            return f"Start the application:\n```bash\npython {entry_pts[0]}\n```"
        return "See the README for usage instructions."

    def _generate_arch_section(self, arch: ArchitectureReport) -> str:
        lines = []
        if arch.patterns:
            lines.append(f"**Pattern:** {', '.join(arch.patterns)}\n")
        if arch.boundaries:
            lines.append(f"**System Boundaries:** {', '.join(arch.boundaries)}\n")
        lines.append("**Layers:**")
        for comp in arch.components[:6]:
            lines.append(f"- `{comp.layer.value}` — {comp.description}")
        return "\n".join(lines)

    def _detect_missing_sections(self, readme: str) -> list[str]:
        readme_lower = readme.lower()
        expected = ["installation", "usage", "contributing", "license", "architecture"]
        return [s for s in expected if s not in readme_lower]

    def _module_api_section(self, result: ParseResult) -> DocumentationSection:
        lines = [f"**File:** `{result.file_path}`", ""]
        for cls in result.classes[:5]:
            lines.append(f"**Class `{cls.name}`**")
            if cls.docstring:
                lines.append(f"> {cls.docstring[:200]}")
            lines.append("")
        for fn in result.functions[:10]:
            lines.append(f"**`{fn.signature or fn.name}()`**")
            if fn.docstring:
                lines.append(f"> {fn.docstring[:200]}")
            lines.append("")
        return DocumentationSection(
            title=f"`{Path(result.file_path).stem}`",
            content="\n".join(lines),
            level=3,
        )

    def _prerequisites(self, results: list[ParseResult]) -> str:
        langs = list({r.language.value for r in results})
        return "\n".join([f"- {lang.title()} runtime" for lang in langs[:3]])

    def _getting_started(self, results: list[ParseResult]) -> str:
        return (
            "```bash\n"
            "git clone <repo-url>\n"
            "cd <repo-name>\n"
            "pip install -r requirements.txt\n"
            "```"
        )

    def _codebase_structure(self, results: list[ParseResult], arch: Any) -> str:
        if arch:
            lines = []
            for comp in arch.components[:8]:
                lines.append(f"- **{comp.layer.value}** (`{comp.name}`): {comp.description}")
            return "\n".join(lines)
        # Fallback: top-level directories
        dirs = sorted({r.file_path.split("/")[0] for r in results if "/" in r.file_path})
        return "\n".join(f"- `{d}/`" for d in dirs[:10])

    def _key_concepts(self, arch: Any) -> str:
        if arch and arch.patterns:
            return f"This project uses: {', '.join(arch.patterns)}"
        return "Review the architecture section for key design patterns."

    def _dev_workflow(self) -> str:
        return (
            "1. Create a feature branch\n"
            "2. Make changes\n"
            "3. Run tests\n"
            "4. Submit a pull request"
        )

    def _test_instructions(self, results: list[ParseResult]) -> str:
        test_files = [r.file_path for r in results if r.file_path and "test" in r.file_path.lower()]
        if test_files:
            return "```bash\npytest\n```"
        return "No test files detected."

    def _contributing_template(self) -> str:
        return (
            "1. Fork the repository\n"
            "2. Create a feature branch (`git checkout -b feature/my-feature`)\n"
            "3. Commit your changes\n"
            "4. Open a Pull Request"
        )
