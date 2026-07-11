"""
ML Platform — Model Abstraction Layer: BaseModel
=================================================
Every model implementation (sklearn, PyTorch, XGBoost, HuggingFace, etc.)
must implement this interface.  The platform's training engine, registry,
and inference engine operate exclusively on BaseModel — never on concrete
framework objects.

Lifecycle contract:
    model.initialize(config)
    model.train(dataset, config)
    metrics = model.evaluate(dataset)
    predictions = model.predict(inputs)
    path = model.save(directory)
    model.load(path)
    artifact = model.export(format)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ── Enums ──────────────────────────────────────────────────────────────────────

class ModelFramework(str, Enum):
    SKLEARN      = "sklearn"
    PYTORCH      = "pytorch"
    TENSORFLOW   = "tensorflow"
    XGBOOST      = "xgboost"
    LIGHTGBM     = "lightgbm"
    HUGGINGFACE  = "huggingface"
    CUSTOM       = "custom"


class ModelTask(str, Enum):
    CLASSIFICATION   = "classification"
    REGRESSION       = "regression"
    CLUSTERING       = "clustering"
    OBJECT_DETECTION = "object_detection"
    SEGMENTATION     = "segmentation"
    NLP_GENERATION   = "nlp_generation"
    NLP_EMBEDDING    = "nlp_embedding"
    REINFORCEMENT    = "reinforcement"


class ModelState(str, Enum):
    CREATED     = "created"
    INITIALIZED = "initialized"
    TRAINED     = "trained"
    EVALUATED   = "evaluated"
    SAVED       = "saved"
    LOADED      = "loaded"


class ExportFormat(str, Enum):
    PICKLE   = "pickle"
    ONNX     = "onnx"
    TORCHSCRIPT = "torchscript"
    SAVEDMODEL  = "savedmodel"    # TensorFlow SavedModel
    JOBLIB   = "joblib"
    MLFLOW   = "mlflow"


# ── Configuration schemas ──────────────────────────────────────────────────────

@dataclass
class ModelConfig:
    """
    Fully declarative model configuration.
    All training decisions are captured here — no magic defaults buried
    inside model classes.
    """
    name:            str
    framework:       ModelFramework
    task:            ModelTask
    architecture:    str                          # e.g. "ResNet50", "RandomForest", "BERT"
    hyperparameters: dict[str, Any]               = field(default_factory=dict)
    input_schema:    dict[str, str]               = field(default_factory=dict)  # feature → dtype
    output_schema:   dict[str, str]               = field(default_factory=dict)  # output → dtype
    tags:            dict[str, str]               = field(default_factory=dict)
    owner:           str                          = "system"
    description:     str                          = ""


@dataclass
class ModelMetadata:
    """
    Populated by the training engine after a train() call.
    Written into the model registry alongside the artifact.
    """
    model_id:       str
    name:           str
    version:        str
    framework:      ModelFramework
    task:           ModelTask
    architecture:   str
    dataset_id:     str
    metrics:        dict[str, float]
    hyperparameters: dict[str, Any]
    feature_names:  list[str]
    input_shape:    list[int]                     # [-1, num_features] for tabular
    output_shape:   list[int]
    trained_at:     str
    owner:          str
    tags:           dict[str, str]               = field(default_factory=dict)
    experiment_id:  str                          = ""
    run_id:         str                          = ""
    artifact_path:  str                          = ""
    description:    str                          = ""


# ── Abstract interface ─────────────────────────────────────────────────────────

class BaseModel(ABC):
    """
    Universal model interface for the AEOS ML Platform.

    Implementation map:
        ClassicalModel     → sklearn / XGBoost / LightGBM
        DeepLearningModel  → PyTorch nn.Module wrapper
        TransformerModel   → HuggingFace PreTrainedModel wrapper
        VisionModel        → torchvision / timm wrapper
        RLModel            → stable-baselines3 wrapper

    All concrete implementations must call super().__init__() and maintain
    self._state so the platform can assert lifecycle ordering.
    """

    def __init__(self) -> None:
        self._state: ModelState = ModelState.CREATED
        self._config: ModelConfig | None = None
        self._metadata: ModelMetadata | None = None

    # ── Lifecycle methods (must implement) ─────────────────────────────────────

    @abstractmethod
    def initialize(self, config: ModelConfig) -> None:
        """
        Build internal model structure from config.
        Must set self._state = ModelState.INITIALIZED on success.
        Must NOT load weights — that is load()'s job.
        """
        ...

    @abstractmethod
    def train(self, dataset: Any, config: Any) -> ModelMetadata:
        """
        Execute one full training run.
        Must set self._state = ModelState.TRAINED on success.
        Must return a populated ModelMetadata — the training engine
        writes this to the experiment tracker and registry.
        """
        ...

    @abstractmethod
    def evaluate(self, dataset: Any) -> dict[str, float]:
        """
        Compute evaluation metrics on the provided dataset split.
        Must NOT modify model weights.
        Returns a flat dict: {"accuracy": 0.92, "f1": 0.89, ...}
        """
        ...

    @abstractmethod
    def predict(self, inputs: Any) -> Any:
        """
        Run inference on a single input or batch.
        inputs: numpy array, DataFrame, PIL image, or tokenized text —
        depends on model type. See concrete implementations.
        """
        ...

    @abstractmethod
    def save(self, directory: str | Path) -> str:
        """
        Persist model weights + architecture to directory.
        Must set self._state = ModelState.SAVED on success.
        Returns the absolute path of the saved artifact.
        """
        ...

    @abstractmethod
    def load(self, path: str | Path) -> None:
        """
        Restore model from artifact at path.
        Must set self._state = ModelState.LOADED on success.
        """
        ...

    @abstractmethod
    def export(self, format: ExportFormat, output_path: str | Path) -> str:
        """
        Export model to an interoperable format (ONNX, TorchScript, etc.).
        Returns the absolute path of the exported artifact.
        """
        ...

    # ── Optional hooks (override as needed) ───────────────────────────────────

    def on_epoch_end(self, epoch: int, metrics: dict[str, float]) -> None:
        """Called by the training engine at the end of each epoch."""

    def on_training_complete(self, metadata: ModelMetadata) -> None:
        """Called by the training engine after all epochs finish."""

    # ── Introspection ──────────────────────────────────────────────────────────

    @property
    def state(self) -> ModelState:
        return self._state

    @property
    def config(self) -> ModelConfig | None:
        return self._config

    @property
    def metadata(self) -> ModelMetadata | None:
        return self._metadata

    def summary(self) -> dict[str, Any]:
        """Return a human-readable summary dict for logging."""
        return {
            "state":        self._state.value,
            "config":       self._config.name if self._config else None,
            "framework":    self._config.framework.value if self._config else None,
            "task":         self._config.task.value if self._config else None,
        }
