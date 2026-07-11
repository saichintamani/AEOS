"""
ML Platform — Utilities: Serialization
=======================================
Consistent serialization helpers used across the platform.
Supports pickle, joblib, ONNX export, and JSON schema dumping.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any


def save_pickle(obj: Any, path: str) -> str:
    """Serialize object to pickle. Returns absolute path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(obj, f)
    return str(p.resolve())


def load_pickle(path: str) -> Any:
    with open(path, "rb") as f:
        return pickle.load(f)


def save_joblib(obj: Any, path: str) -> str:
    """Serialize sklearn-compatible objects with joblib (faster for large arrays)."""
    try:
        import joblib
    except ImportError:
        raise ImportError("Install joblib: pip install joblib")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(obj, str(p))
    return str(p.resolve())


def load_joblib(path: str) -> Any:
    import joblib
    return joblib.load(path)


def save_json(obj: Any, path: str, indent: int = 2) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=indent, default=str))
    return str(p.resolve())


def load_json(path: str) -> Any:
    return json.loads(Path(path).read_text())


def export_onnx(model: Any, path: str, input_shape: list[int], opset: int = 17) -> str:
    """
    Export a PyTorch or sklearn model to ONNX format.
    Requires: torch, skl2onnx, or onnxmltools depending on framework.
    """
    # TODO: implement framework-specific ONNX export
    raise NotImplementedError("ONNX export — implement per framework")
