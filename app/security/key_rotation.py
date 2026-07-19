"""
app/security/key_rotation.py

Key Store and Key Rotation — manages RSA/EC key pairs for JWT signing.

Design:
  - Each key has a kid (key ID), algorithm, creation time, and expiry
  - At any time, exactly ONE key is the active signing key
  - During rotation overlap, the previous key remains valid for verification
    (tokens issued under it can still be verified)
  - Keys are persisted to disk (PEM format) for restart safety
  - Key material is never logged

Rotation process:
  1. Generate new key pair → store as "next"
  2. After overlap_seconds: promote "next" → "active", old "active" → "previous"
  3. After another overlap_seconds: retire "previous" (tokens must have expired)
  4. Publish new JWKS (public keys) to connected verifiers

Supported algorithms:
  - RS256: RSA 2048-bit + SHA-256 (broadest compatibility)
  - ES256: ECDSA P-256 + SHA-256 (smaller tokens, faster verification)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class KeyAlgorithm(str, Enum):
    RS256 = "RS256"
    ES256 = "ES256"


@dataclass
class ManagedKey:
    """A managed RSA or EC key pair."""
    kid: str
    algorithm: KeyAlgorithm
    created_at: float
    expires_at: float               # When this key should stop being used for signing
    retire_at: float                # When this key should be removed from JWKS
    active: bool = False            # True if this is the current signing key

    # Key material (private key for signing; public key derived on demand)
    _private_key: Any = field(default=None, repr=False)
    _public_key: Any = field(default=None, repr=False)

    @property
    def private_key(self) -> Any:
        return self._private_key

    @property
    def public_key(self) -> Any:
        return self._public_key

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    @property
    def should_retire(self) -> bool:
        return time.time() > self.retire_at


def _load_crypto() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import rsa, ec
        from cryptography.hazmat.backends import default_backend
        return rsa, ec, default_backend
    except ImportError as exc:
        raise ImportError(
            "cryptography package required for JWT RS256/ES256. "
            "Install: pip install cryptography"
        ) from exc


class KeyStore:
    """
    Manages asymmetric key pairs for JWT signing.

    Keys are persisted to <keys_dir>/<kid>.pem (private key, PEM format).
    On restart, existing keys are loaded from disk.

    Usage::

        store = KeyStore(keys_dir="/var/lib/aeos/keys", algorithm=KeyAlgorithm.RS256)
        store.initialize()

        # Get the active signing key
        key = store.active_key
        token = sign_jwt(payload, key.private_key, key.kid, key.algorithm)

        # Get all public keys (for JWKS)
        public_keys = store.public_keys()

        # Rotate: generate new key, promote after overlap
        store.rotate()
    """

    def __init__(
        self,
        keys_dir: str = "data/keys",
        algorithm: KeyAlgorithm = KeyAlgorithm.ES256,
        key_ttl_seconds: float = 86400 * 7,       # 7 days active
        overlap_seconds: float = 86400,            # 1 day overlap for rotation
    ) -> None:
        self._keys_dir = Path(keys_dir)
        self._keys_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._algorithm = algorithm
        self._key_ttl = key_ttl_seconds
        self._overlap = overlap_seconds
        self._keys: dict[str, ManagedKey] = {}

    def initialize(self) -> None:
        """Load existing keys from disk or create an initial key pair."""
        self._load_from_disk()
        if not self._keys or not self.active_key:
            logger.info("KeyStore: no active key found — generating initial key pair")
            self._generate_and_activate()

    @property
    def active_key(self) -> ManagedKey | None:
        """Return the current active signing key."""
        return next((k for k in self._keys.values() if k.active), None)

    def valid_keys(self) -> list[ManagedKey]:
        """Return all keys that are still valid for verification (not yet retired)."""
        now = time.time()
        return [k for k in self._keys.values() if k.retire_at > now]

    def rotate(self) -> ManagedKey:
        """
        Generate a new key and promote it to active.

        The previous active key remains in valid_keys() for `overlap_seconds`
        to allow in-flight tokens to be verified.

        Returns the new active key.
        """
        now = time.time()

        # Demote current active key
        if self.active_key:
            old = self.active_key
            old.active = False
            # Retire after overlap window (enough time for tokens to expire)
            old.retire_at = now + self._overlap
            logger.info(
                "KeyStore: demoted key %s (retires at %s)",
                old.kid, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(old.retire_at)),
            )

        # Generate new key
        new_key = self._generate_and_activate()

        # Prune keys past their retire time
        self._prune_retired()

        return new_key

    def public_keys(self) -> list[ManagedKey]:
        """Return all valid keys (for JWKS endpoint)."""
        return self.valid_keys()

    def get_key(self, kid: str) -> ManagedKey | None:
        """Look up a key by kid (for token verification)."""
        key = self._keys.get(kid)
        if key and not key.should_retire:
            return key
        return None

    # ── Internal ──────────────────────────────────────────────────────────

    def _generate_and_activate(self) -> ManagedKey:
        rsa_mod, ec_mod, backend = _load_crypto()
        kid = str(uuid.uuid4())
        now = time.time()

        if self._algorithm == KeyAlgorithm.RS256:
            private_key = rsa_mod.generate_private_key(
                public_exponent=65537,
                key_size=2048,
                backend=backend(),
            )
        else:  # ES256
            private_key = ec_mod.generate_private_key(
                ec_mod.SECP256R1(),
                backend=backend(),
            )

        managed = ManagedKey(
            kid=kid,
            algorithm=self._algorithm,
            created_at=now,
            expires_at=now + self._key_ttl,
            retire_at=now + self._key_ttl + self._overlap,
            active=True,
        )
        managed._private_key = private_key
        managed._public_key = private_key.public_key()

        self._keys[kid] = managed
        self._persist_key(managed)

        logger.info(
            "KeyStore: generated new %s key kid=%s (active, expires %s)",
            self._algorithm.value, kid[:8],
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(managed.expires_at)),
        )
        return managed

    def _persist_key(self, key: ManagedKey) -> None:
        """Write private key PEM + metadata to disk."""
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PrivateFormat, NoEncryption
        )
        pem = key.private_key.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=NoEncryption(),
        )
        key_path = self._keys_dir / f"{key.kid}.pem"
        # Write with restricted permissions (owner read-only)
        fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, pem)
        finally:
            os.close(fd)

        meta = {
            "kid": key.kid,
            "algorithm": key.algorithm.value,
            "created_at": key.created_at,
            "expires_at": key.expires_at,
            "retire_at": key.retire_at,
            "active": key.active,
        }
        meta_path = self._keys_dir / f"{key.kid}.meta.json"
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def _load_from_disk(self) -> None:
        """Load existing keys from disk."""
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.backends import default_backend

        for meta_path in self._keys_dir.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                kid = meta["kid"]
                pem_path = self._keys_dir / f"{kid}.pem"
                if not pem_path.exists():
                    continue
                private_key = load_pem_private_key(
                    pem_path.read_bytes(),
                    password=None,
                    backend=default_backend(),
                )
                managed = ManagedKey(
                    kid=kid,
                    algorithm=KeyAlgorithm(meta["algorithm"]),
                    created_at=meta["created_at"],
                    expires_at=meta["expires_at"],
                    retire_at=meta["retire_at"],
                    active=meta.get("active", False),
                )
                managed._private_key = private_key
                managed._public_key = private_key.public_key()
                self._keys[kid] = managed
                logger.debug("KeyStore: loaded key %s (active=%s)", kid[:8], managed.active)
            except Exception as exc:
                logger.warning("KeyStore: failed to load key from %s: %s", meta_path, exc)

    def _prune_retired(self) -> None:
        retired = [kid for kid, k in self._keys.items() if k.should_retire]
        for kid in retired:
            del self._keys[kid]
            (self._keys_dir / f"{kid}.pem").unlink(missing_ok=True)
            (self._keys_dir / f"{kid}.meta.json").unlink(missing_ok=True)
            logger.info("KeyStore: pruned retired key %s", kid[:8])


class KeyRotator:
    """
    Background task that automatically rotates keys before expiry.

    Usage::

        rotator = KeyRotator(store, rotation_interval_hours=24 * 6)
        await rotator.start()
        # Runs until stopped
        await rotator.stop()
    """

    def __init__(
        self,
        store: KeyStore,
        rotation_interval_hours: float = 24 * 6,  # Rotate every 6 days (key TTL=7d)
        on_rotation: Any = None,                   # Optional async callback
    ) -> None:
        self._store = store
        self._interval = rotation_interval_hours * 3600
        self._on_rotation = on_rotation
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._rotation_loop())
        logger.info("KeyRotator started (interval=%.0fh)", self._interval / 3600)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _rotation_loop(self) -> None:
        while self._running:
            try:
                # Check if active key is within overlap window of expiry
                active = self._store.active_key
                if active and active.expires_at - time.time() < self._store._overlap:
                    logger.info("KeyRotator: active key nearing expiry — rotating")
                    new_key = self._store.rotate()
                    if self._on_rotation:
                        await self._on_rotation(new_key)

                await asyncio.sleep(min(3600, self._interval / 24))  # Check hourly
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("KeyRotator error: %s", exc)
                await asyncio.sleep(60)
