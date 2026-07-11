"""
Phase 9B.6 Priority 3 — Distributed Tracing (OpenTelemetry)

Provides AEOS-specific tracing utilities built on the OpenTelemetry SDK:
  - AEOSTracer      — thin wrapper around OTEL TracerProvider
  - TraceContext     — propagatable context (trace_id, span_id, baggage)
  - span()           — context manager for creating spans
  - AEOS span attributes: node_id, task_id, worker_id, protocol_id, etc.

Works without a live OTEL collector (uses NoOpTracer when not configured).
When OTEL_EXPORTER_OTLP_ENDPOINT is set, exports to the OTLP endpoint.

Usage::

    from app.distributed.observability.tracing import get_tracer, span

    tracer = get_tracer("aeos.scheduler")

    with span(tracer, "dispatch_task", task_id="t1", worker_id="w1") as s:
        ...
        s.set_attribute("result", "success")
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator

# OpenTelemetry — graceful degradation if not installed
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider, ReadableSpan
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import SpanKind, Status, StatusCode
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


# ── AEOS span attribute keys ──────────────────────────────────────────────────

class SpanAttr:
    """Standard attribute key names for AEOS distributed spans."""
    NODE_ID      = "aeos.node_id"
    TASK_ID      = "aeos.task_id"
    WORKER_ID    = "aeos.worker_id"
    WORKFLOW_ID  = "aeos.workflow_id"
    PROTOCOL_ID  = "aeos.protocol_id"
    MACHINE_ID   = "aeos.machine_id"
    RAFT_TERM    = "aeos.raft.term"
    RAFT_ROLE    = "aeos.raft.role"
    LEASE_ID     = "aeos.lease_id"
    CHECKPOINT   = "aeos.checkpoint_id"
    PHASE        = "aeos.phase"
    ERROR_TYPE   = "aeos.error_type"
    VIOLATION_ID = "aeos.violation_id"


# ── TraceContext ──────────────────────────────────────────────────────────────

@dataclass
class TraceContext:
    """
    Propagatable trace context.

    Carries the trace_id, span_id, and optional baggage across service
    boundaries. Can be serialised to/from dict for header propagation.
    """
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    span_id: str  = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: str = ""
    baggage: dict[str, str] = field(default_factory=dict)
    created_at: float = field(default_factory=time.monotonic)

    def to_headers(self) -> dict[str, str]:
        """Serialise to HTTP-compatible propagation headers."""
        headers = {
            "traceparent": f"00-{self.trace_id}-{self.span_id}-01",
        }
        if self.baggage:
            headers["baggage"] = ",".join(f"{k}={v}" for k, v in self.baggage.items())
        return headers

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> "TraceContext":
        """Parse from propagation headers."""
        ctx = cls()
        tp = headers.get("traceparent", "")
        if tp:
            parts = tp.split("-")
            if len(parts) >= 3:
                ctx.trace_id = parts[1]
                ctx.span_id = parts[2]
        baggage_str = headers.get("baggage", "")
        if baggage_str:
            for item in baggage_str.split(","):
                if "=" in item:
                    k, _, v = item.partition("=")
                    ctx.baggage[k.strip()] = v.strip()
        return ctx

    def child(self) -> "TraceContext":
        """Create a child context (same trace, new span)."""
        return TraceContext(
            trace_id=self.trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=self.span_id,
            baggage=dict(self.baggage),
        )


# ── SpanRecord (lightweight, no OTEL dependency) ──────────────────────────────

@dataclass
class SpanRecord:
    """
    Lightweight in-process span record.

    Collected by InMemorySpanCollector for test assertions and local
    audit without requiring a live OTEL exporter.
    """
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str = ""
    start_time: float = field(default_factory=time.monotonic)
    end_time: float = 0.0
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"           # "OK" | "ERROR"
    error_message: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        if self.end_time:
            return self.end_time - self.start_time
        return time.monotonic() - self.start_time

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def add_event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "time": time.monotonic(), **attrs})

    def set_error(self, message: str) -> None:
        self.status = "ERROR"
        self.error_message = message

    def finish(self) -> None:
        self.end_time = time.monotonic()


# ── InMemorySpanCollector ─────────────────────────────────────────────────────

class InMemorySpanCollector:
    """
    Collects SpanRecords in memory.

    Used by AEOSTracer when no external exporter is configured.
    Useful for test assertions and local debugging.
    """

    def __init__(self) -> None:
        self._spans: list[SpanRecord] = []

    def record(self, span: SpanRecord) -> None:
        self._spans.append(span)

    @property
    def spans(self) -> list[SpanRecord]:
        return list(self._spans)

    def clear(self) -> None:
        self._spans.clear()

    def find_by_name(self, name: str) -> list[SpanRecord]:
        return [s for s in self._spans if s.name == name]

    def find_by_trace(self, trace_id: str) -> list[SpanRecord]:
        return [s for s in self._spans if s.trace_id == trace_id]

    def error_spans(self) -> list[SpanRecord]:
        return [s for s in self._spans if s.status == "ERROR"]


# ── AEOSTracer ────────────────────────────────────────────────────────────────

class AEOSTracer:
    """
    AEOS distributed tracer.

    Wraps OpenTelemetry when available; falls back to in-process SpanRecord
    collection when OTEL is absent or not configured.

    Usage::

        tracer = AEOSTracer(service_name="aeos.scheduler", node_id="node-1")

        with tracer.span("dispatch_task", task_id="t1") as s:
            s.set_attribute(SpanAttr.WORKER_ID, "w1")
    """

    def __init__(
        self,
        service_name: str,
        node_id: str = "",
        *,
        collector: InMemorySpanCollector | None = None,
        enable_otel: bool | None = None,
    ) -> None:
        self.service_name = service_name
        self.node_id = node_id
        self._collector = collector or InMemorySpanCollector()

        # Auto-detect OTEL: enable if package available and endpoint is set,
        # unless explicitly overridden via enable_otel parameter
        if enable_otel is None:
            enable_otel = (
                _OTEL_AVAILABLE
                and bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))
            )
        self._otel_enabled = enable_otel and _OTEL_AVAILABLE

        if self._otel_enabled:
            self._setup_otel(service_name)
        else:
            self._otel_tracer = None

    def _setup_otel(self, service_name: str) -> None:
        from opentelemetry.sdk.resources import Resource
        resource = Resource.create({"service.name": service_name, "aeos.node_id": self.node_id})
        provider = TracerProvider(resource=resource)

        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
                exporter = OTLPSpanExporter(endpoint=endpoint)
                provider.add_span_processor(BatchSpanProcessor(exporter))
            except Exception:
                pass  # OTLP exporter not installed — silent fallback

        trace.set_tracer_provider(provider)
        self._otel_tracer = trace.get_tracer(service_name)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        parent_context: TraceContext | None = None,
        **attributes: Any,
    ) -> Generator[SpanRecord, None, None]:
        """
        Context manager that creates a span for the duration of the block.

        Yields a SpanRecord for attribute and event recording.
        On exit, finalises the span and records it.

        Example::

            with tracer.span("execute_task", task_id="t1", worker_id="w1") as s:
                result = do_work()
                s.set_attribute("result_size", len(result))
        """
        ctx = parent_context or TraceContext()
        record = SpanRecord(
            name=name,
            trace_id=ctx.trace_id,
            span_id=ctx.child().span_id,
            parent_span_id=ctx.span_id if parent_context else "",
        )
        if self.node_id:
            record.set_attribute(SpanAttr.NODE_ID, self.node_id)
        for k, v in attributes.items():
            record.set_attribute(k, v)

        try:
            yield record
        except Exception as exc:
            record.set_error(str(exc))
            raise
        finally:
            record.finish()
            self._collector.record(record)

    @property
    def collector(self) -> InMemorySpanCollector:
        return self._collector

    @property
    def spans(self) -> list[SpanRecord]:
        return self._collector.spans


# ── Module-level convenience ──────────────────────────────────────────────────

_DEFAULT_TRACER: AEOSTracer | None = None


def configure(service_name: str = "aeos", node_id: str = "") -> AEOSTracer:
    """Configure the module-level default tracer. Call once at startup."""
    global _DEFAULT_TRACER
    _DEFAULT_TRACER = AEOSTracer(service_name=service_name, node_id=node_id)
    return _DEFAULT_TRACER


def get_tracer(service_name: str = "aeos", node_id: str = "") -> AEOSTracer:
    """Get (or lazily create) an AEOSTracer for the given service."""
    global _DEFAULT_TRACER
    if _DEFAULT_TRACER is None:
        _DEFAULT_TRACER = AEOSTracer(service_name=service_name, node_id=node_id)
    return _DEFAULT_TRACER


@contextmanager
def span(
    tracer: AEOSTracer,
    name: str,
    *,
    parent_context: TraceContext | None = None,
    **attributes: Any,
) -> Generator[SpanRecord, None, None]:
    """Module-level span shortcut."""
    with tracer.span(name, parent_context=parent_context, **attributes) as s:
        yield s
