"""
ML Platform — Enterprise Model Registry
=========================================
The authoritative source of truth for all models in the platform.
Every trained model must be registered here before it can be deployed.

Responsibilities:
  - Immutable version history for every model
  - Deployment lifecycle state machine
  - Rollback support (promote any prior version)
  - Filesystem-backed with manifest indexing

Layout:
    <store_root>/
        manifest.json               # index of ALL registered models
        <model_id>/
            <version>/
                model_record.json   # full ModelRegistryRecord
                artifact/           # model weights, configs, etc.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ml_platform.models.schemas import DeploymentStatus


# ── Registry record ────────────────────────────────────────────────────────────

@dataclass
class ModelRegistryRecord:
    """
    Immutable snapshot of a model at the time of registration.
    One record per (model_id, version) pair.
    """
    model_id:           str
    name:               str
    version:            str
    framework:          str                   # ModelFramework.value
    task:               str                   # ModelTask.value
    architecture:       str
    dataset_id:         str
    metrics:            dict[str, float]
    hyperparameters:    dict[str, Any]
    feature_names:      list[str]
    input_shape:        list[int]
    output_shape:       list[int]
    artifact_path:      str
    experiment_run_id:  str
    owner:              str
    deployment_status:  DeploymentStatus      = DeploymentStatus.UNDEPLOYED
    registered_at:      str                   = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    deployed_at:        str                   = ""
    retired_at:         str                   = ""
    tags:               dict[str, str]        = field(default_factory=dict)
    description:        str                   = ""
    is_latest:          bool                  = True
    promoted_from:      str                   = ""   # version this was promoted from


# ── Registry ───────────────────────────────────────────────────────────────────

class ModelRegistry:
    """
    Enterprise model registry with full lifecycle management.

    Lifecycle state machine:
        UNDEPLOYED → STAGING → PRODUCTION
                   ↘ CANARY  ↗
        PRODUCTION → DEPRECATED → RETIRED

    Rollback:
        registry.promote(model_id, target_version="1.2.0")
        # Creates new record cloned from target_version, marked is_latest=True
    """

    def __init__(self, store_root: str = "data/model_registry") -> None:
        self._root = Path(store_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._root / "manifest.json"

    # ── Registration ───────────────────────────────────────────────────────────

    def register(
        self,
        name:            str,
        framework:       str,
        task:            str,
        architecture:    str,
        dataset_id:      str,
        metrics:         dict[str, float],
        hyperparameters: dict[str, Any],
        feature_names:   list[str],
        input_shape:     list[int],
        output_shape:    list[int],
        artifact_source: str,       # local path to copy into the registry
        experiment_run_id: str = "",
        owner:           str = "system",
        tags:            dict[str, str] | None = None,
        description:     str = "",
    ) -> ModelRegistryRecord:
        model_id = str(uuid.uuid4())[:8]
        version  = self._next_version(name)

        record = ModelRegistryRecord(
            model_id=model_id,
            name=name,
            version=version,
            framework=framework,
            task=task,
            architecture=architecture,
            dataset_id=dataset_id,
            metrics=metrics,
            hyperparameters=hyperparameters,
            feature_names=feature_names,
            input_shape=input_shape,
            output_shape=output_shape,
            artifact_path="",    # filled after copy
            experiment_run_id=experiment_run_id,
            owner=owner,
            tags=tags or {},
            description=description,
        )

        # Copy artifact into registry storage
        artifact_dest = self._artifact_dir(model_id, version)
        artifact_dest.mkdir(parents=True, exist_ok=True)
        if Path(artifact_source).exists():
            shutil.copytree(artifact_source, str(artifact_dest / "artifact"), dirs_exist_ok=True)
        record.artifact_path = str(artifact_dest / "artifact")

        # Demote previous latest for this model name
        self._demote_latest(name)

        # Persist
        self._save_record(record)
        self._update_manifest(record)

        return record

    # ── Lifecycle transitions ──────────────────────────────────────────────────

    def transition(self, model_id: str, target_status: DeploymentStatus) -> ModelRegistryRecord:
        """Move a model to a new deployment status."""
        record = self.get(model_id)
        if record is None:
            raise KeyError(f"Model '{model_id}' not found")

        record.deployment_status = target_status
        if target_status == DeploymentStatus.PRODUCTION:
            record.deployed_at = datetime.now(timezone.utc).isoformat()
        if target_status == DeploymentStatus.RETIRED:
            record.retired_at = datetime.now(timezone.utc).isoformat()

        self._save_record(record)
        self._update_manifest(record)
        return record

    def promote_to_staging(self, model_id: str) -> ModelRegistryRecord:
        return self.transition(model_id, DeploymentStatus.STAGING)

    def promote_to_production(self, model_id: str) -> ModelRegistryRecord:
        return self.transition(model_id, DeploymentStatus.PRODUCTION)

    def retire(self, model_id: str) -> ModelRegistryRecord:
        return self.transition(model_id, DeploymentStatus.RETIRED)

    # ── Rollback ───────────────────────────────────────────────────────────────

    def rollback(self, name: str, target_version: str) -> ModelRegistryRecord:
        """
        Rollback: clone record at target_version as a new latest version.
        The original target_version record remains untouched (audit trail).
        """
        source = self._find_by_name_and_version(name, target_version)
        if source is None:
            raise KeyError(f"Version '{target_version}' not found for model '{name}'")

        new_version = self._next_version(name)
        rollback_record = ModelRegistryRecord(
            model_id=str(uuid.uuid4())[:8],
            name=source.name,
            version=new_version,
            framework=source.framework,
            task=source.task,
            architecture=source.architecture,
            dataset_id=source.dataset_id,
            metrics=source.metrics,
            hyperparameters=source.hyperparameters,
            feature_names=source.feature_names,
            input_shape=source.input_shape,
            output_shape=source.output_shape,
            artifact_path=source.artifact_path,     # reuse artifact — no re-copy
            experiment_run_id=source.experiment_run_id,
            owner=source.owner,
            tags={**source.tags, "rollback_from": target_version},
            description=f"Rollback to v{target_version}",
            is_latest=True,
            promoted_from=target_version,
            deployment_status=DeploymentStatus.STAGING,
        )

        self._demote_latest(name)
        self._save_record(rollback_record)
        self._update_manifest(rollback_record)
        return rollback_record

    # ── Queries ────────────────────────────────────────────────────────────────

    def get(self, model_id: str) -> ModelRegistryRecord | None:
        manifest = self._load_manifest()
        raw = manifest.get(model_id)
        if raw is None:
            return None
        return self._deserialize(raw)

    def get_latest(self, name: str) -> ModelRegistryRecord | None:
        manifest = self._load_manifest()
        candidates = [
            self._deserialize(v) for v in manifest.values()
            if v.get("name") == name and v.get("is_latest")
        ]
        return candidates[0] if candidates else None

    def list_all(self) -> list[ModelRegistryRecord]:
        return [self._deserialize(v) for v in self._load_manifest().values()]

    def list_by_name(self, name: str) -> list[ModelRegistryRecord]:
        return [
            self._deserialize(v) for v in self._load_manifest().values()
            if v.get("name") == name
        ]

    def list_deployed(self) -> list[ModelRegistryRecord]:
        return [
            r for r in self.list_all()
            if r.deployment_status == DeploymentStatus.PRODUCTION
        ]

    def search(
        self,
        framework: str | None = None,
        task: str | None = None,
        min_metric: dict[str, float] | None = None,
    ) -> list[ModelRegistryRecord]:
        results = self.list_all()
        if framework:
            results = [r for r in results if r.framework == framework]
        if task:
            results = [r for r in results if r.task == task]
        if min_metric:
            for metric, threshold in min_metric.items():
                results = [r for r in results if r.metrics.get(metric, 0) >= threshold]
        return results

    # ── Internals ──────────────────────────────────────────────────────────────

    def _artifact_dir(self, model_id: str, version: str) -> Path:
        return self._root / model_id / version

    def _record_path(self, model_id: str, version: str) -> Path:
        path = self._root / model_id / version / "model_record.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _save_record(self, record: ModelRegistryRecord) -> None:
        self._record_path(record.model_id, record.version).write_text(
            json.dumps(asdict(record), indent=2, default=str)
        )

    def _load_manifest(self) -> dict:
        if not self._manifest_path.exists():
            return {}
        try:
            return json.loads(self._manifest_path.read_text())
        except json.JSONDecodeError:
            return {}

    def _update_manifest(self, record: ModelRegistryRecord) -> None:
        manifest = self._load_manifest()
        manifest[record.model_id] = asdict(record)
        self._manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

    def _demote_latest(self, name: str) -> None:
        manifest = self._load_manifest()
        changed = False
        for model_id, raw in manifest.items():
            if raw.get("name") == name and raw.get("is_latest"):
                raw["is_latest"] = False
                changed = True
        if changed:
            self._manifest_path.write_text(json.dumps(manifest, indent=2, default=str))

    def _find_by_name_and_version(self, name: str, version: str) -> ModelRegistryRecord | None:
        for raw in self._load_manifest().values():
            if raw.get("name") == name and raw.get("version") == version:
                return self._deserialize(raw)
        return None

    def _next_version(self, name: str) -> str:
        versions = [
            r.get("version", "0.0.0")
            for r in self._load_manifest().values()
            if r.get("name") == name
        ]
        if not versions:
            return "1.0.0"
        parts = [int(p) for p in versions[-1].split(".")]
        parts[2] += 1
        return ".".join(str(p) for p in parts)

    @staticmethod
    def _deserialize(raw: dict) -> ModelRegistryRecord:
        raw = dict(raw)
        raw["deployment_status"] = DeploymentStatus(raw.get("deployment_status", "undeployed"))
        return ModelRegistryRecord(**raw)
