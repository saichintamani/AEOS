"""
ML Platform — Dataset Layer: Validation Pipeline
=================================================
Multi-stage validation pipeline that runs structural and
statistical checks before a dataset is accepted into the platform.

Each stage is independently composable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ml_platform.datasets.base import DatasetRecord, ValidationStatus


class ValidationSeverity(str, Enum):
    INFO    = "info"
    WARNING = "warning"
    ERROR   = "error"


@dataclass
class ValidationIssue:
    code:     str
    message:  str
    column:   str | None = None
    severity: ValidationSeverity = ValidationSeverity.ERROR


@dataclass
class ValidationReport:
    dataset_id: str
    status:     ValidationStatus
    issues:     list[ValidationIssue] = field(default_factory=list)
    stats:      dict[str, Any]        = field(default_factory=dict)

    @property
    def has_errors(self) -> bool:
        return any(i.severity == ValidationSeverity.ERROR for i in self.issues)

    @property
    def has_warnings(self) -> bool:
        return any(i.severity == ValidationSeverity.WARNING for i in self.issues)


# ── Stage ABC ──────────────────────────────────────────────────────────────────

class BaseValidationStage(ABC):
    """One validation check in the pipeline."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run(self, record: DatasetRecord, report: ValidationReport) -> None:
        """Mutate report in-place: append issues and stats."""
        ...


# ── Concrete stages ────────────────────────────────────────────────────────────

class SchemaValidationStage(BaseValidationStage):
    """Checks column presence, dtype consistency, and null ratios."""

    name = "schema_validation"
    NULL_THRESHOLD = 0.3   # flag columns with >30% nulls

    def run(self, record: DatasetRecord, report: ValidationReport) -> None:
        df = record.data
        if not hasattr(df, "columns"):
            return   # non-tabular dataset — skip

        # Null ratio checks
        null_ratios = df.isnull().mean()
        for col, ratio in null_ratios.items():
            if ratio > self.NULL_THRESHOLD:
                report.issues.append(ValidationIssue(
                    code="HIGH_NULL_RATIO",
                    message=f"Column '{col}' has {ratio:.1%} null values",
                    column=col,
                    severity=ValidationSeverity.WARNING,
                ))

        # Target column present
        target = record.metadata.target_column
        if target and target not in df.columns:
            report.issues.append(ValidationIssue(
                code="MISSING_TARGET",
                message=f"Target column '{target}' not found in dataset",
                severity=ValidationSeverity.ERROR,
            ))

        report.stats["null_ratios"] = null_ratios.to_dict()


class RowCountValidationStage(BaseValidationStage):
    """Ensures the dataset meets minimum row count requirements."""

    name = "row_count_validation"
    MIN_ROWS = 10

    def run(self, record: DatasetRecord, report: ValidationReport) -> None:
        row_count = record.metadata.row_count
        if row_count < self.MIN_ROWS:
            report.issues.append(ValidationIssue(
                code="INSUFFICIENT_ROWS",
                message=f"Dataset has only {row_count} rows (minimum: {self.MIN_ROWS})",
                severity=ValidationSeverity.ERROR,
            ))
        report.stats["row_count"] = row_count


class ClassBalanceValidationStage(BaseValidationStage):
    """
    For classification datasets: checks label distribution.
    Flags severe imbalance (dominant class > 95%).
    """

    name = "class_balance_validation"
    IMBALANCE_THRESHOLD = 0.95

    def run(self, record: DatasetRecord, report: ValidationReport) -> None:
        df = record.data
        target = record.metadata.target_column
        if target is None or not hasattr(df, "columns") or target not in df.columns:
            return

        counts = df[target].value_counts(normalize=True)
        dominant_ratio = float(counts.iloc[0])
        report.stats["class_distribution"] = counts.to_dict()

        if dominant_ratio > self.IMBALANCE_THRESHOLD:
            report.issues.append(ValidationIssue(
                code="SEVERE_CLASS_IMBALANCE",
                message=f"Dominant class covers {dominant_ratio:.1%} of samples",
                column=target,
                severity=ValidationSeverity.WARNING,
            ))


class DuplicateRowValidationStage(BaseValidationStage):
    """Detects and reports exact duplicate rows."""

    name = "duplicate_row_validation"
    DUPLICATE_THRESHOLD = 0.05   # warn if >5% duplicates

    def run(self, record: DatasetRecord, report: ValidationReport) -> None:
        df = record.data
        if not hasattr(df, "duplicated"):
            return

        dup_ratio = float(df.duplicated().mean())
        report.stats["duplicate_ratio"] = dup_ratio

        if dup_ratio > self.DUPLICATE_THRESHOLD:
            report.issues.append(ValidationIssue(
                code="HIGH_DUPLICATE_RATIO",
                message=f"{dup_ratio:.1%} of rows are exact duplicates",
                severity=ValidationSeverity.WARNING,
            ))


# ── Pipeline ───────────────────────────────────────────────────────────────────

class DatasetValidationPipeline:
    """
    Runs all registered validation stages in order.
    Returns a ValidationReport with aggregated issues and stats.

    Usage:
        pipeline = DatasetValidationPipeline()
        report = pipeline.validate(record)
        if report.has_errors:
            raise DatasetValidationError(report)
    """

    DEFAULT_STAGES: list[type[BaseValidationStage]] = [
        RowCountValidationStage,
        SchemaValidationStage,
        ClassBalanceValidationStage,
        DuplicateRowValidationStage,
    ]

    def __init__(self, stages: list[BaseValidationStage] | None = None) -> None:
        self._stages = stages or [cls() for cls in self.DEFAULT_STAGES]

    def add_stage(self, stage: BaseValidationStage) -> None:
        self._stages.append(stage)

    def validate(self, record: DatasetRecord) -> ValidationReport:
        report = ValidationReport(
            dataset_id=record.metadata.dataset_id,
            status=ValidationStatus.PENDING,
        )

        for stage in self._stages:
            try:
                stage.run(record, report)
            except Exception as exc:
                report.issues.append(ValidationIssue(
                    code="STAGE_ERROR",
                    message=f"Stage '{stage.name}' raised: {exc}",
                    severity=ValidationSeverity.ERROR,
                ))

        if report.has_errors:
            report.status = ValidationStatus.FAILED
        elif report.has_warnings:
            report.status = ValidationStatus.WARNINGS
        else:
            report.status = ValidationStatus.PASSED

        return report
