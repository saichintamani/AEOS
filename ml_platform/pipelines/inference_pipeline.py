"""
ML Platform — Inference Pipeline
==================================
End-to-end inference workflow:
  1. Validate inputs
  2. Apply feature preprocessing (FeatureStore)
  3. Route to correct model version (ModelServer router)
  4. Run inference (InferenceEngine)
  5. Apply post-processing (if configured)
  6. Emit monitoring event
  7. Return InferenceResult

This is the production serving path.  Every request goes through this pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ml_platform.inference.schemas import InferenceRequest, InferenceResult, InferenceStatus
from ml_platform.pipelines.base import BasePipeline, PipelineRun, PipelineStatus


class InferencePipeline(BasePipeline):
    """
    Request-scoped inference pipeline.
    One instance per configured endpoint (production, canary, shadow, etc.).

    Usage:
        pipeline = InferencePipeline(
            inference_engine=engine,
            feature_store=feature_store,
            monitoring_store=monitoring_store,
        )
        result = await pipeline.predict(request)
    """

    name = "inference_pipeline"

    def __init__(
        self,
        inference_engine: Any,           # RealTimeInferenceEngine
        feature_store:    Any = None,    # FeatureStore
        monitoring_store: Any = None,    # MonitoringStore
        postprocessor:    Any = None,    # optional callable: result → result
    ) -> None:
        self._engine    = inference_engine
        self._features  = feature_store
        self._monitoring = monitoring_store
        self._post      = postprocessor

    async def predict(self, request: InferenceRequest) -> InferenceResult:
        """Primary async inference entrypoint."""
        # Step 1: validate
        try:
            self._validate_request(request)
        except ValueError as exc:
            return InferenceResult(
                request_id=request.request_id,
                model_id=request.model_id,
                status=InferenceStatus.REJECTED,
                error=str(exc),
            )

        # Step 2: feature preprocessing
        if self._features and request.feature_group:
            pipeline = self._features.load_pipeline(request.feature_group)
            request = InferenceRequest(
                model_id=request.model_id,
                inputs=pipeline.transform(request.inputs),
                request_id=request.request_id,
                feature_group=None,   # already applied
                timeout_ms=request.timeout_ms,
            )

        # Step 3 + 4: route + infer
        result = await self._engine.predict_async(request)

        # Step 5: post-process
        if self._post and result.status == InferenceStatus.SUCCESS:
            result = self._post(result)

        # Step 6: monitoring
        if self._monitoring:
            self._monitoring.record_inference(result)

        return result

    def run(self, **kwargs) -> PipelineRun:
        raise NotImplementedError("Use predict() for inference pipelines")

    def validate_inputs(self, **kwargs) -> None:
        request = kwargs.get("request")
        if request:
            self._validate_request(request)

    def _describe_steps(self) -> list[str]:
        return [
            "1. validate_request",
            "2. apply_feature_preprocessing",
            "3. route_to_model",
            "4. run_inference",
            "5. post_process",
            "6. emit_monitoring_event",
        ]

    def _validate_request(self, request: InferenceRequest) -> None:
        if not request.model_id:
            raise ValueError("model_id is required")
        if request.inputs is None:
            raise ValueError("inputs cannot be None")
