"""
AEOS Certification Harness (Phase 13 Sprint 5).

A reusable framework that measures REAL AEOS operations — task throughput and
latency, Raft leader failover, checkpoint recovery, and federation overhead —
against Bronze/Silver/Gold/Platinum tier thresholds, and decides (via a single
strict gate) whether a run may legitimately claim a certification.

The same entrypoint (`scripts/certify.py <tier>`) runs on a developer
workstation, a Linux server, and Kubernetes. Only the environment
classification and the `--allow-full-scale` opt-in change what scale runs and
whether a certification claim is legitimate — the measurement code is identical
everywhere. On a non-production-grade host, runs are always reported as
dev-scale *operational validations*, never certifications, regardless of the
numbers.

Nothing in this package fabricates a measurement.
"""

from __future__ import annotations

from .environment import Environment, EnvironmentClass, classify
from .profiles import PROFILES, Profile, ScaleSettings, Thresholds, Tier, get_profile
from .report import render_markdown, write_json, write_markdown
from .runner import CertificationHarness, TierRunResult

__all__ = [
    "Environment",
    "EnvironmentClass",
    "classify",
    "PROFILES",
    "Profile",
    "ScaleSettings",
    "Thresholds",
    "Tier",
    "get_profile",
    "CertificationHarness",
    "TierRunResult",
    "render_markdown",
    "write_json",
    "write_markdown",
]
