"""
AEOS Unit Tests — Message Bus
Tests pub/sub, point-to-point, concurrency, and edge cases.
"""

import asyncio
import pytest
from app.core.message_bus import MessageBus, AgentMessage


def _make_bus() -> MessageBus:
    """Fresh bus instance per test (not the singleton)."""
    return MessageBus()


def _msg(topic: str = "test.topic", sender: str = "test", payload: dict | None = None) -> AgentMessage:
    return AgentMessage(topic=topic, sender_id=sender, payload=payload or {"data": 1}, trace_id="trace-123")


# ── Publish / Subscribe ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscribe_and_publish_round_trip():
    bus = _make_bus()
    received: list[AgentMessage] = []

    async def handler(msg: AgentMessage):
        received.append(msg)

    bus.subscribe("evt.test", handler)
    await bus.publish("evt.test", _msg("evt.test"))
    assert len(received) == 1
    assert received[0].topic == "evt.test"


@pytest.mark.asyncio
async def test_multiple_subscribers_all_receive():
    bus = _make_bus()
    counts = [0, 0]

    async def h1(msg): counts[0] += 1
    async def h2(msg): counts[1] += 1

    bus.subscribe("multi", h1)
    bus.subscribe("multi", h2)
    await bus.publish("multi", _msg("multi"))
    assert counts == [1, 1]


@pytest.mark.asyncio
async def test_unknown_topic_publish_no_crash():
    bus = _make_bus()
    # No subscribers registered — should be a silent no-op
    await bus.publish("nonexistent.topic", _msg("nonexistent.topic"))


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    bus = _make_bus()
    received: list = []

    async def handler(msg): received.append(msg)

    bus.subscribe("unsub", handler)
    bus.unsubscribe("unsub", handler)
    await bus.publish("unsub", _msg("unsub"))
    assert received == []


@pytest.mark.asyncio
async def test_handler_exception_does_not_crash_publisher():
    bus = _make_bus()
    good_received: list = []

    async def bad_handler(msg):
        raise RuntimeError("intentional handler failure")

    async def good_handler(msg):
        good_received.append(msg)

    bus.subscribe("crash", bad_handler)
    bus.subscribe("crash", good_handler)
    # Should not raise even though bad_handler throws
    await bus.publish("crash", _msg("crash"))
    assert len(good_received) == 1


@pytest.mark.asyncio
async def test_message_has_trace_id_and_timestamp():
    msg = _msg(trace_id="abc-123")
    assert msg.trace_id == "abc-123"
    assert msg.timestamp != ""
    assert msg.message_id != ""


@pytest.mark.asyncio
async def test_concurrent_publishes_all_delivered():
    bus = _make_bus()
    received: list = []

    async def handler(msg): received.append(msg)

    bus.subscribe("concurrent", handler)
    await asyncio.gather(*[bus.publish("concurrent", _msg("concurrent")) for _ in range(10)])
    assert len(received) == 10


@pytest.mark.asyncio
async def test_clear_resets_all_subscriptions():
    bus = _make_bus()
    received: list = []

    async def handler(msg): received.append(msg)

    bus.subscribe("before", handler)
    bus.clear()
    await bus.publish("before", _msg("before"))
    assert received == []


# ── Point-to-point request ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_returns_agent_message():
    bus = _make_bus()
    msg = await bus.request(sender_id="agent_a", recipient_id="agent_b", payload={"ping": True}, trace_id="t1")
    assert isinstance(msg, AgentMessage)
    assert msg.sender_id == "agent_a"
    assert "agent_a" in msg.topic and "agent_b" in msg.topic


@pytest.mark.asyncio
async def test_request_handler_called():
    bus = _make_bus()
    received: list = []
    topic = "direct.sender→receiver"

    async def handler(msg): received.append(msg)

    bus.subscribe(topic, handler)
    await bus.request(sender_id="sender", recipient_id="receiver", payload={"x": 1})
    assert len(received) == 1


# ── Introspection ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subscriber_count():
    bus = _make_bus()
    async def h(msg): pass
    bus.subscribe("x", h)
    assert bus.subscriber_count("x") == 1
    assert bus.subscriber_count("nonexistent") == 0


@pytest.mark.asyncio
async def test_summarize_reflects_state():
    bus = _make_bus()
    async def h(msg): pass
    bus.subscribe("topic_a", h)
    bus.subscribe("topic_b", h)
    s = bus.summarize()
    assert "topic_a" in s["topics"]
    assert s["total_handlers"] == 2
