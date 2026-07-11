"""
ML Platform — Training Engine
==============================
Orchestrates a complete training run from configuration to model artifact.

Responsibilities:
  - Resolve device (GPU / CPU / MPS)
  - Build data loaders from DatasetRecord
  - Instantiate and initialize the model from ModelConfig + ModelCatalog
  - Drive the epoch loop with callback hooks
  - Emit training events to the ExperimentTracker
  - Register the trained model in the ModelRegistry
  - Return a TrainingResult with full provenance

The engine itself contains zero model-specific logic.  Everything model-
specific lives inside BaseModel implementations.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ml_platform.training.callbacks import BaseCallback, EpochState
from ml_platform.training.config import DeviceType, TrainingConfig


# ── Result schema ──────────────────────────────────────────────────────────────

@dataclass
class TrainingResult:
    """Returned by TrainingEngine.run() — full provenance of one training run."""
    run_id:         str
    experiment_id:  str
    model_id:       str
    model_version:  str
    dataset_id:     str
    epochs_run:     int
    best_epoch:     int
    final_metrics:  dict[str, float]
    artifact_path:  str
    training_time_s: float
    stopped_early:  bool = False
    config:         TrainingConfig | None = None
    metadata:       dict[str, Any]        = field(default_factory=dict)


# ── Device resolution ──────────────────────────────────────────────────────────

class DeviceResolver:
    """Resolves the target compute device at runtime."""

    @staticmethod
    def resolve(requested: DeviceType) -> str:
        if requested == DeviceType.AUTO:
            return DeviceResolver._auto_detect()
        return requested.value

    @staticmethod
    def _auto_detect() -> str:
        try:
            import torch
            if torch.cuda.is_available():
                return "cuda"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"


# ── Training engine ────────────────────────────────────────────────────────────

class TrainingEngine:
    """
    Configuration-driven training orchestrator.

    Usage:
        engine = TrainingEngine(
            experiment_tracker=tracker,
            model_registry=registry,
            callbacks=[EarlyStoppingCallback(), ModelCheckpointCallback(...)],
        )
        result = engine.run(
            model=model,
            dataset=dataset_record,
            config=training_config,
        )
    """

    def __init__(
        self,
        experiment_tracker: Any = None,    # ExperimentTracker (injected)
        model_registry:     Any = None,    # ModelRegistry (injected)
        callbacks:          list[BaseCallback] | None = None,
    ) -> None:
        self._tracker   = experiment_tracker
        self._registry  = model_registry
        self._callbacks = callbacks or []

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        model: Any,            # BaseModel implementation
        dataset: Any,          # DatasetRecord
        config: TrainingConfig,
    ) -> TrainingResult:
        run_id      = str(uuid.uuid4())[:8]
        start_time  = time.monotonic()
        device      = DeviceResolver.resolve(config.device)

        self._log_start(run_id, config, device)
        self._notify_start(config)

        # Start experiment run
        experiment_run = None
        if self._tracker:
            experiment_run = self._tracker.start_run(
                experiment_name=config.experiment_name,
                run_name=config.run_name or run_id,
                config=config,
            )

        # Initialize model
        model.initialize(model.config)

        best_metrics: dict[str, float] = {}
        best_epoch = 0
        stopped_early = False

        try:
            for epoch in range(1, config.max_epochs + 1):
                # -- Forward training step (delegated entirely to model) --
                train_loss = model._train_one_epoch(epoch, config, device)
                val_loss, val_metrics = model._validate_one_epoch(epoch, config, device)

                metrics = {"val_loss": val_loss, **val_metrics}

                # -- Callback epoch_end hook --
                state = EpochState(
                    epoch=epoch,
                    max_epochs=config.max_epochs,
                    train_loss=train_loss,
                    val_loss=val_loss,
                    metrics=metrics,
                    model=model,
                )
                self._notify_epoch_end(state)

                # -- Log to experiment tracker --
                if self._tracker and experiment_run:
                    self._tracker.log_metrics(experiment_run.run_id, epoch, metrics)

                # -- Track best --
                if val_loss is not None and (not best_metrics or val_loss < best_metrics.get("val_loss", float("inf"))):
                    best_metrics = metrics
                    best_epoch = epoch

                # -- Early stop check --
                if state.stop_training:
                    stopped_early = True
                    break

        except Exception as exc:
            self._notify_exception(exc)
            raise

        finally:
            elapsed = time.monotonic() - start_time

        # Save artifact
        artifact_path = self._save_artifact(model, run_id, config)

        # Register model
        model_id, model_version = "", "1.0.0"
        if self._registry:
            record = self._registry.register(
                model=model,
                name=config.experiment_name,
                dataset_id=dataset.metadata.dataset_id if hasattr(dataset, "metadata") else "",
                metrics=best_metrics,
                config=config,
                artifact_path=artifact_path,
                run_id=run_id,
            )
            model_id    = record.model_id
            model_version = record.version

        # End experiment run
        if self._tracker and experiment_run:
            self._tracker.end_run(experiment_run.run_id, status="completed", metrics=best_metrics)

        self._notify_end(state)

        return TrainingResult(
            run_id=run_id,
            experiment_id=experiment_run.experiment_id if experiment_run else "",
            model_id=model_id,
            model_version=model_version,
            dataset_id=dataset.metadata.dataset_id if hasattr(dataset, "metadata") else "",
            epochs_run=epoch,
            best_epoch=best_epoch,
            final_metrics=best_metrics,
            artifact_path=artifact_path,
            training_time_s=round(elapsed, 2),
            stopped_early=stopped_early,
            config=config,
        )

    # ── Callback dispatch ──────────────────────────────────────────────────────

    def add_callback(self, callback: BaseCallback) -> None:
        self._callbacks.append(callback)

    def _notify_start(self, config: TrainingConfig) -> None:
        for cb in self._callbacks:
            cb.on_training_start(config)

    def _notify_epoch_end(self, state: EpochState) -> None:
        for cb in self._callbacks:
            cb.on_epoch_end(state)

    def _notify_end(self, state: EpochState) -> None:
        for cb in self._callbacks:
            cb.on_training_end(state)

    def _notify_exception(self, exc: Exception) -> None:
        for cb in self._callbacks:
            try:
                cb.on_exception(exc)
            except Exception:
                pass

    # ── Internals ──────────────────────────────────────────────────────────────

    def _save_artifact(self, model: Any, run_id: str, config: TrainingConfig) -> str:
        artifact_dir = f"{config.checkpoint.directory}/{config.experiment_name}/{run_id}/final"
        return model.save(artifact_dir)

    def _log_start(self, run_id: str, config: TrainingConfig, device: str) -> None:
        # TODO: wire to platform logger
        pass
