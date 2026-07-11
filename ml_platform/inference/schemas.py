"""
ML Platform — Inference Schemas
================================
Request / response contracts for all inference paths.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class InferenceStatus(str, Enum):
    SUCCESS  = "success"
    FAILED   = "failed"
    TIMEOUT  = "timeout"
    REJECTED = "rejected"   # rate-limited or circuit-opened


class InferenceMode(str, Enum):
    REALTIME  = "realtime"
    BATCH     = "batch"
    STREAM    = "stream"


@dataclass
class InferenceRequest:
    model_id:       str
    inputs:         Any                   # numpy array, dict, list, DataFrame
    request_id:     str                   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    model_version:  str                   = "latest"
    feature_group:  str | None            = None   # FeatureStore group to apply
    mode:           InferenceMode         = InferenceMode.REALTIME
    timeout_ms:     int                   = 5000
    metadata:       dict[str, Any]        = field(default_factory=dict)
    received_at:    str                   = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class InferenceResult:
    request_id:     str
    model_id:       str
    status:         InferenceStatus
    predictions:    list[Any]             = field(default_factory=list)
    probabilities:  list[list[float]]     = field(default_factory=list)  # for classifiers
    confidence:     float | None          = None
    latency_ms:     float                 = 0.0
    model_version:  str                   = ""
    error:          str                   = ""
    completed_at:   str                   = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    metadata:       dict[str, Any]        = field(default_factory=dict)


@dataclass
class BatchJobRequest:
    model_id:       str
    dataset_id:     str
    output_path:    str
    job_id:         str                   = field(default_factory=lambda: str(uuid.uuid4())[:8])
    feature_group:  str | None            = None
    batch_size:     int                   = 512
    priority:       int                   = 5       # 1 (highest) to 10 (lowest)
    tags:           dict[str, str]        = field(default_factory=dict)
    submitted_at:   str                   = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class BatchJobResult:
    job_id:         str
    model_id:       str
    dataset_id:     str
    output_path:    str
    status:         str
    rows_processed: int
    rows_failed:    int
    duration_s:     float
    completed_at:   str                   = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error:          str                   = ""
