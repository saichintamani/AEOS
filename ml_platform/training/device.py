"""
ML Platform — Training Engine: Device Abstraction
==================================================
Abstracts compute hardware from model implementations.
Models call device.to(tensor) without knowing if they're on GPU or CPU.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ml_platform.training.config import DeviceType


@dataclass
class DeviceInfo:
    device_type: str          # "cpu", "cuda", "mps"
    device_index: int         # GPU index (0 for single GPU / CPU)
    device_string: str        # PyTorch device string: "cuda:0", "cpu"
    total_memory_gb: float    # -1 for CPU
    available_memory_gb: float


class DeviceManager:
    """
    Singleton-style manager that resolves and introspects compute devices.

    Usage:
        dm = DeviceManager()
        info = dm.get_device_info(DeviceType.AUTO)
        device = dm.get_torch_device(DeviceType.GPU)
    """

    def get_device_info(self, requested: DeviceType = DeviceType.AUTO) -> DeviceInfo:
        device_str = self._resolve(requested)
        return self._build_info(device_str)

    def get_torch_device(self, requested: DeviceType = DeviceType.AUTO):
        """Returns a torch.device object."""
        try:
            import torch
            return torch.device(self._resolve(requested))
        except ImportError:
            return "cpu"

    def is_gpu_available(self) -> bool:
        try:
            import torch
            return torch.cuda.is_available()
        except ImportError:
            return False

    def gpu_count(self) -> int:
        try:
            import torch
            return torch.cuda.device_count()
        except ImportError:
            return 0

    # ── Internals ──────────────────────────────────────────────────────────────

    def _resolve(self, requested: DeviceType) -> str:
        if requested == DeviceType.AUTO:
            return self._auto()
        if requested == DeviceType.GPU:
            return "cuda:0"
        if requested == DeviceType.MPS:
            return "mps"
        return "cpu"

    def _auto(self) -> str:
        try:
            import torch
            if torch.cuda.is_available():
                return f"cuda:{torch.cuda.current_device()}"
            if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                return "mps"
        except ImportError:
            pass
        return "cpu"

    def _build_info(self, device_str: str) -> DeviceInfo:
        total = -1.0
        avail = -1.0
        if device_str.startswith("cuda"):
            try:
                import torch
                idx = int(device_str.split(":")[-1]) if ":" in device_str else 0
                props = torch.cuda.get_device_properties(idx)
                total = round(props.total_memory / 1024**3, 2)
                avail = round(torch.cuda.mem_get_info(idx)[0] / 1024**3, 2)
            except Exception:
                pass
        return DeviceInfo(
            device_type=device_str.split(":")[0],
            device_index=int(device_str.split(":")[-1]) if ":" in device_str else 0,
            device_string=device_str,
            total_memory_gb=total,
            available_memory_gb=avail,
        )
