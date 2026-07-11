"""
Software Intelligence Platform — Repository Layer: Base Abstractions
=====================================================================
Every repository provider (GitHub, GitLab, Bitbucket, Git, Local)
implements BaseRepositoryProvider.

The ingestion engine operates exclusively through this interface —
it never touches provider-specific SDK objects directly.

Provider contract:
    provider = GitHubProvider(config)
    record   = provider.get_repository("owner/repo")
    files    = provider.list_files(repo_id, extensions=[".py"])
    commits  = provider.get_commits(repo_id, limit=100)
    content  = provider.get_file_content(repo_id, "src/main.py")
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator

from software_intelligence.schemas import (
    CommitRecord, ProviderType, RepositoryRecord, SourceFile,
)


# ── Provider configuration ─────────────────────────────────────────────────────

@dataclass
class ProviderConfig:
    provider_type:   ProviderType
    base_url:        str                 = ""       # for self-hosted GitLab / Bitbucket
    token:           str                 = ""       # personal access token / OAuth
    username:        str                 = ""
    password:        str                 = ""       # app password / basic auth
    ssh_key_path:    str                 = ""
    timeout_s:       int                 = 30
    max_file_size_b: int                 = 500_000  # skip files larger than this
    rate_limit_rpm:  int                 = 60       # requests per minute
    extra:           dict[str, Any]      = field(default_factory=dict)


# ── Ingestion scope ────────────────────────────────────────────────────────────

@dataclass
class IngestionScope:
    """
    Controls what gets ingested from a repository.
    Passed to BaseRepositoryProvider.ingest() to constrain scope.
    """
    extensions:      list[str]           = field(default_factory=list)
    exclude_paths:   list[str]           = field(default_factory=list)  # glob patterns
    include_paths:   list[str]           = field(default_factory=list)  # if set, only these
    max_file_size_b: int                 = 500_000
    max_files:       int                 = 10_000
    branches:        list[str]           = field(default_factory=list)  # default: default_branch
    include_commits: bool                = True
    include_issues:  bool                = False    # requires API provider
    include_prs:     bool                = False
    since_sha:       str                 = ""       # incremental: only since this commit
    since_date:      str                 = ""       # incremental: only since ISO date


@dataclass
class IngestionResult:
    repo_id:        str
    files_ingested: int                  = 0
    files_skipped:  int                  = 0
    files_failed:   int                  = 0
    commits:        int                  = 0
    issues:         int                  = 0
    prs:            int                  = 0
    errors:         list[str]           = field(default_factory=list)
    duration_s:     float               = 0.0
    is_incremental: bool                = False
    last_sha:       str                 = ""


# ── Abstract provider ──────────────────────────────────────────────────────────

class BaseRepositoryProvider(ABC):
    """
    Universal interface for all repository backends.

    Every method must be safe to call concurrently.
    Implementations must handle rate limiting internally.

    Supported providers:
        GitHubProvider      → GitHub REST API v3
        GitLabProvider      → GitLab REST API v4
        BitbucketProvider   → Bitbucket REST API 2.0
        LocalGitProvider    → libgit2 / GitPython on local clone
        LocalFSProvider     → bare filesystem (no git)
    """

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    @property
    def provider_type(self) -> ProviderType:
        return self._config.provider_type

    # ── Repository metadata ────────────────────────────────────────────────────

    @abstractmethod
    def get_repository(self, identifier: str) -> RepositoryRecord:
        """
        Fetch top-level repository metadata.
        identifier: "owner/repo" for hosted providers, path for local.
        """
        ...

    @abstractmethod
    def repository_exists(self, identifier: str) -> bool: ...

    # ── File access ────────────────────────────────────────────────────────────

    @abstractmethod
    def list_files(
        self,
        repo_id: str,
        scope: IngestionScope | None = None,
    ) -> list[SourceFile]:
        """Return all matching files as SourceFile records (content populated)."""
        ...

    @abstractmethod
    def stream_files(
        self,
        repo_id: str,
        scope: IngestionScope | None = None,
    ) -> Iterator[SourceFile]:
        """Memory-safe streaming variant for large repositories."""
        ...

    @abstractmethod
    def get_file_content(self, repo_id: str, path: str, ref: str = "") -> str:
        """Return the UTF-8 content of a single file at the given ref (SHA/branch)."""
        ...

    @abstractmethod
    def file_exists(self, repo_id: str, path: str) -> bool: ...

    # ── Commits ────────────────────────────────────────────────────────────────

    @abstractmethod
    def get_commits(
        self,
        repo_id: str,
        limit: int = 100,
        since_sha: str = "",
        since_date: str = "",
        branch: str = "",
    ) -> list[CommitRecord]: ...

    @abstractmethod
    def get_commit(self, repo_id: str, sha: str) -> CommitRecord: ...

    # ── Issues & PRs (API providers only) ─────────────────────────────────────

    def get_issues(self, repo_id: str, state: str = "all", limit: int = 100) -> list[dict]:
        """Override in API-backed providers. Returns raw issue dicts."""
        return []

    def get_pull_requests(self, repo_id: str, state: str = "all", limit: int = 100) -> list[dict]:
        """Override in API-backed providers. Returns raw PR dicts."""
        return []

    # ── Incremental sync ───────────────────────────────────────────────────────

    @abstractmethod
    def get_changed_files(
        self,
        repo_id: str,
        since_sha: str,
        until_sha: str = "HEAD",
    ) -> list[str]:
        """Return list of file paths changed between two commits."""
        ...

    @abstractmethod
    def get_head_sha(self, repo_id: str, branch: str = "") -> str:
        """Return the HEAD commit SHA for the given branch."""
        ...

    # ── Monorepo support ───────────────────────────────────────────────────────

    def detect_subprojects(self, repo_id: str) -> list[str]:
        """
        Return list of subproject root paths within a monorepo.
        Detected by presence of: package.json, pyproject.toml, Cargo.toml, go.mod, pom.xml
        """
        return []

    # ── Health ─────────────────────────────────────────────────────────────────

    @abstractmethod
    def health_check(self) -> bool:
        """Return True if the provider is reachable and authenticated."""
        ...
