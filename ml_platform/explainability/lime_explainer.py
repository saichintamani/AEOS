"""
ML Platform — Explainability: LIME Explainer
============================================
Wraps the `lime` library behind the BaseExplainer interface.
Supports tabular, text, and image data.

LIME perturbs the input, fits a local surrogate linear model,
and reports which features drove the prediction locally.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable

from ml_platform.explainability.base import (
    BaseExplainer, ExplainerType, ExplanationResult, FeatureContribution,
)


class LIMETabularExplainer(BaseExplainer):
    """
    LIME explainer for tabular (structured) data.

    Args:
        predict_fn:       model.predict_proba or model.predict callable
        training_data:    numpy array of training samples (background distribution)
        feature_names:    column names
        class_names:      label names for classification
        mode:             "classification" or "regression"
    """

    def __init__(
        self,
        predict_fn:    Callable,
        training_data: Any,
        feature_names: list[str],
        class_names:   list[str] | None = None,
        mode:          str = "classification",
    ) -> None:
        try:
            from lime.lime_tabular import LimeTabularExplainer as _LIMETabular
        except ImportError:
            raise ImportError("Install lime: pip install lime")

        self._explainer = _LIMETabular(
            training_data=training_data,
            feature_names=feature_names,
            class_names=class_names or [],
            mode=mode,
        )
        self._predict_fn    = predict_fn
        self._feature_names = feature_names
        self._mode          = mode

    def explain(self, inputs: Any, model_id: str = "", num_features: int = 10) -> ExplanationResult:
        import numpy as np
        sample = inputs[0] if hasattr(inputs, "__len__") and len(inputs) > 0 else inputs
        exp = self._explainer.explain_instance(
            sample,
            self._predict_fn,
            num_features=num_features,
        )
        contributions = [
            FeatureContribution(
                feature_name=feat,
                feature_value=None,
                contribution=weight,
                abs_contribution=abs(weight),
            )
            for feat, weight in exp.as_list()
        ]
        return ExplanationResult(
            explanation_id=str(uuid.uuid4())[:8],
            model_id=model_id,
            explainer_type=ExplainerType.LIME_TABULAR,
            input_sample=inputs,
            prediction=self._predict_fn(inputs.reshape(1, -1)) if hasattr(inputs, "reshape") else None,
            feature_contributions=contributions,
            raw_output={"lime_list": exp.as_list()},
        )

    def explain_batch(self, inputs: Any, model_id: str = "") -> list[ExplanationResult]:
        return [self.explain(inputs[i:i+1], model_id) for i in range(len(inputs))]

    def generate_report(self, results: list[ExplanationResult]) -> dict[str, Any]:
        from collections import defaultdict
        totals: dict[str, float] = defaultdict(float)
        for result in results:
            for fc in result.feature_contributions:
                totals[fc.feature_name] += fc.abs_contribution
        ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        return {
            "explainer_type": "lime_tabular",
            "num_samples": len(results),
            "feature_importance": dict(ranked),
        }


class LIMETextExplainer(BaseExplainer):
    """
    LIME explainer for NLP text classification.
    Each token's contribution to the prediction is computed.
    """

    def __init__(
        self,
        predict_fn: Callable,   # takes list[str], returns probability matrix
        class_names: list[str] | None = None,
    ) -> None:
        try:
            from lime.lime_text import LimeTextExplainer as _LIMEText
        except ImportError:
            raise ImportError("Install lime: pip install lime")

        self._explainer  = _LIMEText(class_names=class_names or [])
        self._predict_fn = predict_fn

    def explain(self, inputs: Any, model_id: str = "", num_features: int = 10) -> ExplanationResult:
        text = inputs if isinstance(inputs, str) else str(inputs)
        exp = self._explainer.explain_instance(
            text,
            self._predict_fn,
            num_features=num_features,
        )
        contributions = [
            FeatureContribution(
                feature_name=word,
                feature_value=word,
                contribution=weight,
                abs_contribution=abs(weight),
            )
            for word, weight in exp.as_list()
        ]
        return ExplanationResult(
            explanation_id=str(uuid.uuid4())[:8],
            model_id=model_id,
            explainer_type=ExplainerType.LIME_TEXT,
            input_sample=text,
            prediction=None,
            feature_contributions=contributions,
            raw_output={"lime_list": exp.as_list()},
        )

    def explain_batch(self, inputs: Any, model_id: str = "") -> list[ExplanationResult]:
        return [self.explain(inp, model_id) for inp in inputs]

    def generate_report(self, results: list[ExplanationResult]) -> dict[str, Any]:
        from collections import defaultdict
        totals: dict[str, float] = defaultdict(float)
        for result in results:
            for fc in result.feature_contributions:
                totals[fc.feature_name] += fc.abs_contribution
        ranked = sorted(totals.items(), key=lambda x: x[1], reverse=True)
        return {
            "explainer_type": "lime_text",
            "num_samples": len(results),
            "token_importance": dict(ranked),
        }
