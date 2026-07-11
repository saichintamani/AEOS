"""
AEOS ML Platform
================
Enterprise Machine Learning Platform for the AEOS AI Engineering Orchestration System.

Module map:
  datasets/        — Dataset loading, versioning, metadata, validation
  preprocessing/   — Data preprocessing utilities
  feature_store/   — Feature transforms, pipelines, drift detection
  experiments/     — Experiment tracking (local + MLflow-ready)
  models/          — BaseModel ABC, ModelConfig, ModelCatalog
  training/        — TrainingEngine, callbacks, device abstraction
  evaluation/      — Model evaluation, metrics
  inference/       — RealTime + Batch inference engines
  registry/        — Enterprise Model Registry with lifecycle management
  serving/         — ModelServer, routing strategies (A/B, Canary, Shadow)
  monitoring/      — Production monitoring, alerts, drift checking
  explainability/  — SHAP, LIME, feature importance
  pipelines/       — End-to-end training + inference pipelines
  utils/           — Serialization, hashing, device utilities
"""

__version__ = "0.1.0"

