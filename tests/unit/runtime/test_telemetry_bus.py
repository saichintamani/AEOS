"""Unit tests — TelemetryBus."""

from __future__ import annotations

import asyncio
import pytest

from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType


def _event(et: TelemetryEventType = TelemetryEventType.TASK_STARTED,
           source: str = "test") -> TelemetryEvent:
    return TelemetryEvent(event_type=et, source=source)


class TestTelemetryBus:

    @pytest.mark.asyncio
    async def test_subscribe_and_publish(self):
        bus = TelemetryBus()
        received = []

        async def handler(e: TelemetryEvent):
            received.append(e)

        await bus.subscribe(TelemetryEventType.TASK_STARTED, handler)
        await bus.publish(_event(TelemetryEventType.TASK_STARTED))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_wildcard_subscriber_receives_all(self):
        bus = TelemetryBus()
        received = []

        async def handler(e):
            received.append(e)

        await bus.subscribe(None, handler)
        await bus.publish(_event(TelemetryEventType.TASK_STARTED))
        await bus.publish(_event(TelemetryEventType.TASK_COMPLETED))
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_specific_subscriber_ignores_other_types(self):
        bus = TelemetryBus()
        received = []

        async def handler(e):
            received.append(e)

        await bus.subscribe(TelemetryEventType.TASK_STARTED, handler)
        await bus.publish(_event(TelemetryEventType.TASK_COMPLETED))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        bus = TelemetryBus()
        received = []

        async def handler(e):
            received.append(e)

        sub_id = await bus.subscribe(TelemetryEventType.TASK_STARTED, handler)
        await bus.unsubscribe(sub_id)
        await bus.publish(_event(TelemetryEventType.TASK_STARTED))
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_crash_bus(self):
        bus = TelemetryBus()

        async def bad_handler(e):
            raise RuntimeError("boom")

        async def good_handler(e):
            pass

        await bus.subscribe(TelemetryEventType.TASK_STARTED, bad_handler)
        await bus.subscribe(TelemetryEventType.TASK_STARTED, good_handler)
        # Should not raise
        await bus.publish(_event(TelemetryEventType.TASK_STARTED))

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self):
        bus = TelemetryBus()
        counts = [0, 0, 0]

        for i in range(3):
            idx = i
            async def h(e, i=idx):
                counts[i] += 1
            await bus.subscribe(TelemetryEventType.TASK_COMPLETED, h)

        await bus.publish(_event(TelemetryEventType.TASK_COMPLETED))
        assert counts == [1, 1, 1]

    @pytest.mark.asyncio
    async def test_emit_creates_task(self):
        bus = TelemetryBus()
        received = []

        async def handler(e):
            received.append(e)

        await bus.subscribe(TelemetryEventType.NODE_JOINED, handler)
        task = bus.emit(_event(TelemetryEventType.NODE_JOINED))
        assert isinstance(task, asyncio.Task)
        await task
        assert len(received) == 1
