"""
AEOS RAG — Document Ingestor
Loads files from disk (text, markdown, PDF, source code) into Document objects
with chunks pre-populated by SemanticChunker.
"""

from __future__ import annotations
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from app.rag.chunker import SemanticChunker, Chunk
from app.core.logger import get_logger

log = get_logger(__name__)

SUPPORTED_EXTENSIONS = {
    ".txt": "text",
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".h": "c_header",
    ".cs": "csharp",
    ".rb": "ruby",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
}

MAX_FILE_SIZE_BYTES = 500_000  # 500KB — skip larger files


@dataclass
class Document:
    id: str               # sha256[:16] of content
    source: str           # file path or descriptive label
    content: str          # raw text
    doc_type: str         # see SUPPORTED_EXTENSIONS values
    metadata: dict = field(default_factory=dict)
    chunks: list[Chunk] = field(default_factory=list)


class DocumentIngestor:
    """
    Loads and chunks documents from disk or inline text.
    All methods return Document(s) with chunks already populated.
    """

    def __init__(self, chunker: SemanticChunker | None = None) -> None:
        self._chunker = chunker or SemanticChunker()

    # ── Public API ─────────────────────────────────────────────────────────────

    def ingest_file(self, path: str | Path) -> Document | None:
        p = Path(path)
        if not p.exists():
            log.warning("File not found, skipping", extra={"ctx_path": str(p)})
            return None
        if p.stat().st_size > MAX_FILE_SIZE_BYTES:
            log.warning(
                "File too large, skipping",
                extra={"ctx_path": str(p), "ctx_size": p.stat().st_size},
            )
            return None

        doc_type = self._detect_type(p)
        try:
            if doc_type == "pdf":
                content = self._read_pdf(p)
            else:
                content = self._read_text_file(p)
        except Exception as exc:
            log.warning("Failed to read file", extra={"ctx_path": str(p), "ctx_error": str(exc)})
            return None

        return self._make_document(content=content, source=str(p), doc_type=doc_type)

    def ingest_directory(
        self,
        path: str | Path,
        extensions: list[str] | None = None,
    ) -> list[Document]:
        p = Path(path)
        if not p.is_dir():
            log.warning("Not a directory", extra={"ctx_path": str(p)})
            return []

        exts = {e.lower() for e in (extensions or list(SUPPORTED_EXTENSIONS.keys()))}
        docs: list[Document] = []
        for file in p.rglob("*"):
            if file.suffix.lower() in exts and file.is_file():
                doc = self.ingest_file(file)
                if doc:
                    docs.append(doc)

        log.info(
            "Directory ingested",
            extra={"ctx_path": str(p), "ctx_docs": len(docs)},
        )
        return docs

    def ingest_text(
        self,
        text: str,
        source: str = "inline",
        doc_type: str = "text",
    ) -> Document:
        return self._make_document(content=text, source=source, doc_type=doc_type)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _make_document(self, content: str, source: str, doc_type: str) -> Document:
        doc_id = self._make_id(content)
        metadata = {"source": source, "doc_type": doc_type}
        chunks = self._chunker.chunk(content, metadata)
        doc = Document(
            id=doc_id,
            source=source,
            content=content,
            doc_type=doc_type,
            metadata=metadata,
            chunks=chunks,
        )
        log.debug(
            "Document created",
            extra={"ctx_doc_id": doc_id, "ctx_chunks": len(chunks), "ctx_source": source},
        )
        return doc

    def _read_text_file(self, path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace")

    def _read_pdf(self, path: Path) -> str:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages)

    def _detect_type(self, path: Path) -> str:
        return SUPPORTED_EXTENSIONS.get(path.suffix.lower(), "text")

    def _make_id(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
