"""
AEOS GitHub Analyzer — Code Parser
Extracts structural metadata from source files.
Python files use the built-in `ast` module; other languages use regex heuristics.
"""

from __future__ import annotations
import ast
import re
from dataclasses import dataclass, field

from app.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class FileStructure:
    path: str
    language: str
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    line_count: int = 0
    has_tests: bool = False
    docstring_coverage: float = 0.0


class CodeParser:

    def parse_file(self, path: str, content: str) -> FileStructure:
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if ext == ".py":
            return self.parse_python(path, content)
        return self.parse_generic(path, content)

    def parse_python(self, path: str, content: str) -> FileStructure:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            log.debug("AST parse failed, falling back to generic", extra={"ctx_path": path})
            return self.parse_generic(path, content)

        classes = self._extract_python_classes(tree)
        functions = self._extract_python_functions(tree)
        imports = self._extract_python_imports(tree)
        doc_coverage = self._docstring_coverage(tree)
        has_tests = (
            "test" in path.lower()
            or any(fn.startswith("test_") for fn in functions)
            or any(cls.startswith("Test") for cls in classes)
        )
        return FileStructure(
            path=path,
            language="python",
            classes=classes,
            functions=functions,
            imports=imports,
            line_count=len(content.splitlines()),
            has_tests=has_tests,
            docstring_coverage=doc_coverage,
        )

    def parse_generic(self, path: str, content: str) -> FileStructure:
        ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        language_map = {
            ".js": "javascript", ".ts": "typescript", ".go": "go",
            ".rs": "rust", ".java": "java", ".cpp": "cpp",
            ".c": "c", ".rb": "ruby", ".md": "markdown",
        }
        language = language_map.get(ext, "unknown")

        # Basic heuristics for non-Python files
        classes = re.findall(r'(?:^|\s)class\s+(\w+)', content, re.MULTILINE)
        functions = re.findall(r'(?:^|\s)(?:def|func|function|fn)\s+(\w+)', content, re.MULTILINE)
        imports = re.findall(r'(?:^|\s)(?:import|require|use|include)\s+[\'"]?(\S+?)[\'"]?[;\s]', content, re.MULTILINE)

        has_tests = "test" in path.lower() or bool(re.search(r'\btest\b|\bspec\b', content.lower()))
        return FileStructure(
            path=path,
            language=language,
            classes=classes[:20],
            functions=functions[:30],
            imports=imports[:20],
            line_count=len(content.splitlines()),
            has_tests=has_tests,
            docstring_coverage=0.0,
        )

    # ── AST helpers ────────────────────────────────────────────────────────────

    def _extract_python_classes(self, tree: ast.Module) -> list[str]:
        return [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]

    def _extract_python_functions(self, tree: ast.Module) -> list[str]:
        return [
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

    def _extract_python_imports(self, tree: ast.Module) -> list[str]:
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.append(module)
        return list(set(imports))[:30]

    def _docstring_coverage(self, tree: ast.Module) -> float:
        funcs = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if not funcs:
            return 1.0
        with_docs = sum(
            1 for f in funcs
            if f.body and isinstance(f.body[0], ast.Expr) and isinstance(f.body[0].value, ast.Constant)
        )
        return round(with_docs / len(funcs), 2)
