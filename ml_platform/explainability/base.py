"""
ML Platform — Explainability: Base Abstractions
================================================
All explainability methods implement BaseExplainer.
Explanations are model-agnostic at this interface level.

Supported methods (concrete implementations in sibling files):
  - SHAP: tree, linear, deep, kernel explainers
  - LIME: tabular, text, image
  - Attention visualization (for Transformers)
  - Feature importance (native, from tree models)
  - Partial dependence plots (PDPs)

Every explanation produces an ExplanationResult which is
serializable and storable in the artifact store.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ExplainerType(str, Enum):
    SHAP_TREE    = "shap_tree"
    SHAP_LINEAR  = "shap_linear"
    SHAP_DEEP    = "shap_deep"
    SHAP_KERNEL  = "shap_kernel"
    LIME_TABULAR = "lime_tabular"
    LIME_TEXT    = "lime_text"
    LIME_IMAGE   = "lime_image"
    ATTENTION    = "attention_visualization"
    FEATURE_IMP  = "feature_importance"
    PDP          = "partial_dependence"


@dataclass
class FeatureContribution:
    feature_name:  str
    feature_value: Any
    contribution:  float        # positive = pushes toward prediction, negative = against
    abs_contribution: float


@dataclass
class ExplanationResult:
    """
    Standardised output from any explainer.
    Stored in the artifact store alongside the model for auditability.
    """
    explanation_id:      str
    model_id:            str
    explainer_type:      ExplainerType
    input_sample:        Any                          # the input that was explained
    prediction:          Any                          # model output for this input
    feature_contributions: list[FeatureContribution]  = field(default_factory=list)
    base_value:          float | None                 = None   # SHAP expected value
    raw_output:          dict[str, Any]               = field(default_factory=dict)
    generated_at:        str                          = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata:            dict[str, Any]               = field(default_factory=dict)

    def top_features(self, n: int = 10) -> list[FeatureContribution]:
        """Return top-N features by absolute contribution."""
        return sorted(self.feature_contributions, key=lambda f: f.abs_contribution, reverse=True)[:n]


class BaseExplainer(ABC):
    """
    Universal explainability interface.

    Lifecycle:
        explainer = SHAPExplainer(model, background_data)
        result = explainer.explain(input_sample)
        report = explainer.generate_report([result1, result2, ...])
    """

    @abstractmethod
    def explain(self, inputs: Any, model_id: str = "") -> ExplanationResult:
        """
        Generate explanation for a single input sample.
        Must NOT modify the model.
        """
        ...

    @abstractmethod
    def explain_batch(self, inputs: Any, model_id: str = "") -> list[ExplanationResult]:
        """Generate explanations for a batch of inputs."""
        ...

    @abstractmethod
    def generate_report(self, results: list[ExplanationResult]) -> dict[str, Any]:
        """
        Aggregate multiple explanations into a summary report.
        Returns a dict suitable for serialisation to HTML / PDF.
        """
        ...
