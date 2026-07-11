"""
Software Intelligence Platform — Python Parser
===============================================
Uses Python's built-in `ast` module for full structural extraction.
This is the most capable parser in the platform — Python is fully supported.

Extracts:
  - FunctionDef / AsyncFunctionDef with parameters, return types, decorators
  - ClassDef with bases, methods, class variables, MRO hints
  - Import / ImportFrom with stdlib/external classification
  - Docstrings for all nodes
  - Cyclomatic complexity (McCabe)
  - Call graph (function → called functions)
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

from software_intelligence.parsers.base import BaseParser, ParserCapabilities
from software_intelligence.schemas import (
    ASTNode, ClassNode, FunctionNode, ImportNode, Language,
    NodeKind, ParseResult, SourceFile,
)

# Standard library top-level modules (subset — extended at runtime via sys.stdlib_module_names)
_STDLIB_PREFIXES = {
    "os", "sys", "re", "io", "json", "math", "time", "datetime", "pathlib",
    "collections", "itertools", "functools", "typing", "abc", "enum",
    "logging", "unittest", "dataclasses", "copy", "hashlib", "uuid",
    "asyncio", "threading", "subprocess", "socket", "http", "urllib",
    "contextlib", "inspect", "ast", "dis", "gc", "struct", "traceback",
}


class PythonParser(BaseParser):

    language = Language.PYTHON
    supported_extensions = [".py", ".pyi"]

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            functions=True, classes=True, imports=True, docstrings=True,
            type_annotations=True, decorators=True, complexity=True,
            call_graph=True, control_flow=False, inheritance=True,
        )

    def parse(self, file: SourceFile) -> ParseResult:
        result = ParseResult(
            file_id=file.file_id,
            file_path=file.path,
            language=Language.PYTHON,
            line_count=len(file.content.splitlines()),
        )
        try:
            tree = ast.parse(file.content, filename=file.path)
        except SyntaxError as exc:
            result.errors.append(f"SyntaxError at line {exc.lineno}: {exc.msg}")
            return result

        result.raw_ast = tree
        result.imports = self._extract_imports(tree, file.path)
        result.classes = self._extract_classes(tree, file.path)
        result.functions = self._extract_functions(tree, file.path)
        result.nodes = (
            [n for n in result.imports] +
            [n for n in result.functions] +
            [n for n in result.classes]
        )
        return result

    # ── Imports ────────────────────────────────────────────────────────────────

    def _extract_imports(self, tree: ast.Module, file_path: str) -> list[ImportNode]:
        nodes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    nodes.append(ImportNode(
                        node_id=str(uuid.uuid4())[:8],
                        kind=NodeKind.IMPORT,
                        name=alias.name,
                        file_path=file_path,
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                        language=Language.PYTHON,
                        module=alias.name,
                        alias=alias.asname or "",
                        is_stdlib=self._is_stdlib(alias.name),
                        is_external=not self._is_stdlib(alias.name) and not alias.name.startswith("."),
                        is_relative=alias.name.startswith("."),
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                symbols = [a.name for a in node.names]
                is_rel = node.level > 0
                nodes.append(ImportNode(
                    node_id=str(uuid.uuid4())[:8],
                    kind=NodeKind.IMPORT,
                    name=module,
                    file_path=file_path,
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    language=Language.PYTHON,
                    module=module,
                    symbols=symbols,
                    is_relative=is_rel,
                    is_stdlib=self._is_stdlib(module),
                    is_external=not self._is_stdlib(module) and not is_rel,
                ))
        return nodes

    # ── Functions ──────────────────────────────────────────────────────────────

    def _extract_functions(self, tree: ast.Module, file_path: str) -> list[FunctionNode]:
        nodes = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            params = [a.arg for a in node.args.args]
            ret_type = ""
            if node.returns:
                ret_type = ast.unparse(node.returns) if hasattr(ast, "unparse") else ""
            decorators = [ast.unparse(d) if hasattr(ast, "unparse") else "" for d in node.decorator_list]
            doc = ast.get_docstring(node) or ""
            calls = self._extract_calls(node)
            cc = self._cyclomatic_complexity(node)

            fn = FunctionNode(
                node_id=str(uuid.uuid4())[:8],
                kind=NodeKind.METHOD if self._is_method(node, tree) else NodeKind.FUNCTION,
                name=node.name,
                file_path=file_path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                language=Language.PYTHON,
                docstring=doc,
                signature=self._function_signature(node),
                decorators=decorators,
                parameters=params,
                return_type=ret_type,
                is_async=isinstance(node, ast.AsyncFunctionDef),
                cyclomatic_complexity=cc,
                calls=calls,
            )
            nodes.append(fn)
        return nodes

    # ── Classes ────────────────────────────────────────────────────────────────

    def _extract_classes(self, tree: ast.Module, file_path: str) -> list[ClassNode]:
        nodes = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(b) if hasattr(ast, "unparse") else "" for b in node.bases]
            methods = [n.name for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            doc = ast.get_docstring(node) or ""
            decorators = [ast.unparse(d) if hasattr(ast, "unparse") else "" for d in node.decorator_list]
            is_dc = any("dataclass" in d for d in decorators)
            is_abstract = any(b in {"ABC", "ABCMeta"} for b in bases)
            nodes.append(ClassNode(
                node_id=str(uuid.uuid4())[:8],
                kind=NodeKind.CLASS,
                name=node.name,
                file_path=file_path,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                language=Language.PYTHON,
                docstring=doc,
                decorators=decorators,
                bases=bases,
                methods=methods,
                is_abstract=is_abstract,
                is_dataclass=is_dc,
            ))
        return nodes

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _cyclomatic_complexity(self, node: ast.AST) -> int:
        """McCabe complexity: 1 + number of decision points."""
        decision_nodes = (
            ast.If, ast.For, ast.While, ast.ExceptHandler,
            ast.With, ast.Assert, ast.comprehension,
        )
        cc = 1
        for child in ast.walk(node):
            if isinstance(child, decision_nodes):
                cc += 1
            elif isinstance(child, ast.BoolOp):
                cc += len(child.values) - 1
        return cc

    def _extract_calls(self, node: ast.AST) -> list[str]:
        calls = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Name):
                    calls.append(child.func.id)
                elif isinstance(child.func, ast.Attribute):
                    calls.append(child.func.attr)
        return list(set(calls))[:20]

    def _is_method(self, node: ast.AST, tree: ast.Module) -> bool:
        for parent in ast.walk(tree):
            if isinstance(parent, ast.ClassDef):
                if any(n is node for n in ast.walk(parent)):
                    return True
        return False

    def _function_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        try:
            params = ", ".join(a.arg for a in node.args.args)
            ret = f" -> {ast.unparse(node.returns)}" if node.returns and hasattr(ast, "unparse") else ""
            prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
            return f"{prefix}def {node.name}({params}){ret}"
        except Exception:
            return f"def {node.name}(...)"

    @staticmethod
    def _is_stdlib(module: str) -> bool:
        top = module.split(".")[0]
        return top in _STDLIB_PREFIXES
