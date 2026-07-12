"""
AEOS Unit Tests — RAG security helpers
"""
import pytest

from app.rag.security import (
    RateLimiter,
    SecurityError,
    safe_resolve,
    sanitize_filename,
    validate_namespace,
    validate_upload_extension,
)


# ── Namespace validation ─────────────────────────────────────────────────────

@pytest.mark.parametrize("ns", ["default", "user_docs", "github-index", "A1_b-2", "x" * 64])
def test_valid_namespaces(ns):
    assert validate_namespace(ns) == ns


@pytest.mark.parametrize("ns", ["../etc", "a/b", "a b", "", "x" * 65, "空", "a.b", "a;b", "a$b"])
def test_invalid_namespaces_rejected(ns):
    with pytest.raises(SecurityError):
        validate_namespace(ns)


# ── Path confinement ─────────────────────────────────────────────────────────

def test_safe_resolve_allows_child(tmp_path):
    target = safe_resolve(tmp_path, "sub/file.txt")
    assert str(target).startswith(str(tmp_path.resolve()))


def test_safe_resolve_blocks_traversal(tmp_path):
    with pytest.raises(SecurityError):
        safe_resolve(tmp_path, "../../etc/passwd")


def test_safe_resolve_blocks_absolute_escape(tmp_path):
    # An absolute path outside the base must be rejected.
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(SecurityError):
        safe_resolve(tmp_path, str(outside))


# ── Filename sanitisation ────────────────────────────────────────────────────

def test_sanitize_strips_directories():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\Windows\\evil.txt") == "evil.txt"


def test_sanitize_removes_unsafe_chars():
    out = sanitize_filename("my file (1)!.md")
    assert "/" not in out and " " not in out and "!" not in out
    assert out.endswith(".md")


def test_sanitize_never_empty():
    assert sanitize_filename("") == "upload"
    assert sanitize_filename("...") == "upload"


# ── Upload extension allow-list ──────────────────────────────────────────────

@pytest.mark.parametrize("name", ["a.txt", "a.md", "a.PDF", "a.html", "a.json"])
def test_allowed_upload_extensions(name):
    assert validate_upload_extension(name)


@pytest.mark.parametrize("name", ["a.exe", "a.sh", "a.py", "a", "a.zip"])
def test_disallowed_upload_extensions(name):
    with pytest.raises(SecurityError):
        validate_upload_extension(name)


# ── Rate limiter ─────────────────────────────────────────────────────────────

def test_rate_limiter_allows_then_blocks():
    rl = RateLimiter(capacity=2)
    assert rl.allow("ip") is True
    assert rl.allow("ip") is True
    assert rl.allow("ip") is False  # bucket exhausted


def test_rate_limiter_is_per_key():
    rl = RateLimiter(capacity=1)
    assert rl.allow("a") is True
    assert rl.allow("b") is True   # different key, own bucket
    assert rl.allow("a") is False
