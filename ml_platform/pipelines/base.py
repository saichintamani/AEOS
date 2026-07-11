"""
ML Platform — Pipelines: Base Abstractions
===========================================
Pipelines orchestrate multi-step ML workflows end-to-end.
Each pipeline step is independently testable.

Two primary pipeline types:
  TrainingPipeline   → data → features → train → evaluate → register
  InferencePipeline  → inputs → preprocess → infer → postprocess → return

Pipelines are serializable (for scheduling, retry, and audit).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class PipelineStatus(str, Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


@dataclass
class PipelineRun:
    pipeline_id:  str
    pipeline_name: str
    status:       PipelineStatus = PipelineStatus.PENDING
    started_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str = ""
    error:        str = ""
    outputs:      dict[str, Any] = field(default_factory=dict)
    metadata:     dict[str, Any] = field(default_factory=dict)


class BasePipeline(ABC):
    """
    A pipeline is an ordered sequence of steps that can be
    run atomically or step-by-step (for debugging).
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run(self, **kwargs) -> PipelineRun:
        """Execute the full pipeline."""
        ...

    @abstractmethod
    def validate_inputs(self, **kwargs) -> None:
        """Validate inputs before running. Raise ValueError on failure."""
        ...

    def dry_run(self, **kwargs) -> list[str]:
        """
        Return a list of step names that would execute,
        without actually running them.
        """
        return self._describe_steps()

    @abstractmethod
    def _describe_steps(self) -> list[str]: ...
