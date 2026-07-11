"""
AEOS RAG — Document Loaders
Each loader produces a unified Document from a specific source type.

Architecture:
    BaseLoader (ABC)                   — contract every loader must satisfy
    ├── TextLoader        (.txt)
    ├── MarkdownLoader    (.md, .markdown)   strips YAML frontmatter
    ├── PDFLoader         (.pdf)             requires pypdf
    ├── HTMLLoader        (.html, .htm)      strips tags via bs4 or regex fallback
    ├── JSONLoader        (.json)            serialises arrays as multi-block text
    ├── PythonLoader      (.py)              preserves indentation + docstrings
    └── TextLoader        (fallback for .ts, .js, .go, .rs, .yaml, …)

    DocumentLoaderRegistry             — maps extensions → loaders, loads directories

Usage:
    from app.rag.loader import loader_registry
    doc  = loader_registry.load("/path/to/file.pdf")
    docs = loader_registry.load_directory("/docs/", extensions=[".md", ".txt"])
"""
from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.rag.schemas import Document
from app.rag.exceptions import LoaderError
from app.core.logger import get_logger

log = get_logger(__name__)

# Safety limit: skip files larger than this
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_doc_id(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_file(path: Path) -> None:
    if not path.exists():
        raise LoaderError(f"File not found: {path}", {"path": str(path)})
    if path.stat().st_size > MAX_FILE_SIZE_BYTES:
        raise LoaderError(
            f"File too large (>{MAX_FILE_SIZE_BYTES // 1_048_576} MB): {path}",
            {"path": str(path), "size_bytes": path.stat().st_size},
        )


# ── Abstract base ──────────────────────────────────────────────────────────────

class BaseLoader(ABC):
    """
    Abstract document loader contract.
    Every loader must produce a unified Document from its source.
    """

    @abstractmethod
    def load(self, source: str | Path, **kwargs: Any) -> Document:
        """
        Load source and return a Document.
        Raises LoaderError on any failure.
        """
        ...

    @abstractmethod
    def can_load(self, source: str | Path) -> bool:
        """Return True if this loader can handle the given source path."""
        ...

    def _build_document(
        self,
        content: str,
        source: str,
        doc_type: str,
        metadata: dict | None = None,
    ) -> Document:
        return Document(
            id=_make_doc_id(content),
            source=source,
            content=content,
            doc_type=doc_type,
            metadata=metadata or {"source": source, "doc_type": doc_type},
            ingested_at=_utc_now(),
        )


# ── Concrete loaders ───────────────────────────────────────────────────────────

class TextLoader(BaseLoader):
    """Loads plain text files (.txt and generic source files)."""

    _EXTENSIONS = {".txt"}

    def can_load(self, source: str | Path) -> bool:
        return Path(source).suffix.lower() in self._EXTENSIONS

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        path = Path(source)
        _check_file(path)
        try:
            content = path.read_text(encoding=kwargs.get("encoding", "utf-8"), errors="replace")
        except Exception as exc:
            raise LoaderError(f"Cannot read {path}: {exc}", {"path": str(path)}) from exc
        log.debug("TextLoader loaded", extra={"ctx_path": str(path)})
        return self._build_document(content, str(path), "text")


class MarkdownLoader(BaseLoader):
    """Loads Markdown files (.md, .markdown). Strips YAML frontmatter if present."""

    _EXTENSIONS = {".md", ".markdown"}

    def can_load(self, source: str | Path) -> bool:
        return Path(source).suffix.lower() in self._EXTENSIONS

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        path = Path(source)
        _check_file(path)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise LoaderError(f"Cannot read {path}: {exc}", {"path": str(path)}) from exc
        content = self._strip_frontmatter(raw)
        log.debug("MarkdownLoader loaded", extra={"ctx_path": str(path)})
        return self._build_document(content, str(path), "markdown")

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        if not text.startswith("---"):
            return text
        end = text.find("---", 3)
        if end == -1:
            return text
        return text[end + 3:].lstrip()


class PDFLoader(BaseLoader):
    """Loads PDF files using pypdf. Requires: pip install pypdf."""

    def can_load(self, source: str | Path) -> bool:
        return Path(source).suffix.lower() == ".pdf"

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        path = Path(source)
        _check_file(path)
        try:
            from pypdf import PdfReader
        except ImportError:
            raise LoaderError(
                "pypdf is not installed. Run: pip install pypdf",
                {"path": str(path)},
            )
        try:
            reader = PdfReader(str(path))
            pages = [page.extract_text() or "" for page in reader.pages]
            content = "\n\n".join(pages)
            metadata = {
                "source": str(path),
                "doc_type": "pdf",
                "page_count": len(reader.pages),
            }
        except Exception as exc:
            raise LoaderError(f"Cannot parse PDF {path}: {exc}", {"path": str(path)}) from exc
        log.debug("PDFLoader loaded", extra={"ctx_path": str(path), "ctx_pages": len(pages)})
        return self._build_document(content, str(path), "pdf", metadata)


class HTMLLoader(BaseLoader):
    """
    Loads HTML files (.html, .htm).
    Uses BeautifulSoup4 when available; falls back to regex tag stripping.
    """

    _EXTENSIONS = {".html", ".htm"}

    def can_load(self, source: str | Path) -> bool:
        return Path(source).suffix.lower() in self._EXTENSIONS

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        path = Path(source)
        _check_file(path)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            content = self._extract_text(raw)
        except Exception as exc:
            raise LoaderError(f"Cannot parse HTML {path}: {exc}", {"path": str(path)}) from exc
        log.debug("HTMLLoader loaded", extra={"ctx_path": str(path)})
        return self._build_document(content, str(path), "html")

    @staticmethod
    def _extract_text(html: str) -> str:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)
        except ImportError:
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            return re.sub(r"\s+", " ", text).strip()


