"""
AEOS Kernel Package

The HyperKernel is the central runtime coordinator of the AEOS platform.
Every request, resource allocation, plugin lifecycle event, and policy
decision passes through the kernel.

Import surface:
    from app.kernel import AEOSKernel, get_kernel
    from app.kernel.lifecycle import LifecycleState
    from app.kernel.plugin import BasePlugin, PluginManifest
    from app.kernel.exceptions import KernelError, KernelBootError
"""

from app.kernel.exceptions import (
    KernelError,
    KernelBootError,
    KernelStateError,
    PluginConflictError,
    PluginNotFoundError,
    ServiceNotFoundError,
)

__all__ = [
    "KernelError",
    "KernelBootError",
    "KernelStateError",
    "PluginConflictError",
    "PluginNotFoundError",
    "ServiceNotFoundError",
]

# Lazy import to avoid circular deps at import time
def get_kernel():
    from app.kernel.kernel import AEOSKernel
    return AEOSKernel.get_instance()
