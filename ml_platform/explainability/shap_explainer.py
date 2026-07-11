"""
ML Platform — Explainability: SHAP Explainer
=============================================
Wraps the `shap` library behind the BaseExplainer interface.
Automatically selects the best SHAP explainer for the model type.

Auto-selection:
  TreeExplainer    → XGBoost, LightGBM, RandomForest, GradientBoosting
  LinearExplainer  → LinearRegression, LogisticRegression, Ridge
  DeepExplainer    → PyTorch, TensorFlow neural nets
  KernelExplainer  → fallback for any model with predict()

Usage:
    explainer = SHAPExplainer.auto(model=trained_model, background=X_train[:100])
    result = explainer.explain(X_test[0:1])
"""

from __future__ import annotations

import uuid
from typing import Any

from ml_platform.explainability.base import (
    BaseExplainer, ExplainerType, ExplanationResult, FeatureContribution,
)


class SHAPExplainer(BaseExplainer):
    """
    SHAP-based explainer.  Wraps TreeExplainer / LinearExplainer / KernelExplainer.
    """

    def __init__(
        self,
        shap_explainer: Any,          # fitted shap.Explainer subclass
        feature_names: list[str],
        explainer_type: ExplainerType = ExplainerType.SHAP_TREE,
    ) -> None:
        self._explainer      = shap_explainer
        self._feature_names  = feature_names
        self._type           = explainer_type

    # ── Factory ────────────────────────────────────────────────────────────────

    @classmethod
    def auto(
        cls,
        model: Any,
        background: Any,
        feature_names: list[str] | None = None,
    ) -> "SHAPExplainer":
        """Auto-select the best SHAP explainer for the model type."""
        try:
            import shap
        except ImportError:
            raise ImportError("Install shap: pip install shap")

        model_type = type(model).__name__.lower()
        feature_names = feature_names or []

        if any(k in model_type for k in ("forest", "tree", "boost", "xgb", "lgbm")):
            return cls(
                shap.TreeExplainer(model),
                feature_names,
                ExplainerType.SHAP_TREE,
            )
        if any(k in model_type for k in ("linear", "logistic", "ridge", "lasso")):
            return cls(
                shap.LinearExplainer(model, background),
                feature_names,
                ExplainerType.SHAP_LINEAR,
            )
        # Fallback: kernel (model-agnostic, slower)
        return cls(
            shap.KernelExplainer(model.predict, background),
            feature_names,
            ExplainerType.SHAP_KERNEL,
        )

    # ── Interface implementation ───────────────────────────────────────────────

    def explain(self, inputs: Any, model_id: str = "") -> ExplanationResult:
        import shap
        import numpy as np

        shap_values = self._explainer.shap_values(inputs)

        # For multi-class: shap_values is a list; take class-1 for binary
        if isinstance(shap_values, list):
            shap_array = shap_values[1] if len(shap_values) == 2 else shap_values[0]
        else:
            shap_array = shap_values

        # Flatten to 1-D if single sample
        row = shap_array[0] if len(shap_array.shape) > 1 else shap_array

        contributions = [
            FeatureContribution(
                feature_name=self._feature_names[i] if i < len(self._feature_names) else f"f{i}",
                feature_value=float(inputs[0][i]) if hasattr(inputs, "__getitem__") else None,
                contribution=float(row[i]),
                abs_contribution=abs(float(row[i])),
            )
            for i in range(len(row))
        ]

        base_value = None
        if hasattr(self._explainer, "expected_value"):
            ev = self._explainer.expected_value
            base_value = float(ev[1]) if isinstance(ev, (list, np.ndarray)) else float(ev)

        return ExplanationResult(
            explanation_id=str(uuid.uuid4())[:8],
            model_id=model_id,
            explainer_type=self._type,
            input_sample=inputs,
            prediction=None,   # caller sets this
            feature_contributions=contributions,
            base_value=base_value,
            raw_output={"shap_values": row.tolist()},
        )

    def explain_batch(self, inputs: Any, model_id: str = "") -> list[ExplanationResult]:
        return [self.explain(inputs[i:i+1], model_id) for i in range(len(inputs))]

    def generate_report(self, results: list[ExplanationResult]) -> dict[str, Any]:
        if not results:
            return {}
        # Aggregate mean |SHAP| per feature
        from collections import defaultdict
        totals: dict[str, float] = defaultdict(float)
        counts: dict[str, int]   = defaultdict(int)
        for result in results:
            for fc in result.feature_contributions:
                totals[fc.feature_name] += fc.abs_contribution
                counts[fc.feature_name] += 1
        mean_importances = {
            feat: round(totals[feat] / counts[feat], 6)
            for feat in totals
        }
        ranked = sorted(mean_importances.items(), key=lambda x: x[1], reverse=True)
        return {
            "explainer_type": self._type.value,
            "num_samples":    len(results),
            "feature_importance": dict(ranked),
            "top_10_features": [f for f, _ in ranked[:10]],
        }
