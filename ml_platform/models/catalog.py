"""
ML Platform — Model Catalog
============================
Maps model architecture names to concrete BaseModel implementations.
The training engine looks up models here — never imports them directly.

Adding a new model type requires only one line in this file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ml_platform.models.base import BaseModel, ModelConfig


# ── Skeleton implementations ───────────────────────────────────────────────────
# Each lives in its own module; imported lazily to avoid loading heavy
# frameworks (torch, tensorflow, xgboost) unless that model is actually used.

class ClassicalMLModel:
    """
    Wraps any scikit-learn estimator.
    Supported architectures: RandomForest, GradientBoosting, LogisticRegression,
    Ridge, Lasso, SVC, XGBoost, LightGBM.
    """
    # TODO: implement BaseModel — see models/implementations/classical.py


class PyTorchModel:
    """
    Wraps a torch.nn.Module.
    Training engine drives the epoch loop; model defines forward().
    Supported: custom architectures, timm models, torchvision models.
    """
    # TODO: implement BaseModel — see models/implementations/pytorch.py


class TransformerModel:
    """
    Wraps a HuggingFace PreTrainedModel.
    Handles tokenization, attention masks, and pooling strategies.
    Supported tasks: text-classification, token-classification, seq2seq, causal-LM.
    """
    # TODO: implement BaseModel — see models/implementations/transformer.py


class CNNModel:
    """
    Convolutional architecture for computer vision tasks.
    Backed by torchvision / timm.  Supports pretrained backbones.
    Supported architectures: ResNet, EfficientNet, ViT, ConvNeXt.
    """
    # TODO: implement BaseModel — see models/implementations/cnn.py


class RNNModel:
    """
    Recurrent architecture for sequential data.
    Supported: LSTM, GRU, Bidirectional variants.
    """
    # TODO: implement BaseModel — see models/implementations/rnn.py


# ── Registry ───────────────────────────────────────────────────────────────────

_CATALOG: dict[str, type] = {
    # Classical ML
    "random_forest":         ClassicalMLModel,
    "gradient_boosting":     ClassicalMLModel,
    "logistic_regression":   ClassicalMLModel,
    "linear_regression":     ClassicalMLModel,
    "ridge":                 ClassicalMLModel,
    "lasso":                 ClassicalMLModel,
    "svc":                   ClassicalMLModel,
    "xgboost":               ClassicalMLModel,
    "lightgbm":              ClassicalMLModel,
    # Deep learning
    "pytorch_custom":        PyTorchModel,
    "resnet50":              CNNModel,
    "resnet18":              CNNModel,
    "efficientnet_b0":       CNNModel,
    "efficientnet_b4":       CNNModel,
    "vit_base":              CNNModel,
    "lstm":                  RNNModel,
    "gru":                   RNNModel,
    # Transformers / NLP
    "bert_base":             TransformerModel,
    "roberta_base":          TransformerModel,
    "distilbert":            TransformerModel,
    "gpt2":                  TransformerModel,
    "t5_small":              TransformerModel,
}


def get_model_class(architecture: str) -> type:
    """Return the model class for the given architecture name."""
    cls = _CATALOG.get(architecture.lower())
    if cls is None:
        raise ValueError(
            f"Architecture '{architecture}' not registered. "
            f"Available: {sorted(_CATALOG.keys())}"
        )
    return cls


def list_architectures() -> list[str]:
    return sorted(_CATALOG.keys())


def register_model(architecture: str, cls: type) -> None:
    """Register a custom model class at runtime."""
    _CATALOG[architecture.lower()] = cls
