"""
ML Platform — Feature Store: Feature Drift Detection
=====================================================
Detects statistical drift between a reference (training) distribution
and a production (serving) distribution.

Design principles:
- Stateless: drift detectors are called on-demand; they don't poll.
- Pluggable: each DriftTest is independently composable.
- Auditable: every DriftReport is serializable for monitoring dashboards.

Future integrations: Evidently AI, NannyML, WhyLogs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DriftSeverity(str, Enum):
    NONE     = "none"
    LOW      = "low"
    MEDIUM   = "medium"
    HIGH     = "high"
    CRITICAL = "critical"


@dataclass
class ColumnDriftResult:
    column:        str
    test_name:     str
    statistic:     float
    p_value:       float | None
    threshold:     float
    drift_detected: bool
    severity:      DriftSeverity


@dataclass
class DriftReport:
    """Full drift analysis output. Written to monitoring store after each check."""
    report_id:      str
    model_id:       str
    reference_id:   str          # dataset_id of training distribution
    current_id:     str          # dataset_id or batch_id of serving distribution
    generated_at:   str          = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    column_results: list[ColumnDriftResult] = field(default_factory=list)
    overall_drift:  DriftSeverity           = DriftSeverity.NONE
    summary:        dict[str, Any]          = field(default_factory=dict)

    @property
    def drift_detected(self) -> bool:
        return self.overall_drift != DriftSeverity.NONE

    @property
    def drifted_columns(self) -> list[str]:
        return [r.column for r in self.column_results if r.drift_detected]


# ── Drift test ABC ─────────────────────────────────────────────────────────────

class BaseDriftTest(ABC):
    """Single statistical test for one feature column."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def test(
        self,
        reference: Any,   # 1-D array-like: training distribution
        current: Any,     # 1-D array-like: serving distribution
        threshold: float,
    ) -> ColumnDriftResult: ...


# ── Concrete tests ─────────────────────────────────────────────────────────────

class KolmogorovSmirnovTest(BaseDriftTest):
    """
    Two-sample K-S test for continuous features.
    Detects changes in the CDF shape.
    Default p-value threshold: 0.05.
    """

    name = "kolmogorov_smirnov"

    def test(self, reference: Any, current: Any, threshold: float = 0.05) -> ColumnDriftResult:
        from scipy import stats
        import numpy as np
        stat, p_value = stats.ks_2samp(
            np.array(reference).astype(float),
            np.array(current).astype(float),
        )
        drift = p_value < threshold
        severity = self._severity(stat, drift)
        return ColumnDriftResult(
            column="",          # caller sets this
            test_name=self.name,
            statistic=round(float(stat), 4),
            p_value=round(float(p_value), 6),
            threshold=threshold,
            drift_detected=drift,
            severity=severity,
        )

    def _severity(self, stat: float, drift: bool) -> DriftSeverity:
        if not drift:
            return DriftSeverity.NONE
        if stat < 0.1:
            return DriftSeverity.LOW
        if stat < 0.2:
            return DriftSeverity.MEDIUM
        if stat < 0.4:
            return DriftSeverity.HIGH
        return DriftSeverity.CRITICAL


class ChiSquaredTest(BaseDriftTest):
    """
    Chi-squared test for categorical features.
    Compares observed vs expected frequency distributions.
    """

    name = "chi_squared"

    def test(self, reference: Any, current: Any, threshold: float = 0.05) -> ColumnDriftResult:
        from scipy import stats
        import numpy as np

        ref = np.array(reference)
        cur = np.array(current)
        categories = np.union1d(np.unique(ref), np.unique(cur))
        ref_counts = np.array([np.sum(ref == c) for c in categories], dtype=float)
        cur_counts = np.array([np.sum(cur == c) for c in categories], dtype=float)

        # Normalize reference to expected counts for current size
        expected = ref_counts / ref_counts.sum() * cur_counts.sum()
        # Avoid division by zero
        expected = np.where(expected == 0, 1e-10, expected)
        stat, p_value = stats.chisquare(cur_counts, f_exp=expected)

        drift = p_value < threshold
        return ColumnDriftResult(
            column="",
            test_name=self.name,
            statistic=round(float(stat), 4),
            p_value=round(float(p_value), 6),
            threshold=threshold,
            drift_detected=drift,
            severity=DriftSeverity.HIGH if drift else DriftSeverity.NONE,
        )


