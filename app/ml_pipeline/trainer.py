"""
AEOS ML Pipeline — Model Trainer
Trains scikit-learn models with preprocessing and train/test split.
Supports 5 algorithms; extensible by adding to SUPPORTED_ALGORITHMS.
"""

from __future__ import annotations
from app.core.logger import get_logger

log = get_logger(__name__)

SUPPORTED_ALGORITHMS = {
    "logistic_regression":  "sklearn.linear_model.LogisticRegression",
    "random_forest":        "sklearn.ensemble.RandomForestClassifier",
    "gradient_boosting":    "sklearn.ensemble.GradientBoostingClassifier",
    "linear_regression":    "sklearn.linear_model.LinearRegression",
    "ridge":                "sklearn.linear_model.Ridge",
}


class ModelTrainer:

    def train(
        self,
        df,
        target_col: str,
        algorithm: str = "random_forest",
        test_size: float = 0.2,
        hyperparams: dict | None = None,
    ) -> dict:
        import numpy as np
        from sklearn.model_selection import train_test_split

        if algorithm not in SUPPORTED_ALGORITHMS:
            raise ValueError(
                f"Algorithm '{algorithm}' not supported. "
                f"Choose from: {list(SUPPORTED_ALGORITHMS.keys())}"
            )

        log.info(
            "Training model",
            extra={"ctx_algorithm": algorithm, "ctx_rows": len(df)},
        )

        X, y, feature_names = self._preprocess(df, target_col)
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=test_size, random_state=42
        )

        model = self._build_model(algorithm, hyperparams or {})
        model.fit(X_train, y_train)

        log.info("Model training complete", extra={"ctx_algorithm": algorithm})
        return {
            "model": model,
            "X_test": X_test,
            "y_test": y_test,
            "feature_names": feature_names,
            "train_meta": {
                "algorithm": algorithm,
                "train_rows": len(X_train),
                "test_rows": len(X_test),
                "features": feature_names,
                "hyperparams": hyperparams or {},
            },
        }

    def _build_model(self, algorithm: str, hyperparams: dict):
        import importlib
        module_path, class_name = SUPPORTED_ALGORITHMS[algorithm].rsplit(".", 1)
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        return cls(**hyperparams)

    def _preprocess(self, df, target_col: str) -> tuple:
        import numpy as np
        from sklearn.preprocessing import LabelEncoder

        feature_cols = [c for c in df.columns if c != target_col]
        X_df = df[feature_cols].copy()
        y_series = df[target_col].copy()

        # Encode categorical columns
        for col in X_df.select_dtypes(include=["object", "category"]).columns:
            le = LabelEncoder()
            X_df[col] = le.fit_transform(X_df[col].astype(str))

        # Fill numeric nulls with column median
        X_df = X_df.fillna(X_df.median(numeric_only=True))
        X_df = X_df.fillna(0)

        # Encode target if categorical
        if y_series.dtype == object or str(y_series.dtype) == "category":
            le = LabelEncoder()
            y = le.fit_transform(y_series.astype(str))
        else:
            y = y_series.to_numpy()

        return X_df.to_numpy(), y, feature_cols
