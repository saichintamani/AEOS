"""
AEOS ML Pipeline — Model Registry
Filesystem-backed model store: pickle for the model, JSON for human-readable metadata.
Layout:
  data/model_registry/
    manifest.json            # index of all models
    <model_id>.pkl           # serialized sklearn model
    <model_id>_meta.json     # ModelRecord as JSON
"""

from __future__ import annotations
import json
import pickle
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class ModelRecord:
    model_id: str
    name: str
    algorithm: str
    dataset_id: str
    metrics: dict
    feature_names: list[str]
    created_at: str
    model_path: str
    meta_path: str


class ModelRegistry:

    def __init__(self, registry_path: str | None = None) -> None:
        self._root = Path(registry_path or settings.ml_model_registry_path)
        self._root.mkdir(parents=True, exist_ok=True)

    # ── Write ──────────────────────────────────────────────────────────────────

    def save(
        self,
        model,
        name: str,
        algorithm: str,
        dataset_id: str,
        metrics: dict,
        feature_names: list[str],
    ) -> ModelRecord:
        model_id = str(uuid.uuid4())[:8]
        model_path = str(self._root / f"{model_id}.pkl")
        meta_path = str(self._root / f"{model_id}_meta.json")

        # Serialize model
        with open(model_path, "wb") as f:
            pickle.dump(model, f)

        record = ModelRecord(
            model_id=model_id,
            name=name,
            algorithm=algorithm,
            dataset_id=dataset_id,
            metrics=metrics,
            feature_names=feature_names,
            created_at=datetime.now(timezone.utc).isoformat(),
            model_path=model_path,
            meta_path=meta_path,
        )

        # Write per-model JSON
        with open(meta_path, "w") as f:
            json.dump(asdict(record), f, indent=2)

        # Update manifest
        manifest = self._load_manifest()
        manifest[model_id] = asdict(record)
        self._save_manifest(manifest)

        log.info(
            "Model saved to registry",
            extra={"ctx_model_id": model_id, "ctx_name": name, "ctx_algorithm": algorithm},
        )
        return record

    # ── Read ───────────────────────────────────────────────────────────────────

    def load(self, model_id: str):
        record = self.get(model_id)
        if record is None:
            raise KeyError(f"Model '{model_id}' not found in registry.")
        with open(record.model_path, "rb") as f:
            return pickle.load(f)

    def get(self, model_id: str) -> ModelRecord | None:
        manifest = self._load_manifest()
        data = manifest.get(model_id)
        if data is None:
            return None
        return ModelRecord(**data)

    def list_models(self) -> list[ModelRecord]:
        manifest = self._load_manifest()
        return [ModelRecord(**v) for v in manifest.values()]

    # ── Delete ─────────────────────────────────────────────────────────────────

    def delete(self, model_id: str) -> bool:
        manifest = self._load_manifest()
        if model_id not in manifest:
            return False
        record = ModelRecord(**manifest.pop(model_id))
        Path(record.model_path).unlink(missing_ok=True)
        Path(record.meta_path).unlink(missing_ok=True)
        self._save_manifest(manifest)
        log.info("Model deleted from registry", extra={"ctx_model_id": model_id})
        return True

    # ── Manifest helpers ───────────────────────────────────────────────────────

    def _manifest_path(self) -> Path:
        return self._root / "manifest.json"

    def _load_manifest(self) -> dict:
        mp = self._manifest_path()
        if not mp.exists():
            return {}
        try:
            return json.loads(mp.read_text())
        except json.JSONDecodeError:
            return {}

    def _save_manifest(self, manifest: dict) -> None:
        self._manifest_path().write_text(json.dumps(manifest, indent=2))
