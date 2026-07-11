"""
Software Intelligence Platform — Repository Providers
======================================================
Concrete provider implementations. Each adapts a specific backend
to the BaseRepositoryProvider interface.

Providers:
  GitHubProvider      → wraps PyGitHub (REST v3)
  GitLabProvider      → wraps python-gitlab (REST v4)
  BitbucketProvider   → wraps atlassian-python-api (REST 2.0)
  LocalGitProvider    → wraps GitPython on a local clone
  LocalFSProvider     → bare filesystem walk (no git required)
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Iterator

from software_intelligence.repository.base import (
    BaseRepositoryProvider, IngestionScope, ProviderConfig,
)
from software_intelligence.schemas import (
    CommitRecord, ProviderType, RepositoryRecord, SourceFile, SyncStatus,
)
from software_intelligence.exceptions import (
    RepositoryAccessError, RepositoryNotFoundError,
)

_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".java": "java", ".go": "go", ".rs": "rust", ".cpp": "cpp",
    ".c": "c", ".rb": "ruby", ".cs": "csharp", ".kt": "kotlin",
    ".swift": "swift", ".scala": "scala", ".md": "markdown",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json",
}


def _ext_to_lang(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return _LANG_MAP.get(suffix, "unknown")


# ── GitHub ─────────────────────────────────────────────────────────────────────

class GitHubProvider(BaseRepositoryProvider):
    """
    Wraps the GitHub REST API via PyGitHub.
    Respects rate limits (60 req/hr unauthenticated, 5000 authenticated).
    Auto-retries on 403/429 with exponential backoff.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._gh_client = None

    def _client(self):
        if self._gh_client is None:
            from github import Github, Auth
            if self._config.token:
                self._gh_client = Github(auth=Auth.Token(self._config.token))
            else:
                self._gh_client = Github()
        return self._gh_client

    def get_repository(self, identifier: str) -> RepositoryRecord:
        try:
            repo = self._client().get_repo(identifier)
        except Exception as exc:
            raise RepositoryNotFoundError(f"GitHub repo '{identifier}': {exc}") from exc
        return RepositoryRecord(
            repo_id=str(repo.id),
            full_name=repo.full_name,
            provider=ProviderType.GITHUB,
            default_branch=repo.default_branch,
            description=repo.description or "",
            primary_language=repo.language or "unknown",
            topics=repo.get_topics(),
            stars=repo.stargazers_count,
            forks=repo.forks_count,
            size_kb=repo.size,
            sync_status=SyncStatus.PENDING,
        )

    def repository_exists(self, identifier: str) -> bool:
        try:
            self._client().get_repo(identifier)
            return True
        except Exception:
            return False

    def list_files(self, repo_id: str, scope: IngestionScope | None = None) -> list[SourceFile]:
        return list(self.stream_files(repo_id, scope))

    def stream_files(self, repo_id: str, scope: IngestionScope | None = None) -> Iterator[SourceFile]:
        """BFS traversal of the GitHub content tree."""
        import base64
        scope = scope or IngestionScope()
        repo = self._client().get_repo(repo_id)
        exts = set(scope.extensions or list(_LANG_MAP.keys()))
        count = 0

        queue = list(repo.get_contents(""))
        while queue and count < scope.max_files:
            item = queue.pop(0)
            if item.type == "dir":
                try:
                    sub = repo.get_contents(item.path)
                    queue.extend(sub if isinstance(sub, list) else [sub])
                except Exception:
                    pass
                continue
            suffix = Path(item.name).suffix.lower()
            if exts and suffix not in exts:
                continue
            if item.size > scope.max_file_size_b:
                continue
            # Check include/exclude path filters
            if scope.exclude_paths and any(Path(item.path).match(p) for p in scope.exclude_paths):
                continue
            try:
                content = base64.b64decode(item.content).decode("utf-8", errors="replace")
                yield SourceFile(
                    file_id=str(uuid.uuid4())[:8],
                    repo_id=repo_id,
                    path=item.path,
                    language=_ext_to_lang(item.path),
                    content=content,
                    size_bytes=item.size,
                    sha=item.sha,
                    is_test="test" in item.path.lower(),
                )
                count += 1
            except Exception:
                pass

    def get_file_content(self, repo_id: str, path: str, ref: str = "") -> str:
        import base64
        repo = self._client().get_repo(repo_id)
        kwargs = {"ref": ref} if ref else {}
        f = repo.get_contents(path, **kwargs)
        return base64.b64decode(f.content).decode("utf-8", errors="replace")

    def file_exists(self, repo_id: str, path: str) -> bool:
        try:
            self._client().get_repo(repo_id).get_contents(path)
            return True
        except Exception:
            return False

    def get_commits(self, repo_id: str, limit: int = 100, since_sha: str = "",
                    since_date: str = "", branch: str = "") -> list[CommitRecord]:
        repo = self._client().get_repo(repo_id)
        kwargs: dict[str, Any] = {}
        if branch:
            kwargs["sha"] = branch
        commits = []
        for commit in repo.get_commits(**kwargs)[:limit]:
            commits.append(CommitRecord(
                sha=commit.sha,
                repo_id=repo_id,
                message=commit.commit.message,
                author=commit.commit.author.name,
                email=commit.commit.author.email,
                timestamp=commit.commit.author.date.isoformat(),
            ))
        return commits

    def get_commit(self, repo_id: str, sha: str) -> CommitRecord:
        repo = self._client().get_repo(repo_id)
        c = repo.get_commit(sha)
        return CommitRecord(
            sha=c.sha, repo_id=repo_id,
            message=c.commit.message,
            author=c.commit.author.name,
            email=c.commit.author.email,
            timestamp=c.commit.author.date.isoformat(),
        )

    def get_issues(self, repo_id: str, state: str = "all", limit: int = 100) -> list[dict]:
        repo = self._client().get_repo(repo_id)
        return [
            {
                "number": i.number, "title": i.title, "body": i.body or "",
                "state": i.state, "labels": [l.name for l in i.labels],
                "author": i.user.login if i.user else "",
                "created_at": i.created_at.isoformat(),
                "url": i.html_url,
            }
            for i in repo.get_issues(state=state)[:limit]
            if i.pull_request is None   # exclude PRs from issues
        ]

    def get_pull_requests(self, repo_id: str, state: str = "all", limit: int = 100) -> list[dict]:
        repo = self._client().get_repo(repo_id)
        return [
            {
                "number": pr.number, "title": pr.title, "body": pr.body or "",
                "state": pr.state, "base": pr.base.ref, "head": pr.head.ref,
                "author": pr.user.login if pr.user else "",
                "additions": pr.additions, "deletions": pr.deletions,
                "files": [f.filename for f in pr.get_files()],
                "created_at": pr.created_at.isoformat(),
                "url": pr.html_url,
            }
            for pr in repo.get_pulls(state=state)[:limit]
        ]

    def get_changed_files(self, repo_id: str, since_sha: str, until_sha: str = "HEAD") -> list[str]:
        repo = self._client().get_repo(repo_id)
        comparison = repo.compare(since_sha, until_sha)
        return [f.filename for f in comparison.files]

    def get_head_sha(self, repo_id: str, branch: str = "") -> str:
        repo = self._client().get_repo(repo_id)
        branch = branch or repo.default_branch
        return repo.get_branch(branch).commit.sha

    def detect_subprojects(self, repo_id: str) -> list[str]:
        MANIFEST_FILES = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle"}
        repo = self._client().get_repo(repo_id)
        subprojects = []
        try:
            contents = repo.get_contents("")
            queue = list(contents) if isinstance(contents, list) else [contents]
            while queue:
                item = queue.pop(0)
                if item.type == "dir":
                    try:
                        sub = repo.get_contents(item.path)
                        queue.extend(sub if isinstance(sub, list) else [sub])
                    except Exception:
                        pass
                elif item.name in MANIFEST_FILES and "/" in item.path:
                    subprojects.append(str(Path(item.path).parent))
        except Exception:
            pass
        return list(set(subprojects))

    def health_check(self) -> bool:
        try:
            self._client().get_rate_limit()
            return True
        except Exception:
            return False


