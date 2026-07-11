"""
ML Platform — Inference Engine
================================
Abstracts batch, real-time, and (future) streaming inference
behind a single InferenceEngine interface.

Design principles:
  - The engine loads models from the registry on demand (lazy loading)
  - Model instances are cached in memory with configurable TTL
  - All inference paths are async-ready
  - GPU inference is transparent — the engine moves tensors to device

Inference paths:
  RealTimeInferenceEngine  → single sample or small batch, low latency
  BatchInferenceEngine     → large batch, high throughput, async
  StreamInferenceEngine    → (future) Kafka / Kinesis stream consumption
"""

from __future__ import annotations

import asyncio
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ml_platform.inference.schemas import InferenceRequest, InferenceResult, InferenceStatus


# ── Base inference engine ──────────────────────────────────────────────────────

class BaseInferenceEngine(ABC):

    @abstractmethod
    def predict(self, request: InferenceRequest) -> InferenceResult:
        """Synchronous single-request inference."""
        ...

    @abstractmethod
    async def predict_async(self, request: InferenceRequest) -> InferenceResult:
        """Async single-request inference for use in FastAPI routes."""
        ...

    @abstractmethod
    def predict_batch(self, requests: list[InferenceRequest]) -> list[InferenceResult]:
        """Synchronous batch inference."""
        ...


# ── Model cache ────────────────────────────────────────────────────────────────

@dataclass
class CachedModel:
    model_id:    str
    model:       Any
    loaded_at:   float = field(default_factory=time.monotonic)
    call_count:  int   = 0
    last_used:   float = field(default_factory=time.monotonic)


class ModelCache:
    """
    LRU in-memory cache of loaded model objects.
    Prevents reloading from disk on every request.
    """

    def __init__(self, max_size: int = 10) -> None:
        self._cache: dict[str, CachedModel] = {}
        self._max_size = max_size

    def get(self, model_id: str) -> Any | None:
        entry = self._cache.get(model_id)
        if entry is None:
            return None
        entry.last_used = time.monotonic()
        entry.call_count += 1
        return entry.model

    def put(self, model_id: str, model: Any) -> None:
        if len(self._cache) >= self._max_size:
            # Evict least recently used
            lru_key = min(self._cache, key=lambda k: self._cache[k].last_used)
            del self._cache[lru_key]
        self._cache[model_id] = CachedModel(model_id=model_id, model=model)

    def evict(self, model_id: str) -> None:
        self._cache.pop(model_id, None)

    def clear(self) -> None:
        self._cache.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "cached_models": list(self._cache.keys()),
            "size": len(self._cache),
            "call_counts": {k: v.call_count for k, v in self._cache.items()},
        }


# ── Real-time inference engine ─────────────────────────────────────────────────

class RealTimeInferenceEngine(BaseInferenceEngine):
    """
    Low-latency inference for online serving.
    Optimised for p99 latency over throughput.

    Integrates with:
      - ModelRegistry: loads model by model_id
      - ModelCache: avoids repeated disk I/O
      - FeatureStore: applies registered preprocessing pipeline
      - MonitoringStore: emits prediction events for drift detection
    """

    def __init__(
        self,
        registry: Any,              # ModelRegistry
        feature_store: Any = None,  # FeatureStore (optional)
        monitoring: Any   = None,   # MonitoringStore (optional)
        cache_size: int   = 10,
    ) -> None:
        self._registry  = registry
        self._features  = feature_store
        self._monitoring = monitoring
        self._cache     = ModelCache(max_size=cache_size)

    def predict(self, request: InferenceRequest) -> InferenceResult:
        start = time.monotonic()
        try:
            model = self._get_model(request.model_id)
            inputs = self._preprocess(request)
            raw_output = model.predict(inputs)
            result = self._build_result(request, raw_output, start, InferenceStatus.SUCCESS)
        except Exception as exc:
            result = InferenceResult(
                request_id=request.request_id,
                model_id=request.model_id,
                status=InferenceStatus.FAILED,
                error=str(exc),
                latency_ms=round((time.monotonic() - start) * 1000, 2),
            )
        if self._monitoring:
            self._monitoring.record_inference(result)
        return result

    async def predict_async(self, request: InferenceRequest) -> InferenceResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.predict, request)

    def predict_batch(self, requests: list[InferenceRequest]) -> list[InferenceResult]:
        return [self.predict(r) for r in requests]

    # ── Internals ──────────────────────────────────────────────────────────────

    def _get_model(self, model_id: str) -> Any:
        cached = self._cache.get(model_id)
        if cached:
            return cached
        record = self._registry.get(model_id)
        if record is None:
            raise KeyError(f"Model '{model_id}' not found in registry")
        # TODO: instantiate correct BaseModel subclass from catalog, call load()
        # model = get_model_class(record.architecture)()
        # model.load(record.artifact_path)
        # self._cache.put(model_id, model)
        # return model
        raise NotImplementedError("Model loading from registry — implement with catalog")

    def _preprocess(self, request: InferenceRequest) -> Any:
        if self._features and request.feature_group:
            pipeline = self._features.load_pipeline(request.feature_group)
            return pipeline.transform(request.inputs)
        return request.inputs

    def _build_result(
        self,
        request: InferenceRequest,
        raw_output: Any,
        start: float,
        status: InferenceStatus,
    ) -> InferenceResult:
        return InferenceResult(
            request_id=request.request_id,
            model_id=request.model_id,
            status=status,
            predictions=raw_output if isinstance(raw_output, list) else [raw_output],
            latency_ms=round((time.monotonic() - start) * 1000, 2),
        )


# ── Batch inference engine ─────────────────────────────────────────────────────

class BatchInferenceEngine(BaseInferenceEngine):
    """
    High-throughput inference for offline scoring.
    Designed for large datasets — reads from DatasetRecord, writes results to sink.

    Usage:
        engine = BatchInferenceEngine(registry=registry)
        results = engine.run_batch_job(
            model_id="abc123",
            dataset=dataset_record,
            output_path="data/predictions/batch_001.parquet",
        )
    """

    def __init__(self, registry: Any, batch_size: int = 512) -> None:
        self._registry  = registry
        self._batch_size = batch_size

    def predict(self, request: InferenceRequest) -> InferenceResult:
        return RealTimeInferenceEngine(self._registry).predict(request)

    async def predict_async(self, request: InferenceRequest) -> InferenceResult:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.predict, request)

    def predict_batch(self, requests: list[InferenceRequest]) -> list[InferenceResult]:
        return [self.predict(r) for r in requests]

    def run_batch_job(
        self,
        model_id: str,
        dataset: Any,              # DatasetRecord
        output_path: str,
        feature_group: str | None = None,
    ) -> dict[str, Any]:
        """
        Score an entire dataset and write predictions to output_path.
        Returns a job summary dict.
        """
        # TODO:
        # 1. Load model from registry
        # 2. Load and iterate dataset in batches (use StreamingDatasetLoader)
        # 3. Apply feature_store preprocessing if feature_group given
        # 4. Call model.predict() on each batch
        # 5. Write predictions to output_path (parquet)
        # 6. Return summary: {rows_processed, output_path, duration_s, model_id}
        raise NotImplementedError("Batch job execution — implement with StreamingDatasetLoader")
