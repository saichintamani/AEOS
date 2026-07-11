"""
AEOS Structured Logger
Emits JSON logs with consistent fields: timestamp, level, logger, trace_id,
task_id, agent_id, and message. All fields are queryable in CloudWatch / Loki.
"""

import logging
import json
import sys
import uuid
from datetime import datetime, timezone
from contextvars import ContextVar
from typing import Optional

from app.core.config import settings

# ── Context variables (propagated across async call chains) ────────────────────
_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_task_id_var: ContextVar[str] = ContextVar("task_id", default="")
_agent_id_var: ContextVar[str] = ContextVar("agent_id", default="")


def set_trace_context(
    trace_id: Optional[str] = None,
    task_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> None:
    """Inject trace context into the current async context."""
    if trace_id is not None:
        _trace_id_var.set(trace_id)
    if task_id is not None:
        _task_id_var.set(task_id)
    if agent_id is not None:
        _agent_id_var.set(agent_id)


def new_trace_id() -> str:
    """Generate and register a new trace ID for the current context."""
    tid = str(uuid.uuid4())
    _trace_id_var.set(tid)
    return tid


class _JSONFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": _trace_id_var.get() or None,
            "task_id": _task_id_var.get() or None,
            "agent_id": _agent_id_var.get() or None,
            "module": record.module,
            "line": record.lineno,
        }
        # Attach any extra fields passed via `extra={...}`
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value  # strip ctx_ prefix

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


class _HumanFormatter(logging.Formatter):
    """Human-readable format for local development."""

    COLOURS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour = self.COLOURS.get(record.levelname, "")
        trace = _trace_id_var.get()
        trace_str = f" [{trace[:8]}]" if trace else ""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        return (
            f"{colour}{ts} {record.levelname:<8}{self.RESET}"
            f"{trace_str} {record.name}: {record.getMessage()}"
        )


def _build_handler() -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_json:
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(_HumanFormatter())
    return handler


def _configure_root_logger() -> None:
    root = logging.getLogger()
    if root.handlers:
        return  # already configured (e.g., pytest re-runs)
    root.setLevel(settings.log_level.upper())
    root.addHandler(_build_handler())

    # Quiet noisy third-party loggers
    for noisy in ("uvicorn.access", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_root_logger()


def get_logger(name: str) -> logging.Logger:
    """
    Returns a named logger. Preferred usage:
        log = get_logger(__name__)
    """
    return logging.getLogger(name)