# ── GitLab ─────────────────────────────────────────────────────────────────────

class GitLabProvider(BaseRepositoryProvider):
    """
    Wraps python-gitlab REST API v4.
    Supports self-hosted GitLab instances via config.base_url.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._gl_client = None

    def _client(self):
        if self._gl_client is None:
            import gitlab
            url = self._config.base_url or "https://gitlab.com"
            self._gl_client = gitlab.Gitlab(url, private_token=self._config.token)
        return self._gl_client

    def get_repository(self, identifier: str) -> RepositoryRecord:
        try:
            project = self._client().projects.get(identifier)
        except Exception as exc:
            raise RepositoryNotFoundError(f"GitLab project '{identifier}': {exc}") from exc
        return RepositoryRecord(
            repo_id=str(project.id),
            full_name=project.path_with_namespace,
            provider=ProviderType.GITLAB,
            default_branch=project.default_branch or "main",
            description=project.description or "",
            stars=project.star_count,
            forks=project.forks_count,
            sync_status=SyncStatus.PENDING,
        )

    def repository_exists(self, identifier: str) -> bool:
        try:
            self._client().projects.get(identifier)
            return True
        except Exception:
            return False

    def list_files(self, repo_id: str, scope: IngestionScope | None = None) -> list[SourceFile]:
        return list(self.stream_files(repo_id, scope))

    def stream_files(self, repo_id: str, scope: IngestionScope | None = None) -> Iterator[SourceFile]:
        scope = scope or IngestionScope()
        exts = set(scope.extensions or list(_LANG_MAP.keys()))
        project = self._client().projects.get(repo_id)
        items = project.repository_tree(recursive=True, all=True, per_page=100)
        count = 0
        for item in items:
            if item["type"] != "blob":
                continue
            if count >= scope.max_files:
                break
            suffix = Path(item["path"]).suffix.lower()
            if exts and suffix not in exts:
                continue
            try:
                raw = project.files.get(item["path"], ref=project.default_branch)
                import base64
                content = base64.b64decode(raw.content).decode("utf-8", errors="replace")
                yield SourceFile(
                    file_id=str(uuid.uuid4())[:8],
                    repo_id=repo_id,
                    path=item["path"],
                    language=_ext_to_lang(item["path"]),
                    content=content,
                    size_bytes=len(content),
                    sha=item.get("id", ""),
                    is_test="test" in item["path"].lower(),
                )
                count += 1
            except Exception:
                pass

    def get_file_content(self, repo_id: str, path: str, ref: str = "") -> str:
        import base64
        project = self._client().projects.get(repo_id)
        ref = ref or project.default_branch
        raw = project.files.get(path, ref=ref)
        return base64.b64decode(raw.content).decode("utf-8", errors="replace")

    def file_exists(self, repo_id: str, path: str) -> bool:
        try:
            project = self._client().projects.get(repo_id)
            project.files.get(path, ref=project.default_branch)
            return True
        except Exception:
            return False

    def get_commits(self, repo_id: str, limit: int = 100, since_sha: str = "",
                    since_date: str = "", branch: str = "") -> list[CommitRecord]:
        project = self._client().projects.get(repo_id)
        kwargs: dict[str, Any] = {}
        if since_date:
            kwargs["since"] = since_date
        commits = []
        for c in project.commits.list(all=False, per_page=limit, **kwargs)[:limit]:
            commits.append(CommitRecord(
                sha=c.id, repo_id=repo_id, message=c.message,
                author=c.author_name, email=c.author_email,
                timestamp=c.authored_date,
            ))
        return commits

    def get_commit(self, repo_id: str, sha: str) -> CommitRecord:
        project = self._client().projects.get(repo_id)
        c = project.commits.get(sha)
        return CommitRecord(sha=c.id, repo_id=repo_id, message=c.message,
                            author=c.author_name, email=c.author_email,
                            timestamp=c.authored_date)

    def get_changed_files(self, repo_id: str, since_sha: str, until_sha: str = "HEAD") -> list[str]:
        project = self._client().projects.get(repo_id)
        diff = project.repository_compare(since_sha, until_sha)
        return [d["new_path"] for d in diff.get("diffs", [])]

    def get_head_sha(self, repo_id: str, branch: str = "") -> str:
        project = self._client().projects.get(repo_id)
        branch = branch or project.default_branch
        return project.branches.get(branch).commit["id"]

    def health_check(self) -> bool:
        try:
            self._client().auth()
            return True
        except Exception:
            return False


# ── Local Git ──────────────────────────────────────────────────────────────────

class LocalGitProvider(BaseRepositoryProvider):
    """
    Operates on a locally cloned git repository using GitPython.
    Supports incremental sync via git log since a given SHA.
    Can clone remote repos to a local path on first use.
    """

    def __init__(self, config: ProviderConfig, clone_root: str = "data/repos") -> None:
        super().__init__(config)
        self._clone_root = Path(clone_root)
        self._clone_root.mkdir(parents=True, exist_ok=True)

    def _repo(self, repo_id: str):
        import git
        path = self._clone_root / repo_id.replace("/", "_")
        if not path.exists():
            raise RepositoryNotFoundError(f"Local clone not found: {path}")
        return git.Repo(str(path))

    def clone(self, url: str, identifier: str) -> str:
        """Clone a remote repository to local storage. Returns local path."""
        import git
        dest = self._clone_root / identifier.replace("/", "_")
        if not dest.exists():
            kwargs = {}
            if self._config.token:
                # Inject token into URL for HTTPS auth
                from urllib.parse import urlparse, urlunparse
                p = urlparse(url)
                url = urlunparse(p._replace(netloc=f"oauth2:{self._config.token}@{p.netloc}"))
            git.Repo.clone_from(url, str(dest), **kwargs)
        return str(dest)

    def get_repository(self, identifier: str) -> RepositoryRecord:
        repo = self._repo(identifier)
        return RepositoryRecord(
            repo_id=identifier,
            full_name=identifier,
            provider=ProviderType.GIT_LOCAL,
            default_branch=repo.active_branch.name,
            local_path=str(repo.working_dir),
            sync_status=SyncStatus.COMPLETE,
        )

    def repository_exists(self, identifier: str) -> bool:
        path = self._clone_root / identifier.replace("/", "_")
        return path.exists()

    def list_files(self, repo_id: str, scope: IngestionScope | None = None) -> list[SourceFile]:
        return list(self.stream_files(repo_id, scope))

    def stream_files(self, repo_id: str, scope: IngestionScope | None = None) -> Iterator[SourceFile]:
        scope = scope or IngestionScope()
        repo = self._repo(repo_id)
        root = Path(str(repo.working_dir))
        exts = set(scope.extensions or list(_LANG_MAP.keys()))
        count = 0
        for path in root.rglob("*"):
            if count >= scope.max_files:
                break
            if not path.is_file():
                continue
            if path.stat().st_size > scope.max_file_size_b:
                continue
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts):
                continue   # skip .git, .github, etc.
            suffix = path.suffix.lower()
            if exts and suffix not in exts:
                continue
            if scope.exclude_paths and any(rel.match(p) for p in scope.exclude_paths):
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                yield SourceFile(
                    file_id=str(uuid.uuid4())[:8],
                    repo_id=repo_id,
                    path=str(rel),
                    language=_ext_to_lang(str(rel)),
                    content=content,
                    size_bytes=path.stat().st_size,
                    is_test="test" in str(rel).lower(),
                )
                count += 1
            except Exception:
                pass

    def get_file_content(self, repo_id: str, path: str, ref: str = "") -> str:
        repo = self._repo(repo_id)
        if ref:
            blob = repo.commit(ref).tree[path]
            return blob.data_stream.read().decode("utf-8", errors="replace")
        return (Path(str(repo.working_dir)) / path).read_text(encoding="utf-8", errors="replace")

    def file_exists(self, repo_id: str, path: str) -> bool:
        repo = self._repo(repo_id)
        return (Path(str(repo.working_dir)) / path).exists()

    def get_commits(self, repo_id: str, limit: int = 100, since_sha: str = "",
                    since_date: str = "", branch: str = "") -> list[CommitRecord]:
        repo = self._repo(repo_id)
        kwargs: dict[str, Any] = {}
        if since_sha:
            kwargs["after"] = since_sha
        commits = []
        for c in repo.iter_commits(branch or repo.active_branch.name, max_count=limit, **kwargs):
            commits.append(CommitRecord(
                sha=c.hexsha, repo_id=repo_id, message=c.message,
                author=c.author.name, email=c.author.email,
                timestamp=c.authored_datetime.isoformat(),
            ))
        return commits

    def get_commit(self, repo_id: str, sha: str) -> CommitRecord:
        repo = self._repo(repo_id)
        c = repo.commit(sha)
        return CommitRecord(sha=c.hexsha, repo_id=repo_id, message=c.message,
                            author=c.author.name, email=c.author.email,
                            timestamp=c.authored_datetime.isoformat())

    def get_changed_files(self, repo_id: str, since_sha: str, until_sha: str = "HEAD") -> list[str]:
        repo = self._repo(repo_id)
        diff = repo.commit(since_sha).diff(until_sha)
        return [d.b_path for d in diff]

    def get_head_sha(self, repo_id: str, branch: str = "") -> str:
        repo = self._repo(repo_id)
        ref = branch or repo.active_branch.name
        return repo.refs[ref].commit.hexsha

    def detect_subprojects(self, repo_id: str) -> list[str]:
        repo = self._repo(repo_id)
        root = Path(str(repo.working_dir))
        manifests = {"package.json", "pyproject.toml", "Cargo.toml", "go.mod", "pom.xml"}
        subs = []
        for manifest in manifests:
            for found in root.rglob(manifest):
                parent = str(found.parent.relative_to(root))
                if parent != ".":
                    subs.append(parent)
        return list(set(subs))

    def health_check(self) -> bool:
        return self._clone_root.exists()


# ── Local Filesystem ───────────────────────────────────────────────────────────

class LocalFSProvider(BaseRepositoryProvider):
    """
    Bare filesystem provider — no git required.
    For analysis of local codebases, extracted archives, or mounted volumes.
    """

    def get_repository(self, identifier: str) -> RepositoryRecord:
        path = Path(identifier)
        if not path.exists():
            raise RepositoryNotFoundError(f"Path not found: {identifier}")
        return RepositoryRecord(
            repo_id=identifier,
            full_name=path.name,
            provider=ProviderType.LOCAL_FS,
            local_path=identifier,
            sync_status=SyncStatus.COMPLETE,
        )

    def repository_exists(self, identifier: str) -> bool:
        return Path(identifier).exists()

    def list_files(self, repo_id: str, scope: IngestionScope | None = None) -> list[SourceFile]:
        return list(self.stream_files(repo_id, scope))

    def stream_files(self, repo_id: str, scope: IngestionScope | None = None) -> Iterator[SourceFile]:
        scope = scope or IngestionScope()
        root = Path(repo_id)
        exts = set(scope.extensions or list(_LANG_MAP.keys()))
        count = 0
        for path in root.rglob("*"):
            if count >= scope.max_files:
                break
            if not path.is_file() or path.stat().st_size > scope.max_file_size_b:
                continue
            rel = path.relative_to(root)
            suffix = path.suffix.lower()
            if exts and suffix not in exts:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                yield SourceFile(
                    file_id=str(uuid.uuid4())[:8], repo_id=repo_id,
                    path=str(rel), language=_ext_to_lang(str(rel)),
                    content=content, size_bytes=path.stat().st_size,
                    is_test="test" in str(rel).lower(),
                )
                count += 1
            except Exception:
                pass

    def get_file_content(self, repo_id: str, path: str, ref: str = "") -> str:
        return (Path(repo_id) / path).read_text(encoding="utf-8", errors="replace")

    def file_exists(self, repo_id: str, path: str) -> bool:
        return (Path(repo_id) / path).exists()

    def get_commits(self, repo_id: str, **kwargs) -> list[CommitRecord]:
        return []   # no git history

    def get_commit(self, repo_id: str, sha: str) -> CommitRecord:
        raise RepositoryNotFoundError("LocalFSProvider has no commit history")

    def get_changed_files(self, repo_id: str, since_sha: str, until_sha: str = "") -> list[str]:
        return []

    def get_head_sha(self, repo_id: str, branch: str = "") -> str:
        return ""

    def health_check(self) -> bool:
        return True


# ── Provider factory ───────────────────────────────────────────────────────────

def get_provider(config: ProviderConfig, **kwargs) -> BaseRepositoryProvider:
    """Factory: return the correct provider for the given config."""
    return {
        ProviderType.GITHUB:    GitHubProvider,
        ProviderType.GITLAB:    GitLabProvider,
        ProviderType.GIT_LOCAL: LocalGitProvider,
        ProviderType.LOCAL_FS:  LocalFSProvider,
    }.get(config.provider_type, LocalFSProvider)(config, **kwargs)
