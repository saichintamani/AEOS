"""
ML Platform — Feature Store: Transform Implementations
======================================================
All transforms derive from BaseFeatureTransformer.
Backed by sklearn under the hood; swappable at the ABC boundary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ml_platform.feature_store.base import (
    BaseFeatureTransformer, TransformSpec, TransformType,
)


# ── Normalization (Min-Max) ────────────────────────────────────────────────────

class MinMaxNormalizer(BaseFeatureTransformer):
    """Scale features to [feature_range_min, feature_range_max]."""

    def __init__(self, columns: list[str] | None = None, feature_range: tuple = (0, 1)) -> None:
        super().__init__(columns)
        self._range = feature_range
        self._scaler = None

    def fit(self, X: Any) -> "MinMaxNormalizer":
        from sklearn.preprocessing import MinMaxScaler
        import pandas as pd
        df = X[self._columns] if self._columns and isinstance(X, pd.DataFrame) else X
        self._scaler = MinMaxScaler(feature_range=self._range)
        self._scaler.fit(df)
        self._fitted = True
        return self

    def transform(self, X: Any) -> Any:
        self._assert_fitted()
        import pandas as pd
        if isinstance(X, pd.DataFrame) and self._columns:
            result = X.copy()
            result[self._columns] = self._scaler.transform(X[self._columns])
            return result
        return self._scaler.transform(X)

    def inverse_transform(self, X: Any) -> Any:
        self._assert_fitted()
        return self._scaler.inverse_transform(X)

    def get_spec(self) -> TransformSpec:
        return TransformSpec(
            name="MinMaxNormalizer",
            type=TransformType.NORMALIZATION,
            columns=self._columns,
            parameters={"feature_range": list(self._range)},
            fitted_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Standard Scaling (Z-score) ─────────────────────────────────────────────────

class StandardScaler(BaseFeatureTransformer):
    """Zero mean, unit variance scaling."""

    def __init__(self, columns: list[str] | None = None, with_std: bool = True) -> None:
        super().__init__(columns)
        self._with_std = with_std
        self._scaler = None

    def fit(self, X: Any) -> "StandardScaler":
        from sklearn.preprocessing import StandardScaler as SKStandardScaler
        import pandas as pd
        df = X[self._columns] if self._columns and isinstance(X, pd.DataFrame) else X
        self._scaler = SKStandardScaler(with_std=self._with_std)
        self._scaler.fit(df)
        self._fitted = True
        return self

    def transform(self, X: Any) -> Any:
        self._assert_fitted()
        import pandas as pd
        if isinstance(X, pd.DataFrame) and self._columns:
            result = X.copy()
            result[self._columns] = self._scaler.transform(X[self._columns])
            return result
        return self._scaler.transform(X)

    def inverse_transform(self, X: Any) -> Any:
        self._assert_fitted()
        return self._scaler.inverse_transform(X)

    def get_spec(self) -> TransformSpec:
        return TransformSpec(
            name="StandardScaler",
            type=TransformType.SCALING,
            columns=self._columns,
            parameters={"with_std": self._with_std},
            fitted_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Categorical Encoding ───────────────────────────────────────────────────────

class OrdinalEncoder(BaseFeatureTransformer):
    """Encode categorical columns to integer codes."""

    def __init__(self, columns: list[str] | None = None) -> None:
        super().__init__(columns)
        self._encoders: dict = {}

    def fit(self, X: Any) -> "OrdinalEncoder":
        from sklearn.preprocessing import LabelEncoder
        import pandas as pd
        cols = self._columns or X.select_dtypes(include=["object", "category"]).columns.tolist()
        self._columns = cols
        for col in cols:
            le = LabelEncoder()
            le.fit(X[col].astype(str))
            self._encoders[col] = le
        self._fitted = True
        return self

    def transform(self, X: Any) -> Any:
        self._assert_fitted()
        import pandas as pd
        result = X.copy()
        for col, le in self._encoders.items():
            result[col] = le.transform(X[col].astype(str))
        return result

    def inverse_transform(self, X: Any) -> Any:
        self._assert_fitted()
        result = X.copy()
        for col, le in self._encoders.items():
            result[col] = le.inverse_transform(X[col].astype(int))
        return result

    def get_spec(self) -> TransformSpec:
        return TransformSpec(
            name="OrdinalEncoder",
            type=TransformType.ENCODING,
            columns=self._columns,
            parameters={"classes": {col: list(le.classes_) for col, le in self._encoders.items()}},
            fitted_at=datetime.now(timezone.utc).isoformat(),
        )


class OneHotEncoder(BaseFeatureTransformer):
    """One-hot encode categorical columns."""

    def __init__(self, columns: list[str] | None = None, drop: str | None = "first") -> None:
        super().__init__(columns)
        self._drop = drop
        self._encoder = None
        self._output_columns: list[str] = []

    def fit(self, X: Any) -> "OneHotEncoder":
        from sklearn.preprocessing import OneHotEncoder as SKOneHotEncoder
        import pandas as pd
        cols = self._columns or X.select_dtypes(include=["object", "category"]).columns.tolist()
        self._columns = cols
        self._encoder = SKOneHotEncoder(drop=self._drop, sparse_output=False, handle_unknown="ignore")
        self._encoder.fit(X[cols])
        self._output_columns = list(self._encoder.get_feature_names_out(cols))
        self._fitted = True
        return self

    def transform(self, X: Any) -> Any:
        self._assert_fitted()
        import pandas as pd
        encoded = self._encoder.transform(X[self._columns])
        encoded_df = pd.DataFrame(encoded, columns=self._output_columns, index=X.index)
        return pd.concat([X.drop(columns=self._columns), encoded_df], axis=1)

    def inverse_transform(self, X: Any) -> Any:
        # One-hot inverse is lossy if drop="first"
        raise NotImplementedError("Inverse transform not supported for OneHotEncoder with drop='first'")

    def get_spec(self) -> TransformSpec:
        return TransformSpec(
            name="OneHotEncoder",
            type=TransformType.ENCODING,
            columns=self._columns,
            parameters={"drop": self._drop, "output_columns": self._output_columns},
            fitted_at=datetime.now(timezone.utc).isoformat(),
        )


# ── Feature Selection ──────────────────────────────────────────────────────────

class VarianceThresholdSelector(BaseFeatureTransformer):
    """Remove features with variance below a threshold."""

    def __init__(self, threshold: float = 0.0) -> None:
        super().__init__()
        self._threshold = threshold
        self._selector = None
        self._selected_columns: list[str] = []

    def fit(self, X: Any) -> "VarianceThresholdSelector":
        from sklearn.feature_selection import VarianceThreshold
        import pandas as pd
        self._selector = VarianceThreshold(threshold=self._threshold)
        self._selector.fit(X)
        if isinstance(X, pd.DataFrame):
            mask = self._selector.get_support()
            self._selected_columns = [c for c, keep in zip(X.columns, mask) if keep]
        self._fitted = True
        return self

    def transform(self, X: Any) -> Any:
        self._assert_fitted()
        import pandas as pd
        if isinstance(X, pd.DataFrame) and self._selected_columns:
            return X[self._selected_columns]
        return self._selector.transform(X)

    def inverse_transform(self, X: Any) -> Any:
        raise NotImplementedError("Feature selection is not invertible")

    def get_spec(self) -> TransformSpec:
        return TransformSpec(
            name="VarianceThresholdSelector",
            type=TransformType.SELECTION,
            columns=self._selected_columns,
            parameters={"threshold": self._threshold},
            fitted_at=datetime.now(timezone.utc).isoformat(),
        )


class MissingValueImputer(BaseFeatureTransformer):
    """Fill missing values using configurable strategy."""

    STRATEGIES = ("mean", "median", "most_frequent", "constant")

    def __init__(
        self,
        columns: list[str] | None = None,
        strategy: str = "median",
        fill_value: Any = 0,
    ) -> None:
        super().__init__(columns)
        self._strategy = strategy
        self._fill_value = fill_value
        self._imputer = None

    def fit(self, X: Any) -> "MissingValueImputer":
        from sklearn.impute import SimpleImputer
        import pandas as pd
        cols = self._columns or list(X.columns) if hasattr(X, "columns") else None
        self._columns = cols
        kwargs = {"strategy": self._strategy}
        if self._strategy == "constant":
            kwargs["fill_value"] = self._fill_value
        self._imputer = SimpleImputer(**kwargs)
        data = X[cols] if cols and isinstance(X, pd.DataFrame) else X
        self._imputer.fit(data)
        self._fitted = True
        return self

    def transform(self, X: Any) -> Any:
        self._assert_fitted()
        import pandas as pd
        if isinstance(X, pd.DataFrame) and self._columns:
            result = X.copy()
            result[self._columns] = self._imputer.transform(X[self._columns])
            return result
        return self._imputer.transform(X)

    def inverse_transform(self, X: Any) -> Any:
        raise NotImplementedError("Imputation is not invertible")

    def get_spec(self) -> TransformSpec:
        return TransformSpec(
            name="MissingValueImputer",
            type=TransformType.IMPUTATION,
            columns=self._columns or [],
            parameters={"strategy": self._strategy, "fill_value": self._fill_value},
            fitted_at=datetime.now(timezone.utc).isoformat(),
        )
