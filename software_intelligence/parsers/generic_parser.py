"""
Software Intelligence Platform — Generic / Fallback Parser
==========================================================
Regex-heuristic parser for languages without a dedicated adapter.
Handles: Java, Go, Rust, C++, C, Ruby, C#, Kotlin, Scala, and any unknown.

Accuracy is lower than tree-sitter parsers but sufficient for:
  - Symbol extraction (class / function names)
  - Import/include/use statement collection
  - Rough line count and complexity estimation
  - Sufficient for knowledge graph node creation

When a dedicated parser is added for a language, it automatically
takes precedence via the ParserRegistry without changing this file.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from software_intelligence.parsers.base import BaseParser, ParserCapabilities
from software_intelligence.schemas import (
    ClassNode, FunctionNode, ImportNode, Language,
    NodeKind, ParseResult, SourceFile,
)

_LANG_BY_EXT = {
    ".java": Language.JAVA, ".go": Language.GO, ".rs": Language.RUST,
    ".cpp": Language.CPP, ".cc": Language.CPP, ".cxx": Language.CPP,
    ".c": Language.C, ".h": Language.C, ".hpp": Language.CPP,
    ".rb": Language.RUBY, ".cs": Language.CSHARP,
    ".kt": Language.KOTLIN, ".scala": Language.SCALA,
    ".swift": Language.SWIFT,
}

# (pattern, group_index_for_name, language_hint)
_FUNCTION_PATTERNS: list[tuple[str, int]] = [
    (r'(?:^|\s)(?:public|private|protected|static|async|func|fn|def)?\s+(?:\w+\s+)?(\w+)\s*\([^)]*\)\s*(?:\{|->|=>|:)', 1),
    (r'(?:^|\s)func\s+(\w+)\s*\(', 1),           # Go
    (r'(?:^|\s)fn\s+(\w+)\s*\(', 1),              # Rust
    (r'(?:^|\s)def\s+(\w+)\s*[({]', 1),           # Ruby / Kotlin
    (r'(?:^|\s)fun\s+(\w+)\s*\(', 1),             # Kotlin
]

_CLASS_PATTERNS: list[str] = [
    r'(?:^|\s)(?:public\s+)?(?:abstract\s+)?(?:class|struct|interface|enum|trait|object)\s+(\w+)',
]

_IMPORT_PATTERNS: list[tuple[str, int]] = [
    (r'^import\s+([\w.]+)', 1),                                 # Java / Go / Kotlin
    (r'^use\s+([\w:]+)', 1),                                    # Rust
    (r'^#include\s+[<"]([^>"]+)[>"]', 1),                       # C / C++
    (r'^require\s+[\'"]([^"\']+)[\'"]', 1),                    # Ruby
    (r'^using\s+([\w.]+)', 1),                                  # C#
    (r'^package\s+([\w.]+)', 1),                                # Java / Kotlin
]


class GenericParser(BaseParser):
    """Fallback parser using regex heuristics. Handles all unsupported languages."""

    language = Language.UNKNOWN
    supported_extensions = [".generic"]   # registered as fallback only

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities(
            functions=True, classes=True, imports=True,
            docstrings=False, complexity=False,
        )

    def can_parse(self, file: SourceFile) -> bool:
        return True   # handles everything

    def parse(self, file: SourceFile) -> ParseResult:
        ext = Path(file.path).suffix.lower()
        lang = _LANG_BY_EXT.get(ext, Language.UNKNOWN)

        result = ParseResult(
            file_id=file.file_id,
            file_path=file.path,
            language=lang,
            line_count=len(file.content.splitlines()),
        )

        content = file.content
        lines = content.splitlines()

        # Functions
        seen_fns: set[str] = set()
        for pattern, group_idx in _FUNCTION_PATTERNS:
            for m in re.finditer(pattern, content, re.MULTILINE):
                name = m.group(group_idx)
                if name in seen_fns or self._is_keyword(name):
                    continue
                seen_fns.add(name)
                line = content[:m.start()].count("\n") + 1
                result.functions.append(FunctionNode(
                    node_id=str(uuid.uuid4())[:8],
                    kind=NodeKind.FUNCTION,
                    name=name,
                    file_path=file.path,
                    line_start=line, line_end=line,
                    language=lang,
                ))

        # Classes
        seen_cls: set[str] = set()
        for pattern in _CLASS_PATTERNS:
            for m in re.finditer(pattern, content, re.MULTILINE):
                name = m.group(1)
                if name in seen_cls:
                    continue
                seen_cls.add(name)
                line = content[:m.start()].count("\n") + 1
                result.classes.append(ClassNode(
                    node_id=str(uuid.uuid4())[:8],
                    kind=NodeKind.CLASS,
                    name=name,
                    file_path=file.path,
                    line_start=line, line_end=line,
                    language=lang,
                ))

        # Imports
        seen_imp: set[str] = set()
        for pattern, group_idx in _IMPORT_PATTERNS:
            for m in re.finditer(pattern, content, re.MULTILINE):
                module = m.group(group_idx)
                if module in seen_imp:
                    continue
                seen_imp.add(module)
                line = content[:m.start()].count("\n") + 1
                result.imports.append(ImportNode(
                    node_id=str(uuid.uuid4())[:8],
                    kind=NodeKind.IMPORT,
                    name=module,
                    file_path=file.path,
                    line_start=line, line_end=line,
                    language=lang,
                    module=module,
                    is_external=not module.startswith("."),
                ))

        return result

    @staticmethod
    def _is_keyword(name: str) -> bool:
        _KEYWORDS = {
            "if", "else", "for", "while", "return", "switch", "case",
            "try", "catch", "main", "init", "new", "delete", "this",
        }
        return name.lower() in _KEYWORDS
