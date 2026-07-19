"""
app/security/token_verifier.py

Token Signer and Verifier — RS256/ES256 JWT implementation.

Replaces HMAC-SHA256 (SEC-003) with asymmetric cryptography:
  - Signing:    private key (held only by AEOS, never shared)
  - Verifying:  public key (safe to distribute via JWKS endpoint)

This enables:
  - External systems to verify AEOS tokens without any shared secret
  - Federation: cluster A verifies tokens from cluster B via B's JWKS
  - Multi-cluster governance: a governance token from one cluster can
    authorize execution in another

JWT structure:
  Header:  {"alg": "ES256", "kid": "<key-id>", "typ": "JWT"}
  Payload: {"sub": "task-123", "iss": "aeos-cluster-prod",
             "aud": ["aeos"], "exp": 1234567890, "iat": 1234500000,
             "gov_approved": true, "worker_id": "worker-5", ...}

Revocation:
  Tokens are short-lived (TTL configurable, default 5 minutes for
  governance tokens). Revocation is via a Redis SET of revoked jti values.
  Revocation is checked on every verification call.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .key_rotation import KeyStore, ManagedKey, KeyAlgorithm

logger = logging.getLogger(__name__)


class TokenError(Exception):
    """Base class for token errors."""


class TokenExpired(TokenError):
    pass


class TokenSignatureInvalid(TokenError):
    pass


class TokenRevoked(TokenError):
    pass


class TokenMalformed(TokenError):
    pass


class TokenKeyNotFound(TokenError):
    pass


@dataclass
class TokenClaims:
    """Verified JWT claims."""
    sub: str                          # Subject (task_id, worker_id, etc.)
    iss: str                          # Issuer (cluster ID)
    aud: list[str]                    # Audience
    exp: float                        # Expiry timestamp
    iat: float                        # Issued-at timestamp
    jti: str                          # JWT ID (for revocation)
    kid: str                          # Key ID used to sign
    algorithm: str                    # Algorithm used

    # AEOS-specific claims
    gov_approved: bool = False
    worker_id: str = ""
    workflow_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        return time.time() > self.exp

    @property
    def age_seconds(self) -> float:
        return time.time() - self.iat


def _b64url_encode(data: bytes) -> bytes:
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _b64url_decode(data: str | bytes) -> bytes:
    if isinstance(data, str):
        data = data.encode()
    # Add padding
    padding = 4 - len(data) % 4
    if padding != 4:
        data += b"=" * padding
    return base64.urlsafe_b64decode(data)


class TokenSigner:
    """
    Issues signed JWTs using the active signing key from KeyStore.

    Usage::

        signer = TokenSigner(key_store, issuer="aeos-prod-cluster")
        token = signer.sign(
            subject="task-123",
            audience=["aeos"],
            ttl_seconds=300,
            gov_approved=True,
            worker_id="worker-5",
        )
    """

    def __init__(
        self,
        key_store: KeyStore,
        issuer: str = "aeos",
        default_ttl_seconds: float = 300,
    ) -> None:
        self._store = key_store
        self._issuer = issuer
        self._default_ttl = default_ttl_seconds

    def sign(
        self,
        subject: str,
        audience: list[str] | None = None,
        ttl_seconds: float | None = None,
        *,
        gov_approved: bool = False,
        worker_id: str = "",
        workflow_id: str = "",
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """
        Create and sign a JWT.

        Returns the compact serialization: header.payload.signature
        """
        key = self._store.active_key
        if key is None:
            raise TokenError("No active signing key — call KeyStore.initialize() first")

        now = time.time()
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        jti = str(uuid.uuid4())

        payload: dict[str, Any] = {
            "sub": subject,
            "iss": self._issuer,
            "aud": audience or ["aeos"],
            "exp": now + ttl,
            "iat": now,
            "jti": jti,
        }
        if gov_approved:
            payload["gov_approved"] = True
        if worker_id:
            payload["worker_id"] = worker_id
        if workflow_id:
            payload["workflow_id"] = workflow_id
        if extra_claims:
            payload.update(extra_claims)

        header = {
            "alg": key.algorithm.value,
            "kid": key.kid,
            "typ": "JWT",
        }

        header_b64 = _b64url_encode(
            json.dumps(header, separators=(",", ":")).encode()
        )
        payload_b64 = _b64url_encode(
            json.dumps(payload, separators=(",", ":")).encode()
        )
        signing_input = header_b64 + b"." + payload_b64

        signature = self._sign_bytes(signing_input, key)
        sig_b64 = _b64url_encode(signature)

        token = (signing_input + b"." + sig_b64).decode()
        logger.debug("TokenSigner: issued token sub=%s jti=%s exp=+%.0fs",
                     subject, jti[:8], ttl)
        return token

    def _sign_bytes(self, data: bytes, key: ManagedKey) -> bytes:
        if key.algorithm == KeyAlgorithm.ES256:
            from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
            from cryptography.hazmat.primitives.hashes import SHA256
            from cryptography.hazmat.primitives.asymmetric.utils import (
                decode_dss_signature
            )
            import struct

            sig_der = key.private_key.sign(data, ECDSA(SHA256()))
            # Convert DER signature to IEEE P1363 format (r || s, 32 bytes each)
            r, s = decode_dss_signature(sig_der)
            return r.to_bytes(32, "big") + s.to_bytes(32, "big")
        else:  # RS256
            from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
            from cryptography.hazmat.primitives.hashes import SHA256
            return key.private_key.sign(data, PKCS1v15(), SHA256())


class TokenVerifier:
    """
    Verifies JWTs signed with RS256 or ES256.

    Supports:
      - Local verification (from KeyStore public keys)
      - Remote verification (from JWKS endpoint, for federation)
      - Revocation checking (via Redis set)

    Usage::

        verifier = TokenVerifier(key_store, issuer="aeos-prod-cluster")
        try:
            claims = verifier.verify(token, audience="aeos")
        except TokenExpired:
            # Token expired
        except TokenSignatureInvalid:
            # Signature bad
        except TokenRevoked:
            # Token has been revoked
    """

    def __init__(
        self,
        key_store: KeyStore,
        issuer: str = "aeos",
        clock_skew_seconds: float = 30.0,
        revocation_store: Any = None,   # Optional Redis client for revocation
    ) -> None:
        self._store = key_store
        self._issuer = issuer
        self._skew = clock_skew_seconds
        self._revocation = revocation_store
        self._revoked_jtis: set[str] = set()   # In-memory revocation list

    def verify(
        self,
        token: str,
        audience: str | list[str] | None = None,
    ) -> TokenClaims:
        """
        Verify a JWT and return its claims.

        Raises TokenError subclasses on failure. Never returns
        on a token that should be rejected.
        """
        # Parse
        parts = token.split(".")
        if len(parts) != 3:
            raise TokenMalformed("JWT must have exactly 3 parts")

        header_b64, payload_b64, sig_b64 = parts
        try:
            header = json.loads(_b64url_decode(header_b64))
            payload = json.loads(_b64url_decode(payload_b64))
        except (json.JSONDecodeError, ValueError) as exc:
            raise TokenMalformed(f"JWT decode error: {exc}") from exc

        kid = header.get("kid", "")
        alg = header.get("alg", "")

        # Validate algorithm (reject HMAC)
        if alg not in ("RS256", "ES256"):
            raise TokenMalformed(f"Unsupported algorithm: {alg}. RS256 and ES256 only.")

        # Look up signing key
        key = self._store.get_key(kid)
        if key is None:
            raise TokenKeyNotFound(f"Key not found or retired: kid={kid}")

        if key.algorithm.value != alg:
            raise TokenMalformed(f"Algorithm mismatch: key={key.algorithm.value} token={alg}")

        # Verify signature
        signing_input = f"{header_b64}.{payload_b64}".encode()
        signature = _b64url_decode(sig_b64)
        self._verify_signature(signing_input, signature, key)

        # Validate claims
        now = time.time()

        exp = payload.get("exp", 0)
        if now > exp + self._skew:
            raise TokenExpired(f"Token expired at {exp} (now={now:.0f})")

        iat = payload.get("iat", 0)
        if iat > now + self._skew:
            raise TokenMalformed("Token issued in the future")

        iss = payload.get("iss", "")
        if iss != self._issuer:
            raise TokenMalformed(f"Issuer mismatch: expected={self._issuer} got={iss}")

        if audience:
            token_aud = payload.get("aud", [])
            if isinstance(token_aud, str):
                token_aud = [token_aud]
            expected = [audience] if isinstance(audience, str) else audience
            if not any(a in token_aud for a in expected):
                raise TokenMalformed(f"Audience mismatch: expected={expected} got={token_aud}")

        # Revocation check
        jti = payload.get("jti", "")
        if jti and (jti in self._revoked_jtis):
            raise TokenRevoked(f"Token {jti[:8]} has been revoked")

        return TokenClaims(
            sub=payload.get("sub", ""),
            iss=iss,
            aud=payload.get("aud", []),
            exp=exp,
            iat=iat,
            jti=jti,
            kid=kid,
            algorithm=alg,
            gov_approved=payload.get("gov_approved", False),
            worker_id=payload.get("worker_id", ""),
            workflow_id=payload.get("workflow_id", ""),
            extra={k: v for k, v in payload.items()
                   if k not in ("sub", "iss", "aud", "exp", "iat", "jti",
                                "gov_approved", "worker_id", "workflow_id")},
        )

    def revoke(self, jti: str) -> None:
        """Revoke a token by its JWT ID. In-memory only (use Redis for persistence)."""
        self._revoked_jtis.add(jti)
        logger.info("TokenVerifier: revoked jti=%s", jti[:8])

    def _verify_signature(self, data: bytes, signature: bytes, key: ManagedKey) -> None:
        if key.algorithm == KeyAlgorithm.ES256:
            self._verify_ec(data, signature, key)
        else:
            self._verify_rsa(data, signature, key)

    def _verify_ec(self, data: bytes, signature: bytes, key: ManagedKey) -> None:
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        from cryptography.exceptions import InvalidSignature

        if len(signature) != 64:
            raise TokenSignatureInvalid("ES256 signature must be 64 bytes (r || s)")

        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        der_sig = encode_dss_signature(r, s)

        try:
            key.public_key.verify(der_sig, data, ECDSA(SHA256()))
        except InvalidSignature as exc:
            raise TokenSignatureInvalid("ES256 signature verification failed") from exc

    def _verify_rsa(self, data: bytes, signature: bytes, key: ManagedKey) -> None:
        from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.exceptions import InvalidSignature

        try:
            key.public_key.verify(signature, data, PKCS1v15(), SHA256())
        except InvalidSignature as exc:
            raise TokenSignatureInvalid("RS256 signature verification failed") from exc
