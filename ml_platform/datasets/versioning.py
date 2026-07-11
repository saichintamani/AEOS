"""
ML Platform — Dataset Layer: Version Management
===============================================
Tracks all versions of every named dataset.
Enables reproducible training runs by pinning exact dataset versions.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ml_platform.datasets.base import DatasetMetadata


@dataclass
class DatasetVersion:
    dataset_id:  str       # content hash (immutable)
    name:        str       # logical dataset name
    version:     str       # "1.0.0"
    parent_id:   str | None  # dataset_id this was derived from
    created_at:  str
    description: str = ""
    is_latest:   bool = False


class DatasetVersionManager:
    """
    Filesystem-backed version store.

    Layout:
        <store_root>/
            <name>/
                versions.json          # ordered list of DatasetVersion
                <dataset_id>_meta.json # full DatasetMetadata per version
    """

    def __init__(self, store_root: str) -> None:
        self._root = Path(store_root)
        self._root.mkdir(parents=True, exist_ok=True)

    # ── Write ──────────────────────────────────────────────────────────────────

    def register(
        self,
        metadata: DatasetMetadata,
        description: str = "",
        parent_id: str | None = None,
    ) -> DatasetVersion:
        """Register a new version of a dataset."""
        dataset_dir = self._root / metadata.name
        dataset_dir.mkdir(parents=True, exist_ok=True)

        existing = self._load_versions(metadata.name)
        next_version = self._bump_version(existing)

        version = DatasetVersion(
            dataset_id=metadata.dataset_id,
            name=metadata.name,
            version=next_version,
            parent_id=parent_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            description=description,
            is_latest=True,
        )

        # Demote previous latest
        for v in existing:
            v.is_latest = False

        existing.append(version)
        self._save_versions(metadata.name, existing)

        # Write full metadata snapshot
        meta_path = dataset_dir / f"{metadata.dataset_id}_meta.json"
        meta_path.write_text(json.dumps(asdict(metadata), indent=2))

        return version

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_latest(self, name: str) -> DatasetVersion | None:
        versions = self._load_versions(name)
        return next((v for v in reversed(versions) if v.is_latest), None)

    def get_version(self, name: str, version: str) -> DatasetVersion | None:
        return next(
            (v for v in self._load_versions(name) if v.version == version),
            None,
        )

    def list_versions(self, name: str) -> list[DatasetVersion]:
        return self._load_versions(name)

    def list_datasets(self) -> list[str]:
        return [d.name for d in self._root.iterdir() if d.is_dir()]

    def get_metadata(self, name: str, dataset_id: str) -> DatasetMetadata | None:
        path = self._root / name / f"{dataset_id}_meta.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text())
        # Reconstruct — enums stored as values
        from ml_platform.datasets.base import DatasetFormat, DatasetSplit, ValidationStatus
        raw["format"] = DatasetFormat(raw["format"])
        raw["split"] = DatasetSplit(raw["split"])
        raw["validation_status"] = ValidationStatus(raw["validation_status"])
        return DatasetMetadata(**raw)

    # ── Internals ──────────────────────────────────────────────────────────────

    def _versions_path(self, name: str) -> Path:
        return self._root / name / "versions.json"

    def _load_versions(self, name: str) -> list[DatasetVersion]:
        path = self._versions_path(name)
        if not path.exists():
            return []
        raw_list = json.loads(path.read_text())
        return [DatasetVersion(**r) for r in raw_list]

    def _save_versions(self, name: str, versions: list[DatasetVersion]) -> None:
        self._versions_path(name).write_text(
            json.dumps([asdict(v) for v in versions], indent=2)
        )

    @staticmethod
    def _bump_version(existing: list[DatasetVersion]) -> str:
        if not existing:
            return "1.0.0"
        last = existing[-1].version
        parts = [int(p) for p in last.split(".")]
        parts[2] += 1          # bump patch
        return ".".join(str(p) for p in parts)