class JSONLoader(BaseLoader):
    """
    Loads JSON files (.json).
    Arrays → each item serialised to an indented block separated by newlines.
    Objects → single indented JSON block.
    """

    def can_load(self, source: str | Path) -> bool:
        return Path(source).suffix.lower() == ".json"

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        path = Path(source)
        _check_file(path)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(raw)
            content = self._serialise(data)
        except json.JSONDecodeError as exc:
            raise LoaderError(f"Invalid JSON in {path}: {exc}", {"path": str(path)}) from exc
        except Exception as exc:
            raise LoaderError(f"Cannot read {path}: {exc}", {"path": str(path)}) from exc
        log.debug("JSONLoader loaded", extra={"ctx_path": str(path)})
        return self._build_document(content, str(path), "json")

    @staticmethod
    def _serialise(data: Any) -> str:
        if isinstance(data, list):
            return "\n\n".join(
                json.dumps(item, indent=2, default=str) if isinstance(item, dict) else str(item)
                for item in data
            )
        return json.dumps(data, indent=2, default=str)


class PythonLoader(BaseLoader):
    """
    Loads Python source files (.py).
    Preserves all whitespace, indentation, and docstrings as-is.
    """

    def can_load(self, source: str | Path) -> bool:
        return Path(source).suffix.lower() == ".py"

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        path = Path(source)
        _check_file(path)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            raise LoaderError(f"Cannot read {path}: {exc}", {"path": str(path)}) from exc
        metadata = {
            "source": str(path),
            "doc_type": "python",
            "module": path.stem,
            "package": path.parent.name,
        }
        log.debug("PythonLoader loaded", extra={"ctx_path": str(path)})
        return self._build_document(content, str(path), "python", metadata)


# ── Extension → loader map ─────────────────────────────────────────────────────

_TEXT_LOADER = TextLoader()

_EXTENSION_MAP: dict[str, BaseLoader] = {
    ".txt":      TextLoader(),
    ".md":       MarkdownLoader(),
    ".markdown": MarkdownLoader(),
    ".pdf":      PDFLoader(),
    ".html":     HTMLLoader(),
    ".htm":      HTMLLoader(),
    ".json":     JSONLoader(),
    ".py":       PythonLoader(),
}

# Treat all other known source / data formats as plain text
_TEXT_LIKE_EXTENSIONS = {
    ".ts", ".js", ".jsx", ".tsx", ".java", ".go", ".rs",
    ".cpp", ".c", ".h", ".cs", ".rb", ".php", ".swift",
    ".yaml", ".yml", ".toml", ".xml", ".csv", ".sql",
    ".sh", ".bash", ".zsh", ".ps1",
}
for _ext in _TEXT_LIKE_EXTENSIONS:
    _EXTENSION_MAP[_ext] = _TEXT_LOADER


# ── DocumentLoaderRegistry ─────────────────────────────────────────────────────

class DocumentLoaderRegistry:
    """
    Routes source files to the appropriate loader by file extension.
    Custom loaders can be registered at runtime via register().

    The module-level singleton `loader_registry` is the default instance.
    """

    def __init__(self) -> None:
        self._registry: dict[str, BaseLoader] = dict(_EXTENSION_MAP)

    def register(self, extension: str, loader: BaseLoader) -> None:
        """Register (or override) a loader for a file extension, e.g. '.rst'."""
        self._registry[extension.lower()] = loader
        log.debug("Loader registered", extra={"ctx_ext": extension})

    def get_loader(self, source: str | Path) -> BaseLoader | None:
        return self._registry.get(Path(source).suffix.lower())

    def load(self, source: str | Path, **kwargs: Any) -> Document:
        """
        Load a document, auto-selecting loader by file extension.
        Falls back to TextLoader for unknown extensions.
        """
        loader = self.get_loader(source) or _TEXT_LOADER
        if loader is _TEXT_LOADER and Path(source).suffix.lower() not in self._registry:
            log.warning(
                "No registered loader for extension — using TextLoader",
                extra={"ctx_source": str(source)},
            )
        return loader.load(source, **kwargs)

    def load_directory(
        self,
        path: str | Path,
        extensions: list[str] | None = None,
    ) -> list[Document]:
        """
        Recursively load all supported files in a directory.
        Pass extensions to restrict which file types are loaded.
        Skips files that fail and logs a warning per failure.
        """
        root = Path(path)
        if not root.is_dir():
            raise LoaderError(f"Not a directory: {root}", {"path": str(root)})

        supported = (
            {e.lower() for e in extensions}
            if extensions
            else set(self._registry.keys())
        )
        docs: list[Document] = []
        failed = 0

        for file in sorted(root.rglob("*")):
            if not file.is_file() or file.suffix.lower() not in supported:
                continue
            try:
                doc = self.load(file)
                docs.append(doc)
            except LoaderError as exc:
                failed += 1
                log.warning(
                    "File load failed, skipping",
                    extra={"ctx_path": str(file), "ctx_error": str(exc)},
                )

        log.info(
            "Directory loaded",
            extra={
                "ctx_path": str(root),
                "ctx_docs": len(docs),
                "ctx_failed": failed,
            },
        )
        return docs


# Module-level singleton — import and use directly
loader_registry = DocumentLoaderRegistry()
