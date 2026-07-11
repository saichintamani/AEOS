"""
ML Platform — Experiment Tracking
===================================
Tracks every training run: hyperparameters, metrics, artifacts, and lineage.
Filesystem-backed (JSON + JSONL).  Designed with a provider interface so
MLflow, W&B, or Comet can be plugged in without changing call sites.

Architecture:
  ExperimentTracker (facade)
    └── BaseTrackingBackend (ABC)
          ├── LocalTrackingBackend  ← default
          └── MLflowTrackingBackend ← future
"""

from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Schemas ────────────────────────────────────────────────────────────────────

class RunStatus(str):
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    KILLED    = "killed"


@dataclass
class ExperimentRun:
    run_id:         str
    experiment_id:  str
    experiment_name: str
    run_name:       str
    status:         str                        = RunStatus.RUNNING
    hyperparameters: dict[str, Any]            = field(default_factory=dict)
    metrics:        dict[str, list[float]]     = field(default_factory=dict)  # metric → [epoch values]
    tags:           dict[str, str]             = field(default_factory=dict)
    dataset_id:     str                        = ""
    artifact_paths: list[str]                  = field(default_factory=list)
    started_at:     str                        = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ended_at:       str                        = ""
    duration_s:     float                      = 0.0


@dataclass
class Experiment:
    experiment_id:  str
    name:           str
    description:    str
    created_at:     str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    tags:           dict[str, str] = field(default_factory=dict)


# ── Backend ABC ────────────────────────────────────────────────────────────────

class BaseTrackingBackend(ABC):

    @abstractmethod
    def create_experiment(self, name: str, description: str, tags: dict) -> Experiment: ...

    @abstractmethod
    def get_or_create_experiment(self, name: str) -> Experiment: ...

    @abstractmethod
    def start_run(self, experiment_id: str, run_name: str, hyperparameters: dict, tags: dict) -> ExperimentRun: ...

    @abstractmethod
    def log_metrics(self, run_id: str, epoch: int, metrics: dict[str, float]) -> None: ...

    @abstractmethod
    def log_artifact(self, run_id: str, local_path: str, artifact_name: str) -> None: ...

    @abstractmethod
    def end_run(self, run_id: str, status: str, metrics: dict[str, float]) -> None: ...

    @abstractmethod
    def get_run(self, run_id: str) -> ExperimentRun | None: ...

    @abstractmethod
    def list_runs(self, experiment_name: str) -> list[ExperimentRun]: ...

    @abstractmethod
    def compare_runs(self, run_ids: list[str]) -> dict[str, Any]: ...


# ── Local filesystem backend ───────────────────────────────────────────────────

