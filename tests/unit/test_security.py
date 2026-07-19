"""
tests/unit/test_security.py

Tests for AEOS JWT security layer (P12A.1-3).

Covers:
  - KeyStore initialization and persistence
  - Key rotation with overlap
  - RS256 sign + verify
  - ES256 sign + verify
  - Token expiry detection
  - Token revocation
  - Malformed token rejection
  - JWKS serialization
  - Algorithm downgrade rejection (no HMAC)
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("cryptography", reason="cryptography not installed")

from app.security.key_rotation import KeyStore, KeyAlgorithm, KeyRotator
from app.security.token_verifier import (
    TokenSigner, TokenVerifier,
    TokenExpired, TokenSignatureInvalid, TokenRevoked, TokenMalformed, TokenKeyNotFound,
)
from app.security.jwks import JWKSProvider, JWK


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def es256_store(tmp_path):
    store = KeyStore(
        keys_dir=str(tmp_path / "keys"),
        algorithm=KeyAlgorithm.ES256,
        key_ttl_seconds=300,
        overlap_seconds=60,
    )
    store.initialize()
    return store


@pytest.fixture
def rs256_store(tmp_path):
    store = KeyStore(
        keys_dir=str(tmp_path / "keys-rsa"),
        algorithm=KeyAlgorithm.RS256,
        key_ttl_seconds=300,
        overlap_seconds=60,
    )
    store.initialize()
    return store


@pytest.fixture
def es256_signer(es256_store):
    return TokenSigner(es256_store, issuer="aeos-test")


@pytest.fixture
def es256_verifier(es256_store):
    return TokenVerifier(es256_store, issuer="aeos-test", clock_skew_seconds=5.0)


@pytest.fixture
def rs256_signer(rs256_store):
    return TokenSigner(rs256_store, issuer="aeos-test")


@pytest.fixture
def rs256_verifier(rs256_store):
    return TokenVerifier(rs256_store, issuer="aeos-test")


# ── KeyStore tests ─────────────────────────────────────────────────────────

class TestKeyStore:
    def test_initialize_creates_active_key(self, es256_store):
        assert es256_store.active_key is not None
        assert es256_store.active_key.active is True
        assert es256_store.active_key.algorithm == KeyAlgorithm.ES256

    def test_public_key_not_none(self, es256_store):
        key = es256_store.active_key
        assert key.public_key is not None
        assert key.private_key is not None

    def test_rotate_creates_new_active_key(self, es256_store):
        old_kid = es256_store.active_key.kid
        new_key = es256_store.rotate()
        assert new_key.kid != old_kid
        assert new_key.active is True
        assert es256_store.active_key.kid == new_key.kid

    def test_rotate_keeps_old_key_in_valid_keys(self, es256_store):
        old_kid = es256_store.active_key.kid
        es256_store.rotate()
        valid_kids = [k.kid for k in es256_store.valid_keys()]
        assert old_kid in valid_kids  # Old key still verifiable during overlap

    def test_persistence_survives_reload(self, tmp_path):
        store1 = KeyStore(str(tmp_path / "keys-reload"), algorithm=KeyAlgorithm.ES256)
        store1.initialize()
        kid = store1.active_key.kid

        # Reload from disk
        store2 = KeyStore(str(tmp_path / "keys-reload"), algorithm=KeyAlgorithm.ES256)
        store2.initialize()
        assert store2.get_key(kid) is not None

    def test_rsa_key_generation(self, rs256_store):
        key = rs256_store.active_key
        assert key.algorithm == KeyAlgorithm.RS256
        # RSA keys are larger
        assert key.private_key.key_size == 2048


# ── ES256 sign + verify ────────────────────────────────────────────────────

class TestES256:
    def test_sign_and_verify(self, es256_signer, es256_verifier):
        token = es256_signer.sign(
            subject="task-001",
            audience=["aeos"],
            ttl_seconds=60,
        )
        claims = es256_verifier.verify(token, audience="aeos")
        assert claims.sub == "task-001"
        assert claims.algorithm == "ES256"
        assert not claims.is_expired

    def test_gov_approved_claim(self, es256_signer, es256_verifier):
        token = es256_signer.sign(
            subject="task-gov",
            gov_approved=True,
            worker_id="worker-5",
        )
        claims = es256_verifier.verify(token)
        assert claims.gov_approved is True
        assert claims.worker_id == "worker-5"

    def test_expired_token_raises(self, es256_signer, es256_verifier):
        token = es256_signer.sign(subject="task-exp", ttl_seconds=-1)
        with pytest.raises(TokenExpired):
            es256_verifier.verify(token)

    def test_tampered_payload_rejected(self, es256_signer, es256_verifier):
        import base64, json
        token = es256_signer.sign(subject="task-tamper", ttl_seconds=60)
        header, payload, sig = token.split(".")
        # Modify the payload
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["gov_approved"] = True
        new_payload = base64.urlsafe_b64encode(
            json.dumps(decoded).encode()
        ).rstrip(b"=").decode()
        tampered = f"{header}.{new_payload}.{sig}"
        with pytest.raises(TokenSignatureInvalid):
            es256_verifier.verify(tampered)

    def test_wrong_issuer_rejected(self, es256_store):
        signer = TokenSigner(es256_store, issuer="evil-cluster")
        verifier = TokenVerifier(es256_store, issuer="aeos-test")
        token = signer.sign(subject="t1")
        with pytest.raises(TokenMalformed):
            verifier.verify(token)

    def test_wrong_audience_rejected(self, es256_signer, es256_verifier):
        token = es256_signer.sign(subject="t1", audience=["other-service"])
        with pytest.raises(TokenMalformed):
            es256_verifier.verify(token, audience="aeos")

    def test_revocation(self, es256_signer, es256_verifier):
        token = es256_signer.sign(subject="t-revoke", ttl_seconds=60)
        claims = es256_verifier.verify(token)
        es256_verifier.revoke(claims.jti)
        with pytest.raises(TokenRevoked):
            es256_verifier.verify(token)

    def test_malformed_token_rejected(self, es256_verifier):
        with pytest.raises(TokenMalformed):
            es256_verifier.verify("not.a.jwt.at.all")
        with pytest.raises(TokenMalformed):
            es256_verifier.verify("only.two")

    def test_hmac_algorithm_rejected(self, es256_verifier):
        # Craft a fake token with alg=HS256
        import base64, json
        header = base64.urlsafe_b64encode(
            json.dumps({"alg": "HS256", "kid": "fake", "typ": "JWT"}).encode()
        ).rstrip(b"=").decode()
        payload = base64.urlsafe_b64encode(
            json.dumps({"sub": "x", "iss": "aeos-test", "exp": 9999999999,
                        "iat": 0, "aud": ["aeos"]}).encode()
        ).rstrip(b"=").decode()
        fake_token = f"{header}.{payload}.fakesig"
        with pytest.raises(TokenMalformed):
            es256_verifier.verify(fake_token)


# ── RS256 sign + verify ────────────────────────────────────────────────────

class TestRS256:
    def test_sign_and_verify(self, rs256_signer, rs256_verifier):
        token = rs256_signer.sign(subject="task-rsa", ttl_seconds=30)
        claims = rs256_verifier.verify(token)
        assert claims.sub == "task-rsa"
        assert claims.algorithm == "RS256"

    def test_tampered_payload_rejected(self, rs256_signer, rs256_verifier):
        import base64, json
        token = rs256_signer.sign(subject="task-tamper", ttl_seconds=60)
        header, payload, sig = token.split(".")
        decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
        decoded["sub"] = "escalated"
        new_payload = base64.urlsafe_b64encode(
            json.dumps(decoded).encode()
        ).rstrip(b"=").decode()
        with pytest.raises(TokenSignatureInvalid):
            rs256_verifier.verify(f"{header}.{new_payload}.{sig}")


# ── Key rotation with verification ────────────────────────────────────────

class TestKeyRotationWithVerification:
    def test_tokens_valid_across_rotation(self, es256_store):
        signer = TokenSigner(es256_store, issuer="aeos-test")
        verifier = TokenVerifier(es256_store, issuer="aeos-test")

        # Issue token with current key
        token = signer.sign(subject="pre-rotate", ttl_seconds=60)

        # Rotate
        es256_store.rotate()

        # Old token should STILL verify (overlap window)
        claims = verifier.verify(token)
        assert claims.sub == "pre-rotate"

        # New tokens use new key
        new_token = signer.sign(subject="post-rotate", ttl_seconds=60)
        new_claims = verifier.verify(new_token)
        assert new_claims.sub == "post-rotate"
        assert new_claims.kid != claims.kid  # Different key used

    def test_key_not_found_raises(self, es256_store):
        signer = TokenSigner(es256_store, issuer="aeos-test")
        verifier = TokenVerifier(es256_store, issuer="aeos-test")

        token = signer.sign(subject="t", ttl_seconds=60)

        # Remove all keys from store (simulate retired key)
        es256_store._keys.clear()

        with pytest.raises(TokenKeyNotFound):
            verifier.verify(token)


# ── JWKS serialization ────────────────────────────────────────────────────

class TestJWKS:
    def test_jwks_has_keys(self, es256_store):
        provider = JWKSProvider(es256_store)
        jwks = provider.jwks_dict()
        assert "keys" in jwks
        assert len(jwks["keys"]) >= 1

    def test_es256_jwk_fields(self, es256_store):
        provider = JWKSProvider(es256_store)
        jwks = provider.jwks_dict()
        key = jwks["keys"][0]
        assert key["kty"] == "EC"
        assert key["alg"] == "ES256"
        assert key["use"] == "sig"
        assert "crv" in key
        assert "x" in key
        assert "y" in key
        assert "kid" in key

    def test_rs256_jwk_fields(self, rs256_store):
        provider = JWKSProvider(rs256_store)
        jwks = provider.jwks_dict()
        key = jwks["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert "n" in key
        assert "e" in key

    def test_jwks_includes_all_valid_keys_after_rotation(self, es256_store):
        initial_count = len(JWKSProvider(es256_store).jwks_dict()["keys"])
        es256_store.rotate()
        # After rotation: both old and new key should be in JWKS (overlap)
        after_count = len(JWKSProvider(es256_store).jwks_dict()["keys"])
        assert after_count == initial_count + 1

    def test_jwks_json_is_valid_json(self, es256_store):
        import json
        provider = JWKSProvider(es256_store)
        raw = provider.jwks_json()
        parsed = json.loads(raw)
        assert "keys" in parsed
