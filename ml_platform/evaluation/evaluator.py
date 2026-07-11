"""
ML Platform — Evaluation Layer
================================
Model-agnostic evaluation engine.
Computes task-specific metrics and produces an EvaluationReport.

Supports:
  - Classification (binary + multi-class)
  - Regression
  - Ranking (NDCG, MAP)
  - Object detection (IoU, mAP) — future
  - NLP (BLEU, ROUGE) — future
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class EvaluationTask(str, Enum):
    BINARY_CLASSIFICATION  = "binary_classification"
    MULTICLASS_CLASSIFICATION = "multiclass_classification"
    REGRESSION             = "regression"
    RANKING                = "ranking"
    OBJECT_DETECTION       = "object_detection"
    NLP_GENERATION         = "nlp_generation"


@dataclass
class EvaluationReport:
    model_id:       str
    dataset_id:     str
    task:           EvaluationTask
    metrics:        dict[str, float]
    evaluated_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    num_samples:    int = 0
    confusion_matrix: list[list[int]] | None = None
    classification_report: str = ""
    metadata:       dict[str, Any] = field(default_factory=dict)


class BaseEvaluator(ABC):

    @abstractmethod
    def evaluate(self, model: Any, dataset: Any) -> EvaluationReport: ...


class ClassificationEvaluator(BaseEvaluator):
    """Binary and multi-class classification metrics."""

    def evaluate(self, model: Any, dataset: Any) -> EvaluationReport:
        from sklearn.metrics import (
            accuracy_score, precision_score, recall_score, f1_score,
            roc_auc_score, classification_report, confusion_matrix,
        )
        import numpy as np

        X_test = dataset.get("X_test")
        y_test = dataset.get("y_test")
        y_pred = model.predict(X_test)

        n_classes = len(np.unique(y_test))
        avg = "binary" if n_classes == 2 else "weighted"
        task = (
            EvaluationTask.BINARY_CLASSIFICATION
            if n_classes == 2
            else EvaluationTask.MULTICLASS_CLASSIFICATION
        )

        metrics = {
            "accuracy":  round(float(accuracy_score(y_test, y_pred)), 4),
            "precision": round(float(precision_score(y_test, y_pred, average=avg, zero_division=0)), 4),
            "recall":    round(float(recall_score(y_test, y_pred, average=avg, zero_division=0)), 4),
            "f1":        round(float(f1_score(y_test, y_pred, average=avg, zero_division=0)), 4),
        }

        # AUC-ROC (binary only with predict_proba)
        if n_classes == 2 and hasattr(model, "predict_proba"):
            try:
                y_proba = model.predict_proba(X_test)[:, 1]
                metrics["roc_auc"] = round(float(roc_auc_score(y_test, y_proba)), 4)
            except Exception:
                pass

        cm = confusion_matrix(y_test, y_pred).tolist()
        clf_report = classification_report(y_test, y_pred, zero_division=0)

        return EvaluationReport(
            model_id="",
            dataset_id="",
            task=task,
            metrics=metrics,
            num_samples=len(y_test),
            confusion_matrix=cm,
            classification_report=clf_report,
        )


class RegressionEvaluator(BaseEvaluator):
    """Regression metrics: RMSE, MAE, R²."""

    def evaluate(self, model: Any, dataset: Any) -> EvaluationReport:
        from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
        import numpy as np

        X_test = dataset.get("X_test")
        y_test = dataset.get("y_test")
        y_pred = model.predict(X_test)

        mse = float(mean_squared_error(y_test, y_pred))
        metrics = {
            "rmse": round(float(np.sqrt(mse)), 4),
            "mae":  round(float(mean_absolute_error(y_test, y_pred)), 4),
            "mse":  round(mse, 4),
            "r2":   round(float(r2_score(y_test, y_pred)), 4),
        }

        return EvaluationReport(
            model_id="",
            dataset_id="",
            task=EvaluationTask.REGRESSION,
            metrics=metrics,
            num_samples=len(y_test),
        )


def get_evaluator(task: EvaluationTask) -> BaseEvaluator:
    """Factory: return the correct evaluator for the task type."""
    return {
        EvaluationTask.BINARY_CLASSIFICATION:     ClassificationEvaluator,
        EvaluationTask.MULTICLASS_CLASSIFICATION: ClassificationEvaluator,
        EvaluationTask.REGRESSION:                RegressionEvaluator,
    }.get(task, ClassificationEvaluator)()
