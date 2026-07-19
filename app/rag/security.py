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
from pathlib import Path

# RateLimiter now lives in core so any layer can use it; re-exported here for
# backward compatibility with existing imports (app.rag.security.RateLimiter).
from app.core.ratelimit import RateLimiter, RateDecision  # noqa: F401

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


# Magic bytes of executables / archives we never accept, regardless of extension.
_EXECUTABLE_MAGIC = (
    b"MZ",              # Windows PE / DOS
    b"\x7fELF",         # Linux ELF
    b"\xca\xfe\xba\xbe",  # Mach-O / Java class
    b"\xcf\xfa\xed\xfe",  # Mach-O 64
    b"\xfe\xed\xfa\xce",  # Mach-O
    b"PK\x03\x04",      # ZIP / Office / JAR (none of our allowed types are zip)
    b"\x1f\x8b",        # gzip
    b"\xde\xad\xbe\xef",  # some bytecode caches
)


def validate_upload_content(ext: str, data: bytes) -> None:
    """
    Content-based validation (not just the extension): confirm the *bytes* match
    the claimed type and are never an executable/archive. Complements the
    extension allow-list — an attacker renaming evil.exe → notes.txt is caught here.

    - Reject any known executable/archive magic signature.
    - `.pdf` must start with the `%PDF` signature.
    - Text types (.txt/.md/.markdown/.html/.htm/.json) must be NUL-free, decodable text.
    """
    if not data:
        raise SecurityError("Empty file.")
    for magic in _EXECUTABLE_MAGIC:
        if data.startswith(magic):
            raise SecurityError("File content is an executable/archive; rejected.")

    if ext == ".pdf":
        if not data.startswith(b"%PDF"):
            raise SecurityError("Content does not look like a valid PDF.")
        return

    # Remaining allowed types are text — must be genuine text, not disguised binary.
    if b"\x00" in data[:8192]:
        raise SecurityError("Binary content in a text file; rejected.")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            data.decode("latin-1")
        except UnicodeDecodeError:
            raise SecurityError("File is not valid text.")
