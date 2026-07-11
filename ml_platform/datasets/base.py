"""
ML Platform — Dataset Layer: Base Abstractions
===============================================
All dataset types derive from BaseDataset.
Implementations must produce a normalized DatasetRecord and support
versioning, validation, and metadata emission.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator


# ── Enums ──────────────────────────────────────────────────────────────────────

class DatasetFormat(str, Enum):
    CSV        = "csv"
    PARQUET    = "parquet"
    JSON       = "json"
    IMAGES     = "images"
    TEXT       = "text"
    STREAMING  = "streaming"
    INLINE     = "inline"


class DatasetSplit(str, Enum):
    TRAIN      = "train"
    VALIDATION = "validation"
    TEST       = "test"
    FULL       = "full"


class ValidationStatus(str, Enum):
    PENDING  = "pending"
    PASSED   = "passed"
    FAILED   = "failed"
    WARNINGS = "warnings"


# ── Schemas ────────────────────────────────────────────────────────────────────

@dataclass
class DatasetMetadata:
    """Immutable record describing a dataset version."""
    dataset_id:       str                        # sha256[:16] content hash
    name:             str
    version:          str                        # semantic: "1.0.0"
    format:           DatasetFormat
    source:           str                        # file path, URI, or "inline"
    row_count:        int
    feature_columns:  list[str]
    target_column:    str | None
    schema:           dict[str, str]             # col_name → dtype string
    split:            DatasetSplit
    tags:             dict[str, str]             = field(default_factory=dict)
    created_at:       str                        = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    validation_status: ValidationStatus          = ValidationStatus.PENDING
    lineage:          list[str]                  = field(default_factory=list)
    # lineage: list of parent dataset_ids used to derive this one


@dataclass
class DatasetRecord:
    """Runtime container returned by all loaders."""
    metadata: DatasetMetadata
    data: Any                   # pandas DataFrame, list[Path] for images, or Iterator


# ── Abstract base ──────────────────────────────────────────────────────────────

class BaseDataset(ABC):
    """
    Every dataset loader must implement this interface.

    Lifecycle:
        loader = SomeDatasetLoader(config)
        record = loader.load(source, **kwargs)
        loader.validate(record)
        loader.save_metadata(record.metadata)
    """

    @abstractmethod
    def load(self, source: str, **kwargs) -> DatasetRecord:
        """Load data from source and return a DatasetRecord."""
        ...

    @abstractmethod
    def validate(self, record: DatasetRecord) -> ValidationStatus:
        """
        Run structural and statistical validation.
        Must not mutate the record — only read and return status.
        """
        ...

    @abstractmethod
    def save_metadata(self, metadata: DatasetMetadata) -> None:
        """Persist metadata to the dataset registry store."""
        ...

    # ── Shared helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def compute_hash(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()[:16]

    @staticmethod
    def make_version(major: int = 1, minor: int = 0, patch: int = 0) -> str:
        return f"{major}.{minor}.{patch}"


class BaseStreamingDataset(ABC):
    """
    Interface for datasets too large to fit in memory.
    Yields batches rather than loading all at once.
    """

    @abstractmethod
    def stream(self, source: str, batch_size: int = 1024, **kwargs) -> Iterator[Any]:
        """Yield batches of data from the source."""
        ...

    @abstractmethod
    def estimate_size(self, source: str) -> int:
        """Return estimated total row count without full load."""
        ...
