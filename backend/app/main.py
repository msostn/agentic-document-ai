from __future__ import annotations

import logging
import time
import uuid
from fastapi import FastAPI, Response, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.database import engine
from app.routes import documents
from app.timing import (
    set_request_id,
    clear_request_id,
    get_request_id,
    is_metrics_enabled,
    get_metrics_snapshot,
    pop_context_building_ms,
    record_request,
    KeyValueFormatter,
    log_query_event,
)

logger = logging.getLogger("app.main")

_root_handler = logging.StreamHandler()
_root_handler.setFormatter(
    KeyValueFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    handlers=[_root_handler],
)

app = FastAPI(title="Agentic Document Intelligence")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(documents.router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler for exceptions that no route or middleware handled.

    Every other error response in this API is JSON ``{"detail": ...}``.
    Without this handler Starlette answers an unexpected failure with a
    plain-text body, so the contract was inconsistent for exactly the case
    operators care about most.

    The stack trace is written to the server log (with the request's
    correlation ID) and never returned to the client.
    """
    # The correlation middleware has already cleared its contextvar by the
    # time this runs, so the ID is read from the request scope.
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "unhandled_exception",
        exc_info=exc,
        extra={
            "request_id": request_id or get_request_id(),
            "method": request.method,
            "path": request.url.path,
            "error_type": type(exc).__name__,
        },
    )
    response = JSONResponse(status_code=500, content={"detail": "Internal server error."})
    if request_id:
        response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def add_request_correlation_id(
    request: Request,
    call_next: callable,
) -> Response:
    """Add a request correlation ID and track per-stage timing."""

    # Generate a fresh request ID for each request
    request_id = str(uuid.uuid4())
    set_request_id(request_id)
    # Publish it on the request scope so late handlers (the global
    # Exception handler runs after this middleware has cleared its
    # contextvar) can still correlate with the same ID.
    request.state.request_id = request_id

    # Start total request timing
    start = time.perf_counter()

    response: Response | None = None
    try:
        response = await call_next(request)
    finally:
        end = time.perf_counter()
        total_ms = (end - start) * 1000.0
        record_request(
            total_ms=total_ms,
            error=response is None or response.status_code >= 400,
        )
        if request.url.path.startswith("/documents"):
            log_query_event(
                logger,
                level=logging.INFO,
                request_id=request_id,
                stage="total",
                duration_ms=total_ms,
                outcome=(
                    "success"
                    if response is not None and response.status_code < 400
                    else "error"
                ),
            )
        clear_request_id()
        pop_context_building_ms()

    # Add request ID as header
    response.headers["X-Request-ID"] = request_id

    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def health_ready(response: Response) -> dict[str, object]:
    checks: dict[str, str] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "unavailable"

    import httpx

    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            if resp.status_code == 200:
                checks["ollama"] = "ok"
            else:
                checks["ollama"] = "unavailable"
    except Exception:
        checks["ollama"] = "unavailable"

    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"

    if overall != "ok":
        response.status_code = 503

    return {"status": overall, "checks": checks}


@app.get("/metrics/runtime")
def runtime_metrics() -> dict[str, int | float]:
    """Read-only endpoint returning process-local runtime metrics.

    Only aggregate operational numbers are exposed. No document content,
    chunk text, questions, answers, source metadata, or chunk IDs are
    included in the response.

    The endpoint is disabled when ``ENABLE_METRICS_ENDPOINT`` is ``False``
    (in which case it returns HTTP 503).
    """
    if not is_metrics_enabled():
        raise HTTPException(status_code=503, detail="Metrics endpoint disabled")

    return get_metrics_snapshot()
