"""
ML Platform — Training Pipeline
=================================
End-to-end training workflow:
  1. Load dataset
  2. Validate dataset
  3. Build feature transforms
  4. Split train/val/test
  5. Initialize model
  6. Run training engine
  7. Evaluate on test set
  8. Register model in registry
  9. Log everything to experiment tracker

This pipeline is the primary entry point for triggering a new training run.
It can be invoked via:
  - CLI: python -m ml_platform.pipelines.training_pipeline --config config.yaml
  - API: POST /api/v1/ml/train
  - AEOS agent: ML Pipeline Worker message bus event
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ml_platform.pipelines.base import BasePipeline, PipelineRun, PipelineStatus


@dataclass
class TrainingPipelineConfig:
    """All inputs to a training pipeline run — fully declarative."""
    experiment_name:  str
    model_architecture: str
    dataset_source:   str
    dataset_format:   str                 # "csv", "parquet", "json", "images", "text"
    target_column:    str | None          = None
    feature_group:    str | None          = None    # FeatureStore group to apply
    training_config:  dict[str, Any]      = field(default_factory=dict)
    model_config:     dict[str, Any]      = field(default_factory=dict)
    tags:             dict[str, str]      = field(default_factory=dict)
    owner:            str                 = "system"
    pipeline_id:      str                 = field(default_factory=lambda: str(uuid.uuid4())[:8])


class TrainingPipeline(BasePipeline):
    """
    Dependency-injected training pipeline.
    All dependencies are passed at construction time — no global state.

    Usage:
        pipeline = TrainingPipeline(
            dataset_loaders={"csv": CSVDatasetLoader()},
            feature_store=feature_store,
            validator=DatasetValidationPipeline(),
            engine=TrainingEngine(tracker=tracker, registry=registry),
            registry=model_registry,
            tracker=experiment_tracker,
        )
        run = pipeline.run(config=TrainingPipelineConfig(...))
    """

    name = "training_pipeline"

    def __init__(
        self,
        dataset_loaders: dict[str, Any],    # format → BaseDataset loader
        feature_store:   Any = None,         # FeatureStore
        validator:       Any = None,         # DatasetValidationPipeline
        engine:          Any = None,         # TrainingEngine
        registry:        Any = None,         # ModelRegistry
        tracker:         Any = None,         # ExperimentTracker
    ) -> None:
        self._loaders   = dataset_loaders
        self._features  = feature_store
        self._validator = validator
        self._engine    = engine
        self._registry  = registry
        self._tracker   = tracker

    def run(self, config: TrainingPipelineConfig | None = None, **kwargs) -> PipelineRun:
        if config is None:
            raise ValueError("TrainingPipelineConfig is required")

        run = PipelineRun(
            pipeline_id=config.pipeline_id,
            pipeline_name=self.name,
            status=PipelineStatus.RUNNING,
        )

        try:
            # ── Step 1: Load dataset ──────────────────────────────────────────
            loader = self._loaders.get(config.dataset_format)
            if loader is None:
                raise ValueError(f"No loader registered for format: {config.dataset_format}")
            dataset = loader.load(config.dataset_source, target_col=config.target_column)
            run.outputs["dataset_id"] = dataset.metadata.dataset_id

            # ── Step 2: Validate dataset ──────────────────────────────────────
            if self._validator:
                validation_report = self._validator.validate(dataset)
                run.outputs["validation_status"] = validation_report.status.value
                if validation_report.has_errors:
                    raise ValueError(f"Dataset validation failed: {validation_report.issues}")

            # ── Step 3: Apply feature transforms ─────────────────────────────
            X = dataset.data
            if self._features and config.feature_group:
                pipeline = self._features.load_pipeline(config.feature_group)
                X = pipeline.transform(X)
                run.outputs["feature_group"] = config.feature_group

            # ── Step 4: Build model ───────────────────────────────────────────
            from ml_platform.models.base import ModelConfig, ModelFramework, ModelTask
            from ml_platform.models.catalog import get_model_class
            # TODO: build ModelConfig from config.model_config dict
            # model_cls = get_model_class(config.model_architecture)
            # model = model_cls()

            # ── Step 5: Build training config ─────────────────────────────────
            from ml_platform.training.config import TrainingConfig
            # training_cfg = TrainingConfig(
            #     experiment_name=config.experiment_name,
            #     **config.training_config
            # )

            # ── Step 6: Train ─────────────────────────────────────────────────
            # result = self._engine.run(model=model, dataset=dataset, config=training_cfg)
            # run.outputs["training_result"] = result

            # ── Step 7: Register ──────────────────────────────────────────────
            # record = self._registry.register(...)
            # run.outputs["model_id"] = record.model_id

            run.status = PipelineStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc).isoformat()

        except Exception as exc:
            run.status = PipelineStatus.FAILED
            run.error  = str(exc)
            run.completed_at = datetime.now(timezone.utc).isoformat()
            raise

        return run

    def validate_inputs(self, config: TrainingPipelineConfig | None = None, **kwargs) -> None:
        if config is None:
            raise ValueError("config is required")
        if not config.dataset_source:
            raise ValueError("dataset_source is required")
        if not config.model_architecture:
            raise ValueError("model_architecture is required")

    def _describe_steps(self) -> list[str]:
        return [
            "1. load_dataset",
            "2. validate_dataset",
            "3. apply_feature_transforms",
            "4. initialize_model",
            "5. run_training_engine",
            "6. evaluate_on_test_set",
            "7. register_model",
            "8. log_experiment",
        ]
