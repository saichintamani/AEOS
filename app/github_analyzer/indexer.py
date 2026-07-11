"""
AEOS GitHub Analyzer — Repo Indexer
Orchestrates the full fetch → parse → summarize → RAG ingest pipeline.
"""

from __future__ import annotations

from app.github_analyzer.fetcher import GitHubFetcher
from app.github_analyzer.parser import CodeParser
from app.github_analyzer.summarizer import RepoSummarizer
from app.core.logger import get_logger

log = get_logger(__name__)


class RepoIndexer:

    def __init__(self, rag_namespace: str = "github") -> None:
        self._rag_namespace = rag_namespace
        self._fetcher = GitHubFetcher()
        self._parser = CodeParser()
        self._summarizer = RepoSummarizer()

    async def index_repo(
        self,
        repo_full_name: str,
        index_into_rag: bool = True,
        file_extensions: list[str] | None = None,
    ) -> dict:
        """
        Full pipeline: fetch → parse → summarize → (optionally) index into RAG.
        Returns a stats dict with the repo summary.
        """
        log.info("Starting repo indexing", extra={"ctx_repo": repo_full_name})

        # 1. Fetch
        try:
            repo_meta = self._fetcher.get_repo(repo_full_name)
            readme = self._fetcher.get_readme(repo_full_name)
            files = self._fetcher.get_files(repo_full_name, extensions=file_extensions)
        except Exception as exc:
            log.error("Repo fetch failed", extra={"ctx_repo": repo_full_name, "ctx_error": str(exc)})
            raise ValueError(f"Could not fetch repo '{repo_full_name}': {exc}") from exc

        # 2. Parse
        structures = []
        for f in files:
            try:
                structure = self._parser.parse_file(f["path"], f["content"])
                structures.append(structure)
            except Exception as exc:
                log.debug("Parse failed for file", extra={"ctx_path": f["path"], "ctx_error": str(exc)})

        # 3. Summarize
        summary = self._summarizer.summarize(repo_meta, files, structures)

        # 4. Index into RAG (optional)
        chunks_added = 0
        if index_into_rag:
            from app.rag.rag_engine import get_rag_engine
            engine = get_rag_engine(self._rag_namespace)

            # Index README
            if readme:
                chunks_added += engine.ingest_text(
                    readme,
                    source=f"github://{repo_full_name}/README",
                    doc_type="markdown",
                )

            # Index each file as indexable text (path + signatures + content)
            for f, struct in zip(files, structures):
                indexable = self._build_indexable_text(f, struct)
                chunks_added += engine.ingest_text(
                    indexable,
                    source=f"github://{repo_full_name}/{f['path']}",
                    doc_type=f.get("type", "code"),
                )

        result = {
            **summary,
            "chunks_indexed": chunks_added,
            "rag_namespace": self._rag_namespace if index_into_rag else None,
            "indexed_into_rag": index_into_rag,
        }
        log.info(
            "Repo indexing complete",
            extra={"ctx_repo": repo_full_name, "ctx_chunks": chunks_added},
        )
        return result

    def _build_indexable_text(self, file: dict, struct) -> str:
        """
        Combines path, imports, class/function signatures, and raw content
        into a single text blob for embedding.
        """
        lines = [
            f"File: {file['path']}",
            f"Language: {struct.language}",
        ]
        if struct.classes:
            lines.append(f"Classes: {', '.join(struct.classes[:10])}")
        if struct.functions:
            lines.append(f"Functions: {', '.join(struct.functions[:20])}")
        if struct.imports:
            lines.append(f"Imports: {', '.join(struct.imports[:10])}")
        lines.append("")
        # Append up to 2000 chars of content
        lines.append(file["content"][:2000])
        return "\n".join(lines)
