"""
AEOS ML Pipeline — Model Evaluator
Computes classification and regression metrics from trained sklearn models.
"""

from __future__ import annotations
import numpy as np
from app.core.logger import get_logger

log = get_logger(__name__)


class ModelEvaluator:

    def evaluate(self, model, X_test, y_test, task_type: str | None = None) -> dict:
        detected = task_type or self._detect_task_type(y_test)
        log.info("Evaluating model", extra={"ctx_task_type": detected})
        if detected == "classification":
            return self.evaluate_classification(model, X_test, y_test)
        return self.evaluate_regression(model, X_test, y_test)

    def evaluate_classification(self, model, X_test, y_test) -> dict:
        from sklearn.metrics import (
            accuracy_score, precision_score, recall_score,
            f1_score, classification_report,
        )
        y_pred = model.predict(X_test)
        avg = "binary" if len(set(y_test)) == 2 else "weighted"
        return {
            "task_type": "classification",
            "accuracy":  round(float(accuracy_score(y_test, y_pred)), 4),
            "precision": round(float(precision_score(y_test, y_pred, average=avg, zero_division=0)), 4),
            "recall":    round(float(recall_score(y_test, y_pred, average=avg, zero_division=0)), 4),
            "f1":        round(float(f1_score(y_test, y_pred, average=avg, zero_division=0)), 4),
            "report":    classification_report(y_test, y_pred, zero_division=0),
        }

    def evaluate_regression(self, model, X_test, y_test) -> dict:
        from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
        y_pred = model.predict(X_test)
        mse = float(mean_squared_error(y_test, y_pred))
        return {
            "task_type": "regression",
            "rmse": round(float(np.sqrt(mse)), 4),
            "mae":  round(float(mean_absolute_error(y_test, y_pred)), 4),
            "mse":  round(mse, 4),
            "r2":   round(float(r2_score(y_test, y_pred)), 4),
        }

    def _detect_task_type(self, y) -> str:
        unique = len(set(y.tolist() if hasattr(y, "tolist") else list(y)))
        return "classification" if unique <= 20 else "regression"
