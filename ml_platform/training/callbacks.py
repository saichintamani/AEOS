"""
ML Platform — Training Engine: Callbacks
=========================================
Callbacks are injected into the training engine and called at defined
lifecycle hooks.  They observe — they do not modify model weights.

Implementing a custom callback requires only extending BaseCallback
and registering it with the training engine.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class EpochState:
    """Snapshot of training state passed to callbacks each epoch."""
    epoch:        int
    max_epochs:   int
    train_loss:   float
    val_loss:     float | None
    metrics:      dict[str, float]
    model:        Any              # the BaseModel instance
    stop_training: bool = False    # callbacks can set this to True


# ── Base ───────────────────────────────────────────────────────────────────────

class BaseCallback(ABC):
    """Training lifecycle hook."""

    def on_training_start(self, config: Any) -> None:
        """Called once before the first epoch."""

    def on_epoch_start(self, state: EpochState) -> None:
        """Called at the beginning of each epoch."""

    @abstractmethod
    def on_epoch_end(self, state: EpochState) -> None:
        """Called at the end of each epoch. May set state.stop_training = True."""

    def on_training_end(self, state: EpochState) -> None:
        """Called once after the last epoch (or early stop)."""

    def on_exception(self, exc: Exception) -> None:
        """Called if the training loop raises an unhandled exception."""


# ── Early Stopping ─────────────────────────────────────────────────────────────

class EarlyStoppingCallback(BaseCallback):
    """
    Stops training when a monitored metric stops improving.

    Args:
        monitor:   Metric key in EpochState.metrics to watch.
        patience:  Number of epochs with no improvement before stopping.
        min_delta: Minimum change to qualify as improvement.
        mode:      "min" (lower is better) or "max" (higher is better).
        restore_best_weights: If True, saves best weights and restores on stop.
    """

    def __init__(
        self,
        monitor:   str   = "val_loss",
        patience:  int   = 5,
        min_delta: float = 1e-4,
        mode:      str   = "min",
        restore_best_weights: bool = True,
    ) -> None:
        self._monitor  = monitor
        self._patience = patience
        self._min_delta = min_delta
        self._mode     = mode
        self._restore  = restore_best_weights
        self._best_value: float | None = None
        self._wait = 0
        self._best_epoch = 0

    def on_epoch_end(self, state: EpochState) -> None:
        current = state.metrics.get(self._monitor) or state.val_loss
        if current is None:
            return

        improved = self._is_improved(current)

        if improved:
            self._best_value = current
            self._best_epoch = state.epoch
            self._wait = 0
            # TODO: if restore_best_weights: snapshot model weights here
        else:
            self._wait += 1
            if self._wait >= self._patience:
                state.stop_training = True
                # TODO: if restore_best_weights: restore snapshotted weights

    def _is_improved(self, current: float) -> bool:
        if self._best_value is None:
            return True
        if self._mode == "min":
            return current < self._best_value - self._min_delta
        return current > self._best_value + self._min_delta


# ── Checkpointing ──────────────────────────────────────────────────────────────

class ModelCheckpointCallback(BaseCallback):
    """
    Saves model weights at configurable intervals.

    Maintains a rolling window of the N best checkpoints to avoid
    unbounded disk growth.
    """

    def __init__(
        self,
        directory:      str  = "checkpoints",
        monitor:        str  = "val_loss",
        save_best_only: bool = True,
        save_every_n:   int  = 1,
        max_to_keep:    int  = 3,
        mode:           str  = "min",
    ) -> None:
        self._dir           = Path(directory)
        self._monitor       = monitor
        self._best_only     = save_best_only
        self._every_n       = save_every_n
        self._max_to_keep   = max_to_keep
        self._mode          = mode
        self._best_value: float | None = None
        self._saved: list[Path] = []

    def on_training_start(self, config: Any) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    def on_epoch_end(self, state: EpochState) -> None:
        if state.epoch % self._every_n != 0:
            return

        current = state.metrics.get(self._monitor) or state.val_loss
        if self._best_only and not self._is_improved(current):
            return

        checkpoint_path = self._dir / f"epoch_{state.epoch:04d}"
        state.model.save(str(checkpoint_path))
        self._saved.append(checkpoint_path)

        if current is not None:
            self._best_value = current

        # Prune old checkpoints
        while len(self._saved) > self._max_to_keep:
            old = self._saved.pop(0)
            # TODO: delete old checkpoint files from disk

    def _is_improved(self, current: float | None) -> bool:
        if current is None or self._best_value is None:
            return True
        if self._mode == "min":
            return current < self._best_value
        return current > self._best_value


# ── Learning Rate Logger ───────────────────────────────────────────────────────

class LRSchedulerCallback(BaseCallback):
    """
    Steps a learning rate scheduler after each epoch.
    Works with PyTorch LR schedulers; extend for other frameworks.
    """

    def __init__(self, scheduler: Any) -> None:
        self._scheduler = scheduler

    def on_epoch_end(self, state: EpochState) -> None:
        if hasattr(self._scheduler, "step"):
            # ReduceLROnPlateau needs the metric
            try:
                self._scheduler.step(state.metrics.get("val_loss", state.val_loss))
            except TypeError:
                self._scheduler.step()


# ── Metrics Logger ─────────────────────────────────────────────────────────────

class MetricsLoggerCallback(BaseCallback):
    """
    Appends per-epoch metrics to a JSONL file.
    Consumed by the experiment tracker dashboard.
    """

    def __init__(self, log_path: str) -> None:
        self._path = Path(log_path)

    def on_training_start(self, config: Any) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("")   # truncate / create

    def on_epoch_end(self, state: EpochState) -> None:
        record = {
            "epoch":      state.epoch,
            "train_loss": state.train_loss,
            "val_loss":   state.val_loss,
            **state.metrics,
        }
        with open(self._path, "a") as f:
            f.write(json.dumps(record) + "\n")
