"""
ML Platform — Feature Store: Base Abstractions
===============================================
All feature transformers implement BaseFeatureTransformer.
Transformers are composable into a FeatureTransformPipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TransformType(str, Enum):
    NORMALIZATION = "normalization"
    SCALING       = "scaling"
    ENCODING      = "encoding"
    SELECTION     = "selection"
    IMPUTATION    = "imputation"
    BINNING       = "binning"
    EMBEDDING     = "embedding"


@dataclass
class TransformSpec:
    """Serializable description of a fitted transform."""
    name:       str
    type:       TransformType
    columns:    list[str]
    parameters: dict[str, Any]  = field(default_factory=dict)
    fitted_at:  str             = ""


class BaseFeatureTransformer(ABC):
    """
    Stateful feature transform.  Follows sklearn's fit/transform/inverse_transform API
    so transforms are directly composable with sklearn Pipelines.

    State machine:
        UNFITTED → fit() → FITTED → transform() → outputs
    """

    def __init__(self, columns: list[str] | None = None) -> None:
        self._columns = columns or []   # None/empty = apply to all applicable cols
        self._fitted  = False
        self._spec: TransformSpec | None = None

    @abstractmethod
    def fit(self, X: Any) -> "BaseFeatureTransformer":
        """Learn parameters from X. Must set self._fitted = True."""
        ...

    @abstractmethod
    def transform(self, X: Any) -> Any:
        """Apply transform. Must raise if not fitted."""
        ...

    @abstractmethod
    def inverse_transform(self, X: Any) -> Any:
        """Reverse the transform (where applicable)."""
        ...

    def fit_transform(self, X: Any) -> Any:
        return self.fit(X).transform(X)

    @abstractmethod
    def get_spec(self) -> TransformSpec:
        """Return a serializable description of this fitted transform."""
        ...

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    def _assert_fitted(self) -> None:
        if not self._fitted:
            raise RuntimeError(f"{self.__class__.__name__} must be fitted before transform()")
