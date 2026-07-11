"""
Wave 9B.4 — Lifecycle Controller

Manages startup and shutdown sequencing for all runtime components.
Ensures clean startup order and graceful shutdown with draining.

LifecycleController — orchestrates start/stop for the full runtime stack.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class LifecycleState(str, Enum):
    IDLE      = "idle"
    STARTING  = "starting"
    RUNNING   = "running"
    STOPPING  = "stopping"
    STOPPED   = "stopped"
    FAILED    = "failed"


@dataclass
class ComponentHandle:
    name: str
    instance: Any
    start_order: int   # lower = starts first / stops last


class LifecycleController:
    """
    Startup / shutdown sequencer.

    Components are registered with a start_order. On start(), they are
    initialized in ascending order. On stop(), reversed order (LIFO).
    """

    def __init__(self) -> None:
        self._components: list[ComponentHandle] = []
        self._state = LifecycleState.IDLE

    @property
    def state(self) -> LifecycleState:
        return self._state

    def register(self, name: str, instance: Any, start_order: int = 50) -> None:
        self._components.append(ComponentHandle(name=name, instance=instance,
                                                start_order=start_order))
        self._components.sort(key=lambda c: c.start_order)

    async def start(self) -> None:
        if self._state not in (LifecycleState.IDLE, LifecycleState.STOPPED):
            logger.warning("LifecycleController: already %s", self._state)
            return
        self._state = LifecycleState.STARTING
        for handle in self._components:
            try:
                if hasattr(handle.instance, "start") and callable(handle.instance.start):
                    coro = handle.instance.start()
                    if asyncio.iscoroutine(coro):
                        await coro
                logger.debug("LifecycleController: started '%s'", handle.name)
            except Exception:
                self._state = LifecycleState.FAILED
                logger.exception("LifecycleController: failed to start '%s'", handle.name)
                raise
        self._state = LifecycleState.RUNNING
        logger.info("LifecycleController: all components started")

    async def stop(self) -> None:
        if self._state != LifecycleState.RUNNING:
            return
        self._state = LifecycleState.STOPPING
        for handle in reversed(self._components):
            try:
                if hasattr(handle.instance, "stop") and callable(handle.instance.stop):
                    coro = handle.instance.stop()
                    if asyncio.iscoroutine(coro):
                        await coro
                logger.debug("LifecycleController: stopped '%s'", handle.name)
            except Exception:
                logger.exception("LifecycleController: error stopping '%s'", handle.name)
        self._state = LifecycleState.STOPPED
        logger.info("LifecycleController: all components stopped")

    def is_running(self) -> bool:
        return self._state == LifecycleState.RUNNING