class PopulationStabilityIndex(BaseDriftTest):
    """
    PSI: measures shift in score/probability distributions.
    Commonly used for model score monitoring.
    PSI < 0.1 = no drift | 0.1–0.2 = moderate | > 0.2 = significant.
    """

    name = "psi"
    N_BINS = 10

    def test(self, reference: Any, current: Any, threshold: float = 0.2) -> ColumnDriftResult:
        import numpy as np

        ref = np.array(reference, dtype=float)
        cur = np.array(current, dtype=float)
        bins = np.percentile(ref, np.linspace(0, 100, self.N_BINS + 1))
        bins = np.unique(bins)

        ref_counts, _ = np.histogram(ref, bins=bins)
        cur_counts, _ = np.histogram(cur, bins=bins)

        ref_pct = ref_counts / ref_counts.sum()
        cur_pct = cur_counts / cur_counts.sum()

        # Avoid log(0)
        ref_pct = np.where(ref_pct == 0, 1e-4, ref_pct)
        cur_pct = np.where(cur_pct == 0, 1e-4, cur_pct)

        psi = float(np.sum((cur_pct - ref_pct) * np.log(cur_pct / ref_pct)))
        drift = psi > threshold

        if psi < 0.1:
            severity = DriftSeverity.NONE
        elif psi < 0.2:
            severity = DriftSeverity.LOW
        elif psi < 0.4:
            severity = DriftSeverity.MEDIUM
        else:
            severity = DriftSeverity.HIGH

        return ColumnDriftResult(
            column="",
            test_name=self.name,
            statistic=round(psi, 4),
            p_value=None,
            threshold=threshold,
            drift_detected=drift,
            severity=severity,
        )


# ── Feature drift detector ─────────────────────────────────────────────────────

class FeatureDriftDetector:
    """
    Runs configured drift tests across all feature columns.
    Produces a DriftReport for consumption by the monitoring module.

    Usage:
        detector = FeatureDriftDetector()
        report = detector.detect(
            reference_df=training_df,
            current_df=serving_batch_df,
            model_id="model_abc",
            reference_id="dataset_abc123",
            current_id="batch_20260628",
        )
    """

    def __init__(
        self,
        continuous_test: BaseDriftTest | None = None,
        categorical_test: BaseDriftTest | None = None,
        p_value_threshold: float = 0.05,
        psi_threshold: float = 0.2,
    ) -> None:
        self._continuous_test   = continuous_test   or KolmogorovSmirnovTest()
        self._categorical_test  = categorical_test  or ChiSquaredTest()
        self._p_threshold       = p_value_threshold
        self._psi_threshold     = psi_threshold

    def detect(
        self,
        reference_df: Any,
        current_df: Any,
        model_id: str,
        reference_id: str,
        current_id: str,
        columns: list[str] | None = None,
    ) -> DriftReport:
        import uuid
        import pandas as pd

        cols = columns or list(reference_df.columns)
        report = DriftReport(
            report_id=str(uuid.uuid4())[:8],
            model_id=model_id,
            reference_id=reference_id,
            current_id=current_id,
        )

        for col in cols:
            if col not in current_df.columns:
                continue

            ref_col = reference_df[col].dropna()
            cur_col = current_df[col].dropna()

            is_categorical = (
                isinstance(reference_df, pd.DataFrame)
                and reference_df[col].dtype == object
            )

            test = self._categorical_test if is_categorical else self._continuous_test
            result = test.test(ref_col, cur_col, self._p_threshold)
            result.column = col
            report.column_results.append(result)

        # Aggregate severity
        severities = [r.severity for r in report.column_results if r.drift_detected]
        if not severities:
            report.overall_drift = DriftSeverity.NONE
        elif DriftSeverity.CRITICAL in severities:
            report.overall_drift = DriftSeverity.CRITICAL
        elif DriftSeverity.HIGH in severities:
            report.overall_drift = DriftSeverity.HIGH
        elif DriftSeverity.MEDIUM in severities:
            report.overall_drift = DriftSeverity.MEDIUM
        else:
            report.overall_drift = DriftSeverity.LOW

        report.summary = {
            "total_columns":   len(cols),
            "drifted_columns": len(report.drifted_columns),
            "drift_rate":      round(len(report.drifted_columns) / max(len(cols), 1), 4),
        }

        return report