class LocalTrackingBackend(BaseTrackingBackend):
    """
    Stores experiments and runs as JSON files.

    Layout:
        <store_root>/
            experiments.json              # experiment index
            <experiment_name>/
                runs/
                    <run_id>.json         # ExperimentRun snapshot
                    <run_id>_metrics.jsonl # per-epoch metric stream
                artifacts/
                    <run_id>/             # uploaded artifacts
    """

    def __init__(self, store_root: str) -> None:
        self._root = Path(store_root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._experiments_file = self._root / "experiments.json"

    def create_experiment(self, name: str, description: str = "", tags: dict | None = None) -> Experiment:
        exp = Experiment(
            experiment_id=str(uuid.uuid4())[:8],
            name=name,
            description=description,
            tags=tags or {},
        )
        index = self._load_experiments()
        index[name] = asdict(exp)
        self._save_experiments(index)
        return exp

    def get_or_create_experiment(self, name: str) -> Experiment:
        index = self._load_experiments()
        if name in index:
            return Experiment(**index[name])
        return self.create_experiment(name)

    def start_run(
        self,
        experiment_id: str,
        run_name: str,
        hyperparameters: dict | None = None,
        tags: dict | None = None,
        experiment_name: str = "",
    ) -> ExperimentRun:
        run = ExperimentRun(
            run_id=str(uuid.uuid4())[:8],
            experiment_id=experiment_id,
            experiment_name=experiment_name,
            run_name=run_name,
            hyperparameters=hyperparameters or {},
            tags=tags or {},
        )
        self._save_run(run)
        return run

    def log_metrics(self, run_id: str, epoch: int, metrics: dict[str, float]) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        metrics_path = self._metrics_path(run.experiment_name, run_id)
        record = {"epoch": epoch, **metrics}
        with open(metrics_path, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Update latest snapshot
        for key, val in metrics.items():
            run.metrics.setdefault(key, []).append(val)
        self._save_run(run)

    def log_artifact(self, run_id: str, local_path: str, artifact_name: str) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        run.artifact_paths.append(local_path)
        self._save_run(run)

    def end_run(self, run_id: str, status: str = "completed", metrics: dict | None = None) -> None:
        run = self.get_run(run_id)
        if run is None:
            return
        run.status = status
        run.ended_at = datetime.now(timezone.utc).isoformat()
        if metrics:
            for key, val in metrics.items():
                run.metrics[key] = run.metrics.get(key, []) + [val]
        self._save_run(run)

    def get_run(self, run_id: str) -> ExperimentRun | None:
        # Search across all experiments
        for exp_dir in self._root.iterdir():
            if not exp_dir.is_dir():
                continue
            run_file = exp_dir / "runs" / f"{run_id}.json"
            if run_file.exists():
                return ExperimentRun(**json.loads(run_file.read_text()))
        return None

    def list_runs(self, experiment_name: str) -> list[ExperimentRun]:
        runs_dir = self._root / experiment_name / "runs"
        if not runs_dir.exists():
            return []
        runs = []
        for f in runs_dir.glob("*.json"):
            if not f.name.endswith("_metrics.jsonl"):
                try:
                    runs.append(ExperimentRun(**json.loads(f.read_text())))
                except Exception:
                    pass
        return sorted(runs, key=lambda r: r.started_at, reverse=True)

    def compare_runs(self, run_ids: list[str]) -> dict[str, Any]:
        runs = [self.get_run(r) for r in run_ids if self.get_run(r)]
        return {
            "run_ids": run_ids,
            "metrics": {
                r.run_id: {k: v[-1] if v else None for k, v in r.metrics.items()}
                for r in runs if r
            },
            "hyperparameters": {
                r.run_id: r.hyperparameters for r in runs if r
            },
        }

    # ── Internals ──────────────────────────────────────────────────────────────

    def _run_path(self, experiment_name: str, run_id: str) -> Path:
        path = self._root / experiment_name / "runs" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _metrics_path(self, experiment_name: str, run_id: str) -> Path:
        path = self._root / experiment_name / "runs" / f"{run_id}_metrics.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _save_run(self, run: ExperimentRun) -> None:
        self._run_path(run.experiment_name, run.run_id).write_text(
            json.dumps(asdict(run), indent=2)
        )

    def _load_experiments(self) -> dict:
        if not self._experiments_file.exists():
            return {}
        try:
            return json.loads(self._experiments_file.read_text())
        except json.JSONDecodeError:
            return {}

    def _save_experiments(self, index: dict) -> None:
        self._experiments_file.write_text(json.dumps(index, indent=2))


# ── Facade ─────────────────────────────────────────────────────────────────────

class ExperimentTracker:
    """
    Primary interface for experiment tracking.
    The training engine calls this — never the backend directly.

    Future: swap LocalTrackingBackend for MLflowTrackingBackend by
    changing the constructor argument — all call sites remain unchanged.
    """

    def __init__(self, backend: BaseTrackingBackend | None = None, store_root: str = "data/experiments") -> None:
        self._backend = backend or LocalTrackingBackend(store_root)

    def start_run(self, experiment_name: str, run_name: str, config: Any) -> ExperimentRun:
        exp = self._backend.get_or_create_experiment(experiment_name)
        hyperparams = {}
        if hasattr(config, "optimizer"):
            hyperparams = {
                "learning_rate": config.optimizer.learning_rate,
                "optimizer": config.optimizer.type.value,
                "batch_size": config.batch_size,
                "max_epochs": config.max_epochs,
                **config.extra,
            }
        return self._backend.start_run(
            experiment_id=exp.experiment_id,
            experiment_name=experiment_name,
            run_name=run_name,
            hyperparameters=hyperparams,
            tags=config.tags if hasattr(config, "tags") else {},
        )

    def log_metrics(self, run_id: str, epoch: int, metrics: dict[str, float]) -> None:
        self._backend.log_metrics(run_id, epoch, metrics)

    def log_artifact(self, run_id: str, local_path: str, artifact_name: str = "") -> None:
        self._backend.log_artifact(run_id, local_path, artifact_name or local_path)

    def end_run(self, run_id: str, status: str = "completed", metrics: dict | None = None) -> None:
        self._backend.end_run(run_id, status, metrics or {})

    def get_run(self, run_id: str) -> ExperimentRun | None:
        return self._backend.get_run(run_id)

    def list_runs(self, experiment_name: str) -> list[ExperimentRun]:
        return self._backend.list_runs(experiment_name)

    def compare_runs(self, run_ids: list[str]) -> dict[str, Any]:
        return self._backend.compare_runs(run_ids)
