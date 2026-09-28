"""Lightweight timing utility and request correlation IDs.

Uses only Python standard library facilities:
- time.perf_counter for high-resolution timing
- uuid.uuid4 for request ID generation
- threading.local for request-scoped state
"""

from __future__ import annotations

import contextvars
import logging
import time
import threading
import uuid
from contextlib import contextmanager
from typing import Generator, Optional

from app.config import settings

# Request-scoped state lives in context variables rather than a plain
# threading.local: FastAPI runs sync route handlers in a worker thread, and
# contextvars propagate into that thread (anyio copies the current context),
# so the handler sees the same request ID the middleware put in the response
# header. A threading.local would give each thread its own ID and break
# log/header correlation.
_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "phase14_request_id", default=None
)
_context_building_var: contextvars.ContextVar[float] = contextvars.ContextVar(
    "phase14_context_building_ms", default=0.0
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Process-local runtime metrics accumulator
# ---------------------------------------------------------------------------

# Configurable flag (pydantic-settings) plus an in-process override so tests
# can toggle the endpoint without touching the environment.
ENABLE_METRICS_ENDPOINT: bool = True

_METRICS_LOCK = threading.Lock()

# Aggregate counters across all requests (process-local, in-memory)
_runtimeMetrics: dict[str, int | float] = {
    "uptime_seconds": time.perf_counter(),
    "total_requests": 0,
    "total_errors": 0,
    "total_retrieval_ms": 0.0,
    "total_context_building_ms": 0.0,
    "total_generation_ms": 0.0,
    "total_request_ms": 0.0,
}


def is_metrics_enabled() -> bool:
    """Return whether the /metrics/runtime endpoint is enabled."""
    return ENABLE_METRICS_ENDPOINT and settings.ENABLE_METRICS_ENDPOINT


def set_metrics_enabled(enabled: bool) -> None:
    """Enable or disable the /metrics/runtime endpoint."""
    global ENABLE_METRICS_ENDPOINT
    ENABLE_METRICS_ENDPOINT = enabled


def record_request(
    *,
    retrieval_ms: float = 0.0,
    context_building_ms: float = 0.0,
    generation_ms: float = 0.0,
    total_ms: float = 0.0,
    error: bool = False,
) -> None:
    """Record per-request timing metrics into the process-local accumulator.

    This is thread-safe (via threading lock) and should be called once per
    completed request. Zero values are handled correctly (counters still
    increment even if durations are 0.0).
    """
    with _METRICS_LOCK:
        _runtimeMetrics["total_requests"] += 1
        if error:
            _runtimeMetrics["total_errors"] += 1
        _runtimeMetrics["total_retrieval_ms"] += retrieval_ms
        _runtimeMetrics["total_context_building_ms"] += context_building_ms
        _runtimeMetrics["total_generation_ms"] += generation_ms
        _runtimeMetrics["total_request_ms"] += total_ms


def add_stage_metrics(
    *,
    retrieval_ms: float = 0.0,
    context_building_ms: float = 0.0,
    generation_ms: float = 0.0,
) -> None:
    """Accumulate per-stage latencies for a completed query request.

    Does not increment ``total_requests`` — request counting stays in
    ``record_request()`` (called once per request by the HTTP middleware)
    so stage averages are computed against the same request count.
    """
    if retrieval_ms <= 0.0 and context_building_ms <= 0.0 and generation_ms <= 0.0:
        return
    with _METRICS_LOCK:
        _runtimeMetrics["total_retrieval_ms"] += retrieval_ms
        _runtimeMetrics["total_context_building_ms"] += context_building_ms
        _runtimeMetrics["total_generation_ms"] += generation_ms


def get_metrics_snapshot() -> dict[str, int | float]:
    """Return a read-only snapshot of the current runtime metrics.

    The snapshot is a shallow copy so callers can safely inspect it
    without race conditions. All values are inclusive across all requests
    since the process started.

    Zero-request state is handled: if no requests have been recorded,
    all counter/duration fields are 0.0 / 0.
    """
    with _METRICS_LOCK:
        uptime = time.perf_counter() - _runtimeMetrics["uptime_seconds"]
        return {
            "uptime_seconds": round(uptime, 2),
            "total_requests": _runtimeMetrics["total_requests"],
            "total_errors": _runtimeMetrics["total_errors"],
            "total_retrieval_ms": round(_runtimeMetrics["total_retrieval_ms"], 2),
            "total_context_building_ms": round(_runtimeMetrics["total_context_building_ms"], 2),
            "total_generation_ms": round(_runtimeMetrics["total_generation_ms"], 2),
            "total_request_ms": round(_runtimeMetrics["total_request_ms"], 2),
            "average_latency_ms": (
                round(_runtimeMetrics["total_request_ms"] / _runtimeMetrics["total_requests"], 2)
                if _runtimeMetrics["total_requests"] > 0
                else 0.0
            ),
            "average_retrieval_ms": (
                round(_runtimeMetrics["total_retrieval_ms"] / _runtimeMetrics["total_requests"], 2)
                if _runtimeMetrics["total_requests"] > 0
                else 0.0
            ),
            "average_context_building_ms": (
                round(_runtimeMetrics["total_context_building_ms"] / _runtimeMetrics["total_requests"], 2)
                if _runtimeMetrics["total_requests"] > 0
                else 0.0
            ),
            "average_generation_ms": (
                round(_runtimeMetrics["total_generation_ms"] / _runtimeMetrics["total_requests"], 2)
                if _runtimeMetrics["total_requests"] > 0
                else 0.0
            ),
        }


# ---------------------------------------------------------------------------
# Request correlation IDs
# ---------------------------------------------------------------------------

def get_request_id() -> str:
    """Get the current request-scoped correlation ID.

    Returns the ID set for the current context, or generates a new one.
    """
    val = _request_id_var.get()
    if val is not None:
        return val
    # Generate a new one if not set
    new_id = str(uuid.uuid4())
    set_request_id(new_id)
    return new_id


def set_request_id(request_id: str) -> None:
    """Set the request-scoped correlation ID for the current context."""
    _request_id_var.set(request_id)


def clear_request_id() -> None:
    """Clear the request-scoped correlation ID for the current context."""
    _request_id_var.set(None)


def add_context_building_ms(duration_ms: float) -> None:
    """Accumulate context-building time for the current request."""
    _context_building_var.set(_context_building_var.get() + float(duration_ms))


def pop_context_building_ms() -> float:
    """Return (and reset) the context-building time accumulated for this request."""
    value = float(_context_building_var.get())
    _context_building_var.set(0.0)
    return value


# ---------------------------------------------------------------------------
# Structured logging helper
# ---------------------------------------------------------------------------

def log_query_event(
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
    request_id: str | None = None,
    document_id: str | None = None,
    stage: str | None = None,
    duration_ms: float | None = None,
    outcome: str | None = None,
    error_category: str | None = None,
) -> None:
    """Log a query event with safe, parseable metadata only.

    This function ensures that NO sensitive content (question text, answer
    text, document text, chunk text, chain-of-thought, reasoning) is included
    in the log message. Only metadata that is safe to expose is logged.

    Safe metadata included:
    - request_id
    - document_id (if appropriate - scoped to the request)
    - stage (retrieval, context_building, generation, total)
    - duration_ms
    - outcome (success, failure, no_evidence, error)
    - error_category (retrieval_error, generation_timeout, etc.)

    Example usage:

        log_query_event(logger, request_id=request_id, document_id=str(doc.id),
                        stage="retrieval", duration_ms=ms, outcome="success")
    """
    extra: dict[str, object] = {
        "request_id": request_id or "unknown",
        "document_id": document_id,
        "stage": stage,
        "duration_ms": duration_ms,
        "outcome": outcome,
        "error_category": error_category,
    }

    # ENABLE_REQUEST_LOGGING=false silences informational per-stage logs
    # (warnings/errors are always emitted so failures stay observable).
    if not settings.ENABLE_REQUEST_LOGGING and level < logging.WARNING:
        return

    # Filter out None values so they don't appear in log output
    safe_extra = {k: v for k, v in extra.items() if v is not None}

    logger.log(level, "query_event", extra=safe_extra)


# ---------------------------------------------------------------------------
# Log formatter that renders structured extras
# ---------------------------------------------------------------------------

_STANDARD_RECORD_KEYS = frozenset(
    vars(
        logging.LogRecord(
            name="", level=0, pathname="", lineno=0, msg="", args=(), exc_info=None
        )
    )
) | {"message", "asctime", "taskName"}


class KeyValueFormatter(logging.Formatter):
    """Append a record's structured extras to the formatted message.

    ``log_query_event`` attaches metadata via ``extra=...``, which the
    default ``logging.Formatter`` silently drops. This formatter renders the
    extras as ``key=value`` pairs so stage log lines are greppable and
    parseable, as required for structured logging.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_KEYS and not key.startswith("_")
        }
        if not extras:
            return base
        pairs = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        return f"{base} {pairs}"


# ---------------------------------------------------------------------------
# Timing context managers
# ---------------------------------------------------------------------------


class StageTimer:
    """Mutable holder for a stage duration in milliseconds."""

    __slots__ = ("label", "ms")

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.ms = 0.0

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"StageTimer(label={self.label!r}, ms={self.ms:.3f})"


@contextmanager
def timed_block(label: str = "") -> "Generator[StageTimer, None, None]":
    """Context manager that yields elapsed milliseconds.

    Usage:

        with timed_block("retrieval") as timer:
            do_something()
        print(f"{timer.label}: {timer.ms:.1f}ms")

    The timer object is updated when the block exits, including when the
    block raises — the exception still propagates unchanged.
    """
    timer = StageTimer(label)
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.ms = (time.perf_counter() - start) * 1000.0
        logger.debug(
            "stage_timing",
            extra={"stage": label or "unnamed", "duration_ms": timer.ms},
        )


@contextmanager
def request_timings() -> "Generator[request_timings_dict, None, None]":
    """Context manager that provides a timings dict for a request.

    Yields a dict with keys: retrieval_ms, context_building_ms,
    generation_ms, total_ms. Caller updates values as stages complete.

    Usage:

        timings = {}
        with request_timings() as t:
            # ... measure stages, assign t["retrieval_ms"] = ms, etc.
            pass
    """
    timings: request_timings_dict = {
        "retrieval_ms": 0.0,
        "context_building_ms": 0.0,
        "generation_ms": 0.0,
        "total_ms": 0.0,
    }
    start = time.perf_counter()
    try:
        yield timings
    finally:
        end = time.perf_counter()
        timings["total_ms"] = (end - start) * 1000.0


request_timings_dict = dict[str, float]