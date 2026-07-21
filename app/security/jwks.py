"""
app/security/jwks.py

JWKS (JSON Web Key Set) Provider and HTTP Endpoint.

Serves public keys in the standard JWKS format so that:
  - External systems can verify AEOS-issued tokens without shared secrets
  - Federation between AEOS clusters can use cross-cluster token validation
  - Third-party integrations (Okta, Auth0, Istio, OPA) can trust AEOS tokens

JWKS format (RFC 7517):
  {
    "keys": [
      {
        "kty": "EC",           // or "RSA"
        "kid": "uuid",
        "alg": "ES256",        // or "RS256"
        "use": "sig",
        "crv": "P-256",        // EC only
        "x": "...",            // EC only
        "y": "...",            // EC only
        "n": "...",            // RSA only
        "e": "AQAB"            // RSA only
      }
    ]
  }

The JWKS endpoint URL is: GET /.well-known/jwks.json
This is the OIDC-compatible discovery path.

Cache-Control: public, max-age=3600
  (Verifiers cache the JWKS; rotation overlap ensures old tokens still verify
   even after JWKS is updated)
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from .key_rotation import KeyStore, ManagedKey, KeyAlgorithm

logger = logging.getLogger(__name__)


class JWK:
    """A single JSON Web Key (public key representation)."""

    def __init__(self, managed_key: ManagedKey) -> None:
        self._key = managed_key

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JWK dict (RFC 7517)."""
        if self._key.algorithm == KeyAlgorithm.ES256:
            return self._ec_jwk()
        else:
            return self._rsa_jwk()

    def _ec_jwk(self) -> dict[str, Any]:
        from cryptography.hazmat.primitives.asymmetric.ec import (
            EllipticCurvePublicKey, SECP256R1
        )
        pub = self._key.public_key
        pub_numbers = pub.public_numbers()

        # Encode X and Y coordinates as URL-safe base64 (no padding)
        key_size_bytes = (pub.key_size + 7) // 8
        x_bytes = pub_numbers.x.to_bytes(key_size_bytes, "big")
        y_bytes = pub_numbers.y.to_bytes(key_size_bytes, "big")

        return {
            "kty": "EC",
            "kid": self._key.kid,
            "alg": "ES256",
            "use": "sig",
            "crv": "P-256",
            "x": _b64url(x_bytes),
            "y": _b64url(y_bytes),
        }

    def _rsa_jwk(self) -> dict[str, Any]:
        pub = self._key.public_key
        pub_numbers = pub.public_numbers()

        # n and e as URL-safe base64
        key_size_bytes = (pub.key_size + 7) // 8
        n_bytes = pub_numbers.n.to_bytes(key_size_bytes, "big")
        e_bytes = pub_numbers.e.to_bytes((pub_numbers.e.bit_length() + 7) // 8, "big")

        return {
            "kty": "RSA",
            "kid": self._key.kid,
            "alg": "RS256",
            "use": "sig",
            "n": _b64url(n_bytes),
            "e": _b64url(e_bytes),
        }


class JWKSProvider:
    """
    Produces the JWKS response from the current KeyStore state.

    Call jwks_dict() to get the current JWKS (all valid public keys).
    The result is ready to be served from /.well-known/jwks.json.

    Usage::

        provider = JWKSProvider(key_store)
        jwks = provider.jwks_dict()
        # Return as JSON with Cache-Control: public, max-age=3600
    """

    def __init__(self, key_store: KeyStore) -> None:
        self._store = key_store

    def jwks_dict(self) -> dict[str, Any]:
        """Return the JWKS as a JSON-serializable dict."""
        valid_keys = self._store.public_keys()
        jwks_keys = []
        for managed in valid_keys:
            try:
                jwk = JWK(managed)
                jwks_keys.append(jwk.to_dict())
            except Exception as exc:
                logger.error("JWKS: failed to serialize key %s: %s", managed.kid[:8], exc)
        return {"keys": jwks_keys}

    def jwks_json(self) -> str:
        """Return the JWKS as a JSON string."""
        return json.dumps(self.jwks_dict(), separators=(",", ":"))


class JWKSEndpoint:
    """
    FastAPI-compatible JWKS endpoint handler.

    Wires into the AEOS API server to serve public keys at the
    OIDC-compatible discovery path.

    Usage (in main.py or router)::

        from app.security.jwks import JWKSEndpoint

        endpoint = JWKSEndpoint(key_store)
        app.get("/.well-known/jwks.json")(endpoint.handle)
    """

    CACHE_MAX_AGE = 3600  # 1 hour — safe given 1-day rotation overlap

    def __init__(self, key_store: KeyStore) -> None:
        self._provider = JWKSProvider(key_store)

    async def handle(self) -> dict[str, Any]:
        """FastAPI route handler for JWKS endpoint."""
        return self._provider.jwks_dict()

    def fastapi_router(self) -> Any:
        """
        Return a FastAPI APIRouter pre-configured for JWKS serving.

        Usage::
            app.include_router(endpoint.fastapi_router())
        """
        try:
            from fastapi import APIRouter
            from fastapi.responses import JSONResponse
        except ImportError as exc:
            raise ImportError("fastapi required for JWKSEndpoint router") from exc

        router = APIRouter()

        @router.get("/.well-known/jwks.json", include_in_schema=True,
                    summary="JWKS — public key set for token verification")
        async def jwks() -> JSONResponse:
            return JSONResponse(
                content=self._provider.jwks_dict(),
                headers={
                    "Cache-Control": f"public, max-age={self.CACHE_MAX_AGE}",
                    "Content-Type": "application/json",
                },
            )

        return router


# ── Remote JWKS verifier (for federation) ─────────────────────────────────


class RemoteJWKSClient:
    """
    Downloads and caches a remote JWKS for cross-cluster token verification.

    Used when AEOS cluster A needs to verify tokens issued by cluster B.
    Caches the JWKS for cache_ttl_seconds, auto-refreshes on expiry.

    Usage::

        client = RemoteJWKSClient("https://cluster-b.aeos.internal/.well-known/jwks.json")
        public_key = await client.get_key(kid="abc123")
    """

    def __init__(self, jwks_url: str, cache_ttl_seconds: float = 3600) -> None:
        self._url = jwks_url
        self._cache_ttl = cache_ttl_seconds
        self._cached: dict[str, Any] = {}    # kid → JWK dict
        self._cached_at: float = 0.0

    async def get_key(self, kid: str) -> dict[str, Any] | None:
        """
        Return the JWK for the given kid.
        Refreshes cache if stale or kid not found.
        """
        if not self._cached or self._cache_expired():
            await self._refresh()

        key = self._cached.get(kid)
        if key is None and not self._cache_expired():
            # kid not in cache — try a forced refresh (key rotation happened)
            await self._refresh()
            key = self._cached.get(kid)

        return key

    def _cache_expired(self) -> bool:
        import time
        return time.time() - self._cached_at > self._cache_ttl

    async def _refresh(self) -> None:
        import time
        try:
            import aiohttp  # type: ignore[import]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self._url,
                    timeout=aiohttp.ClientTimeout(total=10),
                    ssl=True,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json()

            self._cached = {k["kid"]: k for k in data.get("keys", [])}
            self._cached_at = time.time()
            logger.info(
                "RemoteJWKSClient: refreshed %d keys from %s",
                len(self._cached), self._url,
            )
        except Exception as exc:
            logger.error("RemoteJWKSClient: failed to refresh JWKS from %s: %s", self._url, exc)


# ── Helpers ────────────────────────────────────────────────────────────────

def _b64url(data: bytes) -> str:
    """URL-safe base64 encoding without padding (RFC 7515 §2)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_int(value: str) -> int:
    """Decode a URL-safe base64 JWK field (x/y/n/e) into an integer."""
    raw = value.encode() if isinstance(value, str) else value
    padding = (-len(raw)) % 4
    raw = raw + b"=" * padding
    return int.from_bytes(base64.urlsafe_b64decode(raw), "big")


def _public_key_from_jwk(jwk: dict[str, Any]) -> ManagedKey:
    """Reconstruct a verify-only ``ManagedKey`` (public part only) from a JWK.

    This is the import side of the JWKS contract: given a public JWK a remote
    cluster published, rebuild the public key object so an AEOS ``TokenVerifier``
    can check signatures made by that cluster — WITHOUT ever seeing its private
    key. The inverse of ``JWK.to_dict``.
    """
    kty = jwk.get("kty")
    kid = jwk.get("kid", "")
    if kty == "EC":
        from cryptography.hazmat.primitives.asymmetric.ec import (
            EllipticCurvePublicNumbers, SECP256R1,
        )
        x = _b64url_int(jwk["x"])
        y = _b64url_int(jwk["y"])
        public_key = EllipticCurvePublicNumbers(x, y, SECP256R1()).public_key()
        algorithm = KeyAlgorithm.ES256
    elif kty == "RSA":
        from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
        n = _b64url_int(jwk["n"])
        e = _b64url_int(jwk["e"])
        public_key = RSAPublicNumbers(e, n).public_key()
        algorithm = KeyAlgorithm.RS256
    else:
        raise ValueError(f"unsupported JWK key type: {kty!r}")

    key = ManagedKey(
        kid=kid, algorithm=algorithm,
        created_at=0.0, expires_at=float("inf"), retire_at=float("inf"),
    )
    key._public_key = public_key
    return key


class JWKSKeyStore:
    """A verify-only, in-memory key set built from a remote cluster's JWKS.

    Exposes the minimal surface ``TokenVerifier`` needs (``get_key`` /
    ``public_keys``) so an AEOS verifier can validate another cluster's tokens
    from that cluster's *published public keys* alone. Holds no private keys and
    cannot sign — this is exactly the asymmetry federation trust relies on.
    """

    def __init__(self, jwks: dict[str, Any]) -> None:
        self._by_kid: dict[str, ManagedKey] = {}
        for jwk in jwks.get("keys", []):
            try:
                key = _public_key_from_jwk(jwk)
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("JWKSKeyStore: skipping malformed JWK: %s", exc)
                continue
            self._by_kid[key.kid] = key

    def get_key(self, kid: str) -> ManagedKey | None:
        return self._by_kid.get(kid)

    def public_keys(self) -> list[ManagedKey]:
        return list(self._by_kid.values())


def verifier_from_jwks(
    jwks: dict[str, Any],
    issuer: str,
    *,
    clock_skew_seconds: float = 30.0,
):
    """Build a ``TokenVerifier`` that validates ``issuer``'s tokens from its JWKS.

    The production federation path is: fetch the peer's ``/.well-known/jwks.json``
    (``RemoteJWKSClient``), then verify its tokens with this verifier. Imported
    here lazily to avoid a circular import with ``token_verifier``.
    """
    from .token_verifier import TokenVerifier

    return TokenVerifier(
        JWKSKeyStore(jwks),  # type: ignore[arg-type]
        issuer=issuer,
        clock_skew_seconds=clock_skew_seconds,
    )
