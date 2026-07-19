"""
app/testing/scale/__init__.py

Scale Certification Platform — P12A.5

Four tiers: Bronze (10w/10k) → Silver (25w/100k) → Gold (50w/1M) → Platinum (100w + chaos).
"""

from .certification import (
    CertificationRunner,
    CertificationResult,
    CertificationTier,
    TierRequirements,
    TIER_REQUIREMENTS,
    LatencyHistogram,
)

__all__ = [
    "CertificationRunner",
    "CertificationResult",
    "CertificationTier",
    "TierRequirements",
    "TIER_REQUIREMENTS",
    "LatencyHistogram",
]
