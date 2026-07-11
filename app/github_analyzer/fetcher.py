"""
AEOS GitHub Analyzer — Fetcher
Wraps the GitHub REST API via PyGithub.
Falls back to unauthenticated access when GITHUB_TOKEN is unset (60 req/hr limit).
"""

from __future__ import annotations
import base64
from typing import Any

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

MAX_FILE_SIZE_BYTES = 100_000   # skip files larger than 100KB
DEFAULT_CODE_EXTENSIONS = [".py", ".md", ".txt", ".js", ".ts", ".go", ".rs", ".java"]


class GitHubFetcher:

    def __init__(self, token: str | None = None) -> None:
        self._token = token or settings.github_token or None

    def _client(self):
        from github import Github, Auth
        if self._token:
            return Github(auth=Auth.Token(self._token))
        return Github()

    def get_repo(self, repo_full_name: str) -> dict:
        """Return basic repo metadata. repo_full_name: 'owner/repo'."""
        log.info("Fetching repo metadata", extra={"ctx_repo": repo_full_name})
        repo = self._client().get_repo(repo_full_name)
        return {
            "full_name": repo.full_name,
            "description": repo.description or "",
            "language": repo.language or "unknown",
            "stars": repo.stargazers_count,
            "forks": repo.forks_count,
            "topics": repo.get_topics(),
            "default_branch": repo.default_branch,
            "size_kb": repo.size,
        }

    def get_readme(self, repo_full_name: str) -> str:
        """Return decoded README content, or empty string if not found."""
        try:
            repo = self._client().get_repo(repo_full_name)
            readme = repo.get_readme()
            return base64.b64decode(readme.content).decode("utf-8", errors="replace")
        except Exception as exc:
            log.warning("README not found", extra={"ctx_repo": repo_full_name, "ctx_error": str(exc)})
            return ""

    def get_files(
        self,
        repo_full_name: str,
        extensions: list[str] | None = None,
        max_files: int = 50,
    ) -> list[dict]:
        """
        Return list of file dicts from the repo.
        Each dict: {"path": str, "content": str, "size": int, "type": str}
        """
        exts = {e.lower() for e in (extensions or DEFAULT_CODE_EXTENSIONS)}
        log.info(
            "Fetching repo files",
            extra={"ctx_repo": repo_full_name, "ctx_extensions": list(exts)},
        )
        repo = self._client().get_repo(repo_full_name)
        results: list[dict] = []

        try:
            contents = repo.get_contents("")
        except Exception as exc:
            log.warning("Cannot list repo contents", extra={"ctx_error": str(exc)})
            return results

        # BFS over repo tree (depth-limited to avoid huge repos)
        queue = list(contents) if isinstance(contents, list) else [contents]
        visited = 0

        while queue and len(results) < max_files:
            item = queue.pop(0)
            if item.type == "dir":
                if visited < 200:
                    try:
                        sub = repo.get_contents(item.path)
                        queue.extend(sub if isinstance(sub, list) else [sub])
                        visited += 1
                    except Exception:
                        pass
                continue

            suffix = "." + item.name.rsplit(".", 1)[-1].lower() if "." in item.name else ""
            if suffix not in exts:
                continue
            if item.size > MAX_FILE_SIZE_BYTES:
                continue

            try:
                raw = base64.b64decode(item.content).decode("utf-8", errors="replace")
                results.append({
                    "path": item.path,
                    "content": raw,
                    "size": item.size,
                    "type": suffix.lstrip("."),
                })
            except Exception as exc:
                log.debug("Skipping file", extra={"ctx_path": item.path, "ctx_error": str(exc)})

        log.info("Files fetched", extra={"ctx_repo": repo_full_name, "ctx_count": len(results)})
        return results
