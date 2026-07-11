"""
ML Platform — Model Serving Layer
===================================
HTTP serving layer that exposes deployed models as REST endpoints.
Runs as a FastAPI app — independently deployable as a separate service.

Routing strategies:
  DirectRouter    → single model_id, no splitting
  ABTestRouter    → split traffic between two model versions (50/50 or weighted)
  CanaryRouter    → route small % of traffic to candidate model
  ShadowRouter    → mirror traffic to shadow model, discard its responses

The ModelServer holds a router + inference engine.
"""

from __future__ import annotations

import random
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ml_platform.inference.schemas import InferenceRequest, InferenceResult


# ── Router ABC ─────────────────────────────────────────────────────────────────

class BaseModelRouter(ABC):
    """Selects which model_id receives a given inference request."""

    @abstractmethod
    def route(self, request: InferenceRequest) -> str:
        """Return the model_id that should handle this request."""
        ...

    @abstractmethod
    def summary(self) -> dict[str, Any]: ...


# ── Concrete routers ───────────────────────────────────────────────────────────

class DirectRouter(BaseModelRouter):
    """Always routes to a single model."""

    def __init__(self, model_id: str) -> None:
        self._model_id = model_id

    def route(self, request: InferenceRequest) -> str:
        return self._model_id

    def summary(self) -> dict[str, Any]:
        return {"strategy": "direct", "model_id": self._model_id}


class ABTestRouter(BaseModelRouter):
    """
    Splits traffic between two models at a configurable ratio.
    model_a_weight + model_b_weight must sum to 1.0.
    """

    def __init__(
        self,
        model_a_id: str,
        model_b_id: str,
        model_a_weight: float = 0.5,
    ) -> None:
        assert 0 < model_a_weight < 1, "weight must be between 0 and 1"
        self._a  = model_a_id
        self._b  = model_b_id
        self._wa = model_a_weight

    def route(self, request: InferenceRequest) -> str:
        return self._a if random.random() < self._wa else self._b

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": "ab_test",
            "model_a": self._a, "weight_a": self._wa,
            "model_b": self._b, "weight_b": round(1 - self._wa, 4),
        }


class CanaryRouter(BaseModelRouter):
    """
    Routes a small fraction of traffic to a candidate model.
    The rest goes to the stable production model.
    """

    def __init__(
        self,
        production_id: str,
        canary_id: str,
        canary_ratio: float = 0.05,
    ) -> None:
        self._prod   = production_id
        self._canary = canary_id
        self._ratio  = canary_ratio

    def route(self, request: InferenceRequest) -> str:
        return self._canary if random.random() < self._ratio else self._prod

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": "canary",
            "production": self._prod,
            "canary": self._canary,
            "canary_ratio": self._ratio,
        }


class ShadowRouter(BaseModelRouter):
    """
    Sends every request to production AND mirrors it to a shadow model.
    The shadow response is logged but never returned to the caller.
    """

    def __init__(self, production_id: str, shadow_id: str) -> None:
        self._prod   = production_id
        self._shadow = shadow_id

    def route(self, request: InferenceRequest) -> str:
        return self._prod   # caller always gets production response

    @property
    def shadow_model_id(self) -> str:
        return self._shadow

    def summary(self) -> dict[str, Any]:
        return {
            "strategy": "shadow",
            "production": self._prod,
            "shadow": self._shadow,
        }


# ── Model server ───────────────────────────────────────────────────────────────

class ModelServer:
    """
    Thin orchestration layer between the HTTP endpoint and the inference engine.

    Responsibilities:
      - Route requests via the configured router
      - Fire-and-forget shadow inference when ShadowRouter is active
      - Collect timing and status for the monitoring layer
      - Expose health and info endpoints

    Used by the serving FastAPI app (ml_platform/serving/app.py — to be created).
    """

    def __init__(
        self,
        inference_engine: Any,           # RealTimeInferenceEngine
        router: BaseModelRouter,
        monitoring: Any = None,          # MonitoringStore
    ) -> None:
        self._engine    = inference_engine
        self._router    = router
        self._monitoring = monitoring

    async def handle(self, request: InferenceRequest) -> InferenceResult:
        """Primary request handler — called by the FastAPI route."""
        routed_id = self._router.route(request)
        routed_request = InferenceRequest(
            model_id=routed_id,
            inputs=request.inputs,
            request_id=request.request_id,
            feature_group=request.feature_group,
            timeout_ms=request.timeout_ms,
        )
        result = await self._engine.predict_async(routed_request)

        # Shadow inference (fire-and-forget)
        if isinstance(self._router, ShadowRouter):
            import asyncio
            shadow_request = InferenceRequest(
                model_id=self._router.shadow_model_id,
                inputs=request.inputs,
                request_id=f"{request.request_id}_shadow",
                feature_group=request.feature_group,
            )
            asyncio.create_task(self._shadow_predict(shadow_request))

        return result

    async def _shadow_predict(self, request: InferenceRequest) -> None:
        try:
            shadow_result = await self._engine.predict_async(request)
            if self._monitoring:
                self._monitoring.record_shadow_inference(shadow_result)
        except Exception:
            pass   # shadow failures must never affect the primary path

    def health(self) -> dict[str, Any]:
        return {
            "status": "healthy",
            "router": self._router.summary(),
            "cache_stats": getattr(self._engine, "_cache", None) and
                           self._engine._cache.stats(),
        }

    def info(self) -> dict[str, Any]:
        return {
            "router": self._router.summary(),
        }
