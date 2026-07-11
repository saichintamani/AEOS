"""
ML Platform — Model Schemas
============================
Pydantic schemas used across the ML Platform API surface.
Kept separate from base.py dataclasses so they can be used in
FastAPI routes, the registry, and the monitoring layer.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

try:
    from pydantic import BaseModel as PydanticBase, Field
except ImportError:
    # Fallback if pydantic not present — use dataclasses
    from dataclasses import dataclass as PydanticBase, field as Field   # type: ignore


class DeploymentStatus(str, Enum):
    UNDEPLOYED  = "undeployed"
    STAGING     = "staging"
    PRODUCTION  = "production"
    CANARY      = "canary"
    SHADOW      = "shadow"
    DEPRECATED  = "deprecated"
    RETIRED     = "retired"


class ModelHealthStatus(str, Enum):
    HEALTHY    = "healthy"
    DEGRADED   = "degraded"
    UNHEALTHY  = "unhealthy"
    UNKNOWN    = "unknown"
