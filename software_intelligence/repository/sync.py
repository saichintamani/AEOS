"""
Software Intelligence Platform — Incremental Repository Sync
=============================================================
Manages sync state and drives incremental re-ingestion.

IncrementalSyncManager tracks the last-synced SHA for every repository.
On subsequent ingestions, only files changed since that SHA are re-processed.

This is critical for large repositories (millions of LOC) where full
re-ingestion on every run is prohibitively expensive.

State layout (filesystem):
    data/sync_state/
        <repo_id>.json   ← SyncState per repository
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from software_intelligence.repository.base import BaseRepositoryProvider, IngestionScope
from software_intelligence.schemas import IngestionResult, SyncStatus


@dataclass
class SyncState:
    repo_id:        str
    last_sha:       str                = ""
    last_synced_at: str                = ""
    sync_count:     int                = 0
    status:         str                = SyncStatus.PENDING.value
    files_total:    int                = 0
    errors:         list[str]         = field(default_factory=list)


class IncrementalSyncManager:
    """
    Coordinates incremental re-sync for all repositories.

    Lifecycle:
        mgr = IncrementalSyncManager(store_root="data/sync_state")

        # First full sync
        result = mgr.sync(provider, repo_id, scope, processor_fn)

        # Later: incremental sync — only changed files
        result = mgr.sync(provider, repo_id, scope, processor_fn)
    """

    def __init__(self, store_root: str = "data/sync_state") -> None:
        self._root = Path(store_root)
        self._root.mkdir(parents=True, exist_ok=True)

    def sync(
        self,
        provider: BaseRepositoryProvider,
        repo_id: str,
        scope: IngestionScope,
        processor_fn: Any,          # callable(SourceFile) → bool
        force_full: bool = False,
    ) -> IngestionResult:
        """
        Run an incremental sync if state exists, else run a full sync.

        processor_fn is called for every SourceFile that needs re-processing.
        It returns True on success, False on skip, raises on fatal error.
        """
        import time
        start = time.monotonic()
        state = self._load_state(repo_id)
        head_sha = provider.get_head_sha(repo_id)
        is_incremental = bool(state.last_sha) and not force_full

        result = IngestionResult(
            repo_id=repo_id,
            is_incremental=is_incremental,
        )

        state.status = SyncStatus.SYNCING.value
        self._save_state(state)

        try:
            if is_incremental and state.last_sha:
                changed_paths = set(provider.get_changed_files(repo_id, state.last_sha, head_sha))
                for file in provider.stream_files(repo_id, scope):
                    if file.path not in changed_paths:
                        result.files_skipped += 1
                        continue
                    try:
                        ok = processor_fn(file)
                        if ok:
                            result.files_ingested += 1
                        else:
                            result.files_skipped += 1
                    except Exception as exc:
                        result.files_failed += 1
                        result.errors.append(f"{file.path}: {exc}")
            else:
                # Full sync
                for file in provider.stream_files(repo_id, scope):
                    try:
                        ok = processor_fn(file)
                        if ok:
                            result.files_ingested += 1
                        else:
                            result.files_skipped += 1
                    except Exception as exc:
                        result.files_failed += 1
                        result.errors.append(f"{file.path}: {exc}")

            state.last_sha = head_sha
            state.last_synced_at = datetime.now(timezone.utc).isoformat()
            state.sync_count += 1
            state.status = SyncStatus.COMPLETE.value
            state.files_total += result.files_ingested
            result.last_sha = head_sha

        except Exception as exc:
            state.status = SyncStatus.FAILED.value
            state.errors.append(str(exc))
            raise

        finally:
            result.duration_s = round(time.monotonic() - start, 2)
            self._save_state(state)

        return result

    def get_state(self, repo_id: str) -> SyncState:
        return self._load_state(repo_id)

    def reset_state(self, repo_id: str) -> None:
        path = self._state_path(repo_id)
        if path.exists():
            path.unlink()

    def list_synced_repos(self) -> list[str]:
        return [f.stem for f in self._root.glob("*.json")]

    def _state_path(self, repo_id: str) -> Path:
        return self._root / f"{repo_id.replace('/', '_')}.json"

    def _load_state(self, repo_id: str) -> SyncState:
        path = self._state_path(repo_id)
        if not path.exists():
            return SyncState(repo_id=repo_id)
        try:
            return SyncState(**json.loads(path.read_text()))
        except Exception:
            return SyncState(repo_id=repo_id)

    def _save_state(self, state: SyncState) -> None:
        self._state_path(state.repo_id).write_text(
            json.dumps(asdict(state), indent=2)
        )
