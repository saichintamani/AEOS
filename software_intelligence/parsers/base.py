"""
Software Intelligence Platform — Parser Layer: Base Abstractions
================================================================
Language-independent parsing via the Adapter pattern.

Every language parser implements BaseParser.
The ParserRegistry maps file extensions to the correct parser.
The ParserEngine dispatches files to the right adapter transparently.

Adapter contract:
    parser = PythonParser()
    result: ParseResult = parser.parse(file)
    # result.functions, result.classes, result.imports are populated
    # result.nodes is the language-agnostic AST node list
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import Language, ParseResult, SourceFile


# ── Parser capability flags ────────────────────────────────────────────────────

@dataclass
class ParserCapabilities:
    """Declares what a parser can extract."""
    functions:         bool = True
    classes:           bool = True
    imports:           bool = True
    docstrings:        bool = False
    type_annotations:  bool = False
    decorators:        bool = False
    complexity:        bool = False
    call_graph:        bool = False
    control_flow:      bool = False
    inheritance:       bool = False
    interfaces:        bool = False


# ── Abstract parser ────────────────────────────────────────────────────────────

class BaseParser(ABC):
    """
    Language-specific parsing adapter.

    Implementations must:
    1. Accept a SourceFile
    2. Return a fully populated ParseResult with language-agnostic ASTNodes
    3. Never raise on syntax errors — populate ParseResult.errors instead
    4. Be stateless (thread-safe, reusable across files)

    Parser implementations:
        PythonParser      → built-in ast module
        JavaScriptParser  → tree-sitter (js/ts grammar)
        JavaParser        → tree-sitter (java grammar)
        GoParser          → tree-sitter (go grammar)
        RustParser        → tree-sitter (rust grammar)
        GenericParser     → regex heuristics (fallback)
    """

    @property
    @abstractmethod
    def language(self) -> Language: ...

    @property
    @abstractmethod
    def supported_extensions(self) -> list[str]: ...

    @property
    def capabilities(self) -> ParserCapabilities:
        return ParserCapabilities()

    @abstractmethod
    def parse(self, file: SourceFile) -> ParseResult:
        """
        Parse one source file. Must never raise.
        Syntax errors go into ParseResult.errors.
        """
        ...

    def can_parse(self, file: SourceFile) -> bool:
        """Return True if this parser handles the file's extension."""
        from pathlib import Path
        return Path(file.path).suffix.lower() in self.supported_extensions


# ── Parser registry ────────────────────────────────────────────────────────────

class ParserRegistry:
    """
    Maps file extensions to parser instances.
    Thread-safe after construction — parsers are stateless.

    Usage:
        registry = ParserRegistry()
        registry.register(PythonParser())
        registry.register(JavaScriptParser())
        parser = registry.get_parser(file)  # returns best parser for the file
    """

    def __init__(self) -> None:
        self._parsers: dict[str, BaseParser] = {}     # extension → parser
        self._by_language: dict[Language, BaseParser] = {}

    def register(self, parser: BaseParser) -> None:
        for ext in parser.supported_extensions:
            self._parsers[ext.lower()] = parser
        self._by_language[parser.language] = parser

    def get_parser(self, file: SourceFile) -> BaseParser:
        from pathlib import Path
        ext = Path(file.path).suffix.lower()
        parser = self._parsers.get(ext)
        if parser is None:
            parser = self._parsers.get(".generic")
        if parser is None:
            raise KeyError(f"No parser registered for extension '{ext}'")
        return parser

    def get_parser_for_language(self, language: Language) -> BaseParser | None:
        return self._by_language.get(language)

    def supported_extensions(self) -> list[str]:
        return sorted(self._parsers.keys())

    def supported_languages(self) -> list[Language]:
        return list(self._by_language.keys())


# ── Parser engine (facade) ─────────────────────────────────────────────────────

class ParserEngine:
    """
    Primary entry point for parsing.

    Dispatches each SourceFile to the registered parser.
    Falls back to GenericParser for unsupported languages.

    Usage:
        engine = ParserEngine.default()    # loads all built-in parsers
        results = engine.parse_files(source_files)
    """

    def __init__(self, registry: ParserRegistry) -> None:
        self._registry = registry

    @classmethod
    def default(cls) -> "ParserEngine":
        """Build an engine with all built-in parsers pre-registered."""
        from software_intelligence.parsers.python_parser import PythonParser
        from software_intelligence.parsers.js_parser import JavaScriptParser, TypeScriptParser
        from software_intelligence.parsers.generic_parser import GenericParser

        registry = ParserRegistry()
        registry.register(PythonParser())
        registry.register(JavaScriptParser())
        registry.register(TypeScriptParser())
        registry.register(GenericParser())    # fallback for all other extensions
        return cls(registry)

    def parse(self, file: SourceFile) -> ParseResult:
        """Parse a single file. Returns ParseResult with errors populated on failure."""
        try:
            parser = self._registry.get_parser(file)
        except KeyError:
            from software_intelligence.parsers.generic_parser import GenericParser
            parser = GenericParser()
        return parser.parse(file)

    def parse_files(self, files: list[SourceFile]) -> list[ParseResult]:
        """Parse a collection of files, skipping fatal errors."""
        results = []
        for file in files:
            try:
                results.append(self.parse(file))
            except Exception as exc:
                from software_intelligence.schemas import ParseResult as PR
                results.append(PR(
                    file_id=file.file_id,
                    file_path=file.path,
                    language=Language(file.language) if file.language in [l.value for l in Language] else Language.UNKNOWN,
                    errors=[str(exc)],
                ))
        return results

    def parse_stream(self, files):
        """Generator variant for memory-efficient processing of large repos."""
        for file in files:
            yield self.parse(file)
