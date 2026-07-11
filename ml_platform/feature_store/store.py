"""
ML Platform — Feature Store
============================
Central registry for fitted feature transformers and feature group definitions.

A FeatureGroup is a named, versioned collection of transforms that can be
applied consistently across training and inference.  This ensures that
inference-time preprocessing exactly mirrors training-time preprocessing.

Backed by filesystem (JSON for specs, pickle for fitted objects).
Future: swap backend for Redis or a dedicated feature store (Feast, Tecton).
"""

from __future__ import annotations

import json
import pickle
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ml_platform.feature_store.base import BaseFeatureTransformer, TransformSpec
from ml_platform.feature_store.transforms import (
    MinMaxNormalizer, StandardScaler, OrdinalEncoder, OneHotEncoder,
    VarianceThresholdSelector, MissingValueImputer,
)


@dataclass
class FeatureGroup:
    """Named, versioned collection of feature transforms."""
    name:         str
    version:      str
    description:  str
    transforms:   list[str]        # ordered list of transform names
    input_schema: dict[str, str]   # col_name → dtype
    output_schema: dict[str, str]
    created_at:   str              = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    dataset_id:   str              = ""
    tags:         dict[str, str]   = field(default_factory=dict)


class FeatureTransformPipeline:
    """
    Ordered chain of BaseFeatureTransformer steps.
    Mirrors sklearn Pipeline but operates on our transform ABC.

    Usage:
        pipeline = FeatureTransformPipeline([
            ("imputer", MissingValueImputer()),
            ("scaler",  StandardScaler()),
            ("encoder", OrdinalEncoder(columns=["category_col"])),
        ])
        X_train = pipeline.fit_transform(X_raw)
        X_test  = pipeline.transform(X_test_raw)
    """

    def __init__(self, steps: list[tuple[str, BaseFeatureTransformer]]) -> None:
        self._steps = steps

    def fit(self, X: Any) -> "FeatureTransformPipeline":
        current = X
        for _, transform in self._steps:
            current = transform.fit_transform(current)
        return self

    def transform(self, X: Any) -> Any:
        current = X
        for _, transform in self._steps:
            current = transform.transform(current)
        return current

    def fit_transform(self, X: Any) -> Any:
        return self.fit(X).transform(X)   # type: ignore[return-value]
    # Note: fit() above already applies transforms; transform() replays them.
    # Correct implementation: fit then transform separately.

    def get_specs(self) -> list[TransformSpec]:
        return [t.get_spec() for _, t in self._steps if t.is_fitted]

    @property
    def steps(self) -> list[tuple[str, BaseFeatureTransformer]]:
        return self._steps


class FeatureStore:
    """
    Filesystem-backed feature store.

    Layout:
        <store_root>/
            groups/
                <group_name>/
                    versions.json
                    <version>/
                        group.json        # FeatureGroup metadata
                        pipeline.pkl      # serialized FeatureTransformPipeline
                        specs.json        # human-readable transform specs
    """

    def __init__(self, store_root: str) -> None:
        self._root = Path(store_root)
        self._root.mkdir(parents=True, exist_ok=True)
        (self._root / "groups").mkdir(exist_ok=True)

    # ── Registration ───────────────────────────────────────────────────────────

    def save_group(
        self,
        name: str,
        pipeline: FeatureTransformPipeline,
        input_schema: dict[str, str],
        output_schema: dict[str, str],
        description: str = "",
        dataset_id: str = "",
        tags: dict[str, str] | None = None,
    ) -> FeatureGroup:
        existing = self._list_versions(name)
        version = self._bump_version(existing)
        group_dir = self._group_version_dir(name, version)
        group_dir.mkdir(parents=True, exist_ok=True)

        group = FeatureGroup(
            name=name,
            version=version,
            description=description,
            transforms=[step_name for step_name, _ in pipeline.steps],
            input_schema=input_schema,
            output_schema=output_schema,
            dataset_id=dataset_id,
            tags=tags or {},
        )

        (group_dir / "group.json").write_text(json.dumps(asdict(group), indent=2))
        with open(group_dir / "pipeline.pkl", "wb") as f:
            pickle.dump(pipeline, f)

        specs = [asdict(s) for s in pipeline.get_specs()]
        (group_dir / "specs.json").write_text(json.dumps(specs, indent=2))

        # Update version index
        existing.append(version)
        self._save_versions(name, existing)

        return group

    # ── Retrieval ──────────────────────────────────────────────────────────────

    def load_pipeline(self, name: str, version: str = "latest") -> FeatureTransformPipeline:
        if version == "latest":
            versions = self._list_versions(name)
            if not versions:
                raise KeyError(f"No feature group '{name}' found")
            version = versions[-1]
        group_dir = self._group_version_dir(name, version)
        with open(group_dir / "pipeline.pkl", "rb") as f:
            return pickle.load(f)

    def get_group(self, name: str, version: str = "latest") -> FeatureGroup:
        if version == "latest":
            versions = self._list_versions(name)
            if not versions:
                raise KeyError(f"No feature group '{name}' found")
            version = versions[-1]
        group_dir = self._group_version_dir(name, version)
        raw = json.loads((group_dir / "group.json").read_text())
        return FeatureGroup(**raw)

    def list_groups(self) -> list[str]:
        groups_dir = self._root / "groups"
        return [d.name for d in groups_dir.iterdir() if d.is_dir()]

    def list_versions(self, name: str) -> list[str]:
        return self._list_versions(name)

    # ── Internals ──────────────────────────────────────────────────────────────

    def _group_version_dir(self, name: str, version: str) -> Path:
        return self._root / "groups" / name / version

    def _versions_path(self, name: str) -> Path:
        return self._root / "groups" / name / "versions.json"

    def _list_versions(self, name: str) -> list[str]:
        path = self._versions_path(name)
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def _save_versions(self, name: str, versions: list[str]) -> None:
        path = self._versions_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(versions))

    @staticmethod
    def _bump_version(existing: list[str]) -> str:
        if not existing:
            return "1.0.0"
        last = existing[-1]
        parts = [int(p) for p in last.split(".")]
        parts[2] += 1
        return ".".join(str(p) for p in parts)
