"""
AEOS RAG — Security helpers

Centralises the input-hardening primitives used across the RAG surface:
namespace validation, filesystem path confinement, upload validation, filename
sanitisation, and a lightweight in-process rate limiter.

These are deliberately dependency-free and side-effect-free (except the rate
limiter's internal state) so they can be unit-tested in isolation and reused by
both the pipeline and the HTTP layer.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

# A namespace becomes part of an on-disk directory name and a store collection
# name, so it must be a strict, traversal-proof token.
NAMESPACE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Formats we are willing to parse from an untrusted upload.
ALLOWED_UPLOAD_EXTENSIONS = {".txt", ".md", ".markdown", ".pdf", ".html", ".htm", ".json"}

MAX_FILENAME_LENGTH = 128


class SecurityError(ValueError):
    """Raised when an input fails a security validation check."""


def validate_namespace(namespace: str) -> str:
    """
    Return the namespace unchanged if it is a safe token, else raise.
    Guards both the persistence path (no `..`, no separators) and the store
    collection name.
    """
    if not isinstance(namespace, str) or not NAMESPACE_RE.match(namespace):
        raise SecurityError(
            "Invalid namespace: must match ^[A-Za-z0-9_-]{1,64}$"
        )
    return namespace


def safe_resolve(base: str | Path, candidate: str | Path) -> Path:
    """
    Resolve `candidate` inside `base` and guarantee the result stays within it.

    Rejects absolute paths, `..` traversal, and symlink escapes by comparing the
    fully-resolved target against the resolved base. Returns the confined
    absolute Path.
    """
    base_resolved = Path(base).resolve()
    target = (base_resolved / Path(candidate)).resolve()
    if target != base_resolved and base_resolved not in target.parents:
        raise SecurityError("Path escapes the allowed base directory")
    return target


def sanitize_filename(filename: str) -> str:
    """
    Reduce an untrusted client filename to a safe basename: strip any directory
    components, allow only `[A-Za-z0-9._-]`, and bound the length. Never returns
    an empty string.
    """
    base = Path(filename or "").name  # drops any path components
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    cleaned = cleaned[:MAX_FILENAME_LENGTH]
    return cleaned or "upload"


def validate_upload_extension(filename: str) -> str:
    """Return the lowercased extension if allowed, else raise SecurityError."""
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_UPLOAD_EXTENSIONS))
        raise SecurityError(f"Unsupported file type '{ext or '(none)'}'. Allowed: {allowed}")
    return ext


class RateLimiter:
    """
    Thread-safe token-bucket rate limiter keyed by an arbitrary string
    (typically client IP). `capacity` tokens refill at `capacity/60` per second
    by default, giving roughly `capacity` requests per minute with bursts.
    """

    def __init__(self, capacity: int = 60, refill_per_sec: float | None = None) -> None:
        self._capacity = float(max(1, capacity))
        self._refill = refill_per_sec if refill_per_sec is not None else self._capacity / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_ts)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        """Consume one token for `key`; return False if the bucket is empty."""
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self._capacity, now))
            tokens = min(self._capacity, tokens + (now - last) * self._refill)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False
            self._buckets[key] = (tokens - 1.0, now)
            return True
