"""
app/certification/environment.py

Honest environment capture and classification for the certification harness.

The single most important property of a certification report is that its numbers
are attributed to the environment that actually produced them. A P99 latency
measured on a developer laptop is a real measurement — but it is NOT a
production certification, and the report must say so in machine-readable form so
no downstream consumer can mistake one for the other.

This module captures the host facts and classifies the host into an
``EnvironmentClass``. That class is what the runner uses to decide whether a
tier can be *certified* here at all (see ``profiles.Profile.certifiable_in``),
independent of whether the measured numbers happen to clear the thresholds.

No network calls, no cloud-metadata probing that could hang; classification is
best-effort from local signals and errs toward the weakest claim
(``DEVELOPER_WORKSTATION`` / ``UNKNOWN``) when unsure.
"""

from __future__ import annotations

import os
import platform
import socket
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EnvironmentClass(str, Enum):
    """Where measurements were taken — governs what claims are legitimate."""

    DEVELOPER_WORKSTATION = "developer-workstation"
    LINUX_SERVER = "linux-server"
    KUBERNETES = "kubernetes"
    CLOUD = "cloud"
    CI = "ci"
    UNKNOWN = "unknown"


@dataclass
class Environment:
    """A machine-readable snapshot of where a certification run executed."""

    environment_class: EnvironmentClass
    hostname: str
    os_name: str
    os_release: str
    platform: str
    python_version: str
    cpu_count: int
    logical_cpus: int
    total_memory_mb: float | None
    in_container: bool
    in_kubernetes: bool
    is_ci: bool
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment_class": self.environment_class.value,
            "hostname": self.hostname,
            "os_name": self.os_name,
            "os_release": self.os_release,
            "platform": self.platform,
            "python_version": self.python_version,
            "cpu_count": self.cpu_count,
            "logical_cpus": self.logical_cpus,
            "total_memory_mb": self.total_memory_mb,
            "in_container": self.in_container,
            "in_kubernetes": self.in_kubernetes,
            "is_ci": self.is_ci,
            "classification_signals": self.signals,
        }

    @property
    def is_production_grade(self) -> bool:
        """True only for environment classes where a real certification claim is
        legitimate. A developer workstation, CI runner, or unknown host is NEVER
        production-grade regardless of measured numbers."""
        return self.environment_class in (
            EnvironmentClass.LINUX_SERVER,
            EnvironmentClass.KUBERNETES,
            EnvironmentClass.CLOUD,
        )


def _total_memory_mb() -> float | None:
    """Best-effort physical memory in MB, or None if it cannot be determined
    without third-party deps."""
    # psutil if present (accurate, cross-platform)
    try:
        import psutil  # type: ignore[import]

        return round(psutil.virtual_memory().total / (1024 * 1024), 1)
    except Exception:
        pass
    # POSIX sysconf fallback
    try:
        pages = os.sysconf("SC_PHYS_PAGES")  # type: ignore[attr-defined]
        page_size = os.sysconf("SC_PAGE_SIZE")  # type: ignore[attr-defined]
        return round(pages * page_size / (1024 * 1024), 1)
    except (ValueError, AttributeError, OSError):
        return None


def _detect_kubernetes() -> bool:
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    # The service-account token mount is present in-pod.
    return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount")


def _detect_container() -> bool:
    if os.path.exists("/.dockerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8") as fh:
            content = fh.read()
        return any(marker in content for marker in ("docker", "kubepods", "containerd"))
    except OSError:
        return False


def _detect_ci() -> bool:
    # Honour the common CI env markers without assuming a specific provider.
    return any(
        os.environ.get(var)
        for var in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "JENKINS_URL", "BUILDKITE")
    )


def _detect_cloud() -> bool:
    # Conservative, offline-only hints — we do NOT probe metadata endpoints
    # (they can hang off-cloud). Env markers commonly injected by cloud runtimes.
    return any(
        os.environ.get(var)
        for var in ("AWS_EXECUTION_ENV", "AWS_REGION", "GOOGLE_CLOUD_PROJECT",
                    "AZURE_SUBSCRIPTION_ID", "KUBERNETES_PORT")
    ) and not _detect_ci()


def classify() -> Environment:
    """Capture and classify the current host. Errs toward the weakest claim."""
    signals: list[str] = []
    system = platform.system()  # 'Windows', 'Linux', 'Darwin'
    in_k8s = _detect_kubernetes()
    in_container = _detect_container() or in_k8s
    is_ci = _detect_ci()
    is_cloud = _detect_cloud()

    # Classification precedence (most specific production signal wins), but CI
    # and non-Linux desktop hosts are never promoted to a production class.
    if is_ci:
        env_class = EnvironmentClass.CI
        signals.append("CI env marker present")
    elif in_k8s:
        env_class = EnvironmentClass.KUBERNETES
        signals.append("kubernetes service account / KUBERNETES_SERVICE_HOST present")
    elif is_cloud:
        env_class = EnvironmentClass.CLOUD
        signals.append("cloud runtime env marker present")
    elif system == "Linux" and not in_container:
        # A bare Linux host *could* be a server; we still label it a server
        # class but the runner additionally requires an explicit opt-in flag to
        # emit a certified=True result (see runner gating).
        env_class = EnvironmentClass.LINUX_SERVER
        signals.append("bare-metal/VM Linux host (no container markers)")
    elif system == "Linux" and in_container:
        env_class = EnvironmentClass.LINUX_SERVER
        signals.append("containerised Linux host (non-k8s)")
    elif system in ("Windows", "Darwin"):
        env_class = EnvironmentClass.DEVELOPER_WORKSTATION
        signals.append(f"desktop OS ({system}) — treated as developer workstation")
    else:
        env_class = EnvironmentClass.UNKNOWN
        signals.append(f"unrecognised system {system!r}")

    return Environment(
        environment_class=env_class,
        hostname=socket.gethostname(),
        os_name=system,
        os_release=platform.release(),
        platform=platform.platform(),
        python_version=platform.python_version(),
        cpu_count=os.cpu_count() or 1,
        logical_cpus=os.cpu_count() or 1,
        total_memory_mb=_total_memory_mb(),
        in_container=in_container,
        in_kubernetes=in_k8s,
        is_ci=is_ci,
        signals=signals,
    )
