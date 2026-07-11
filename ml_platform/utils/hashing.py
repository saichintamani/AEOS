"""
ML Platform — Utilities: Content Hashing
=========================================
Deterministic content hashing for datasets, configs, and model artifacts.
Used for dataset versioning, cache keys, and deduplication.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def hash_bytes(data: bytes, length: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[:length]


def hash_file(path: str, length: int = 16) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


def hash_dict(d: dict[str, Any], length: int = 16) -> str:
    raw = json.dumps(d, sort_keys=True, default=str).encode()
    return hash_bytes(raw, length)


def hash_dataframe(df: Any, length: int = 16) -> str:
    """Content hash of a pandas DataFrame."""
    raw = df.to_json(orient="records").encode()
    return hash_bytes(raw, length)
