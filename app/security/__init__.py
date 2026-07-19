"""
app/security/__init__.py

AEOS Security Layer — P12A.1-3 (JWT Federation Upgrade)

Replaces HMAC-SHA256 (SEC-003 violation) with asymmetric RS256/ES256.

Components:
  - KeyStore: manages RSA/EC key pairs with rotation
  - TokenSigner: creates signed JWTs
  - TokenVerifier: verifies tokens (local or from JWKS endpoint)
  - JWKSEndpoint: serves public keys for federation
  - KeyRotator: rolling key rotation with overlap window
"""

from .jwks import JWKSProvider, JWKSEndpoint, JWK
from .key_rotation import KeyStore, ManagedKey, KeyRotator, KeyAlgorithm
from .token_verifier import TokenVerifier, TokenClaims, TokenError

__all__ = [
    "JWKSProvider", "JWKSEndpoint", "JWK",
    "KeyStore", "ManagedKey", "KeyRotator", "KeyAlgorithm",
    "TokenVerifier", "TokenClaims", "TokenError",
]
