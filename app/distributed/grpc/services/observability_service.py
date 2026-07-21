"""
ObservabilityServiceServicer — trace-span and structured-event ingest + live
event streaming.

Holds an in-memory store of submitted spans and events (bounded ring buffers)
and fans new events out to WatchEvents subscribers. This is a genuine ingest
endpoint — dashboards submit spans/events over the wire and tail the live
stream — not a stub. Retention is intentionally in-memory; a durable exporter
(OTLP/Tempo) is an operational concern layered on top, not part of this proof.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import logging
from collections import deque

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.observability.v1 import observability_pb2 as pb
from aeos.observability.v1 import observability_pb2_grpc as pb_grpc

from ._util import Broadcaster

logger = logging.getLogger(__name__)


class ObservabilityServiceServicer(pb_grpc.ObservabilityServiceServicer):
    def __init__(self, *, max_retained: int = 10_000) -> None:
        self._spans: deque[pb.Span] = deque(maxlen=max_retained)
        self._events: deque[pb.StructuredEvent] = deque(maxlen=max_retained)
        self._stream = Broadcaster()

    @property
    def spans(self) -> list[pb.Span]:
        return list(self._spans)

    @property
    def events(self) -> list[pb.StructuredEvent]:
        return list(self._events)

    async def SubmitSpans(self, request, context):  # noqa: N802
        accepted = 0
        rejected = 0
        for span in request.spans:
            if span.trace_id and span.span_id:
                self._spans.append(span)
                accepted += 1
            else:
                rejected += 1
        return pb.SubmitSpansResponse(accepted=accepted, rejected=rejected)

    async def SubmitEvents(self, request, context):  # noqa: N802
        accepted = 0
        for event in request.events:
            self._events.append(event)
            accepted += 1
            await self._stream.publish(event)
        return pb.SubmitEventsResponse(accepted=accepted)

    async def WatchEvents(self, request, context):  # noqa: N802
        types = set(request.event_types)
        subject_type = request.subject_type

        def _match(ev: pb.StructuredEvent) -> bool:
            if types and ev.event_type not in types:
                return False
            if subject_type and ev.subject_type != subject_type:
                return False
            return True

        # Replay matching history, then stream live.
        for ev in list(self._events):
            if _match(ev):
                yield ev
        q = await self._stream.subscribe()
        try:
            while True:
                ev = await q.get()
                if _match(ev):
                    yield ev
        finally:
            await self._stream.unsubscribe(q)
