"""
Software Intelligence Platform — Cache Store
=============================================
Temporary storage for parsed results, metrics, and reports to avoid
redundant computation.

Cache backends:
  InMemoryCache   — LRU dict with TTL eviction (default, suitable for single-worker)
  RedisCache      — distributed cache (for multi-worker deployments)
  FilesystemCache — disk-backed cache (for local development / offline use)

Design:
  - Keys are hashed from (repo_id, file_path, content_hash, operation)
  - Values are pickled analysis artifacts (ParseResult, FileMetrics, etc.)
  - TTL: default 1 hour for parse results, 24 hours for aggregated reports
  - Automatic invalidation on file content change (via sha256 hash)
"""

from __future__ import annotations

import hashlib
import json
import pickle
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar


T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    key:        str
    value:      T
    expires_at: float    # Unix timestamp


# ── Abstract cache backend ─────────────────────────────────────────────────────

class BaseCacheBackend(ABC):
    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def keys(self) -> list[str]: ...


# ── In-memory LRU cache ────────────────────────────────────────────────────────

class InMemoryCache(BaseCacheBackend):
    """
    LRU cache with TTL expiration.
    Thread-safe for single-process use; not suitable for multi-worker.
    """

    def __init__(self, max_size: int = 1000) -> None:
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._max_size = max_size

    def get(self, key: str) -> Any | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.time() > entry.expires_at:
            del self._cache[key]
            return None
        # LRU: move to end
        self._cache.move_to_end(key)
        return entry.value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        self._cache[key] = CacheEntry(key=key, value=value, expires_at=expires_at)
        self._cache.move_to_end(key)
        # Evict oldest if over capacity
        if len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    def delete(self, key: str) -> None:
        self._cache.pop(key, None)

    def clear(self) -> None:
        self._cache.clear()

    def keys(self) -> list[str]:
        now = time.time()
        # Prune expired entries
        expired = [k for k, e in self._cache.items() if e.expires_at < now]
        for k in expired:
            del self._cache[k]
        return list(self._cache.keys())


# ── Filesystem cache ───────────────────────────────────────────────────────────

class FilesystemCache(BaseCacheBackend):
    """
    Disk-backed cache using pickle files.
    Each entry is stored as {cache_dir}/{key_hash}.pkl with metadata.
    """

    def __init__(self, cache_dir: str = ".cache/software_intelligence") -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, key: str) -> Path:
        key_hash = hashlib.md5(key.encode()).hexdigest()
        return self._cache_dir / f"{key_hash}.pkl"

    def get(self, key: str) -> Any | None:
        path = self._key_path(key)
        if not path.exists():
            return None
        try:
            with path.open("rb") as f:
                entry: CacheEntry = pickle.load(f)
            if time.time() > entry.expires_at:
                path.unlink(missing_ok=True)
                return None
            return entry.value
        except Exception:
            path.unlink(missing_ok=True)
            return None

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        entry = CacheEntry(key=key, value=value, expires_at=expires_at)
        path = self._key_path(key)
        try:
            with path.open("wb") as f:
                pickle.dump(entry, f)
        except Exception:
            pass

    def delete(self, key: str) -> None:
        self._key_path(key).unlink(missing_ok=True)

    def clear(self) -> None:
        for path in self._cache_dir.glob("*.pkl"):
            path.unlink(missing_ok=True)

    def keys(self) -> list[str]:
        keys = []
        now = time.time()
        for path in self._cache_dir.glob("*.pkl"):
            try:
                with path.open("rb") as f:
                    entry: CacheEntry = pickle.load(f)
                if entry.expires_at < now:
                    path.unlink(missing_ok=True)
                else:
                    keys.append(entry.key)
            except Exception:
                path.unlink(missing_ok=True)
        return keys


# ── Redis cache (stub) ─────────────────────────────────────────────────────────

class RedisCache(BaseCacheBackend):
    """
    Distributed cache using Redis.
    Requires redis-py: pip install redis
    """

    def __init__(self, host: str = "localhost", port: int = 6379, db: int = 0) -> None:
        self._host = host
        self._port = port
        self._db   = db
        self._client: Any = None

    def _connect(self) -> None:
        if self._client is None:
            try:
                import redis
                self._client = redis.Redis(host=self._host, port=self._port, db=self._db)
            except ImportError:
                raise ImportError("Install redis: pip install redis")

    def get(self, key: str) -> Any | None:
        self._connect()
        data = self._client.get(key)
        if data is None:
            return None
        return pickle.loads(data)

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._connect()
        data = pickle.dumps(value)
        self._client.setex(key, ttl_seconds, data)

    def delete(self, key: str) -> None:
        self._connect()
        self._client.delete(key)

    def clear(self) -> None:
        self._connect()
        self._client.flushdb()

    def keys(self) -> list[str]:
        self._connect()
        return [k.decode() for k in self._client.keys("*")]


# ── Cache store facade ─────────────────────────────────────────────────────────

class CacheStore:
    """
    High-level cache for Software Intelligence artifacts.

    Usage:
        cache = CacheStore(backend=InMemoryCache())
        cache.set_parse_result(repo_id, file_path, content_hash, parse_result)
        result = cache.get_parse_result(repo_id, file_path, content_hash)
    """

    DEFAULT_TTL = {
        "parse_result":       3600,      # 1 hour
        "file_metrics":       3600,      # 1 hour
        "repo_metrics":       86400,     # 24 hours
        "security_report":    86400,     # 24 hours
        "debt_report":        86400,     # 24 hours
        "review_report":      3600,      # 1 hour
        "architecture":       86400,     # 24 hours
        "knowledge_graph":    86400,     # 24 hours
    }

    def __init__(self, backend: BaseCacheBackend | None = None) -> None:
        self._backend = backend or InMemoryCache()

    @classmethod
    def in_memory(cls, max_size: int = 1000) -> "CacheStore":
        return cls(backend=InMemoryCache(max_size=max_size))

    @classmethod
    def filesystem(cls, cache_dir: str = ".cache/software_intelligence") -> "CacheStore":
        return cls(backend=FilesystemCache(cache_dir=cache_dir))

    @classmethod
    def redis(cls, host: str = "localhost", port: int = 6379) -> "CacheStore":
        return cls(backend=RedisCache(host=host, port=port))

    # ── Key builders ───────────────────────────────────────────────────────────

    def _key(self, *parts: str) -> str:
        return ":".join(parts)

    def _hash_content(self, content: str) -> str:
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    # ── Parse results ──────────────────────────────────────────────────────────

    def set_parse_result(
        self,
        repo_id: str,
        file_path: str,
        content_hash: str,
        result: Any,
    ) -> None:
        key = self._key("parse", repo_id, file_path, content_hash)
        self._backend.set(key, result, self.DEFAULT_TTL["parse_result"])

    def get_parse_result(
        self,
        repo_id: str,
        file_path: str,
        content_hash: str,
    ) -> Any | None:
        key = self._key("parse", repo_id, file_path, content_hash)
        return self._backend.get(key)

    # ── File metrics ───────────────────────────────────────────────────────────

    def set_file_metrics(
        self,
        repo_id: str,
        file_path: str,
        content_hash: str,
        metrics: Any,
    ) -> None:
        key = self._key("metrics", repo_id, file_path, content_hash)
        self._backend.set(key, metrics, self.DEFAULT_TTL["file_metrics"])

    def get_file_metrics(
        self,
        repo_id: str,
        file_path: str,
        content_hash: str,
    ) -> Any | None:
        key = self._key("metrics", repo_id, file_path, content_hash)
        return self._backend.get(key)

    # ── Repository-level artifacts ─────────────────────────────────────────────

    def set_repo_metrics(self, repo_id: str, metrics: Any) -> None:
        key = self._key("repo_metrics", repo_id)
        self._backend.set(key, metrics, self.DEFAULT_TTL["repo_metrics"])

    def get_repo_metrics(self, repo_id: str) -> Any | None:
        key = self._key("repo_metrics", repo_id)
        return self._backend.get(key)

    def set_security_report(self, repo_id: str, report: Any) -> None:
        key = self._key("security", repo_id)
        self._backend.set(key, report, self.DEFAULT_TTL["security_report"])

    def get_security_report(self, repo_id: str) -> Any | None:
        key = self._key("security", repo_id)
        return self._backend.get(key)

    def set_debt_report(self, repo_id: str, report: Any) -> None:
        key = self._key("debt", repo_id)
        self._backend.set(key, report, self.DEFAULT_TTL["debt_report"])

    def get_debt_report(self, repo_id: str) -> Any | None:
        key = self._key("debt", repo_id)
        return self._backend.get(key)

    def set_architecture(self, repo_id: str, arch: Any) -> None:
        key = self._key("architecture", repo_id)
        self._backend.set(key, arch, self.DEFAULT_TTL["architecture"])

    def get_architecture(self, repo_id: str) -> Any | None:
        key = self._key("architecture", repo_id)
        return self._backend.get(key)

    def set_knowledge_graph(self, repo_id: str, graph: Any) -> None:
        key = self._key("kg", repo_id)
        self._backend.set(key, graph, self.DEFAULT_TTL["knowledge_graph"])

    def get_knowledge_graph(self, repo_id: str) -> Any | None:
        key = self._key("kg", repo_id)
        return self._backend.get(key)

    # ── Invalidation ───────────────────────────────────────────────────────────

    def invalidate_repo(self, repo_id: str) -> None:
        """Invalidate all cached artifacts for a repository."""
        for key in self._backend.keys():
            if key.startswith(f"repo_metrics:{repo_id}") or \
               key.startswith(f"security:{repo_id}") or \
               key.startswith(f"debt:{repo_id}") or \
               key.startswith(f"architecture:{repo_id}") or \
               key.startswith(f"kg:{repo_id}"):
                self._backend.delete(key)

    def clear_all(self) -> None:
        """Clear entire cache."""
        self._backend.clear()

    def stats(self) -> dict[str, int]:
        """Return cache statistics."""
        keys = self._backend.keys()
        return {
            "total_keys": len(keys),
            "parse_results": len([k for k in keys if k.startswith("parse:")]),
            "metrics": len([k for k in keys if k.startswith("metrics:")]),
            "reports": len([k for k in keys if k.startswith(("security:", "debt:", "architecture:"))]),
        }
