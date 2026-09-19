"""Phase 12 backend tests: health/ready endpoint and CORS configuration."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    _results.append((name, passed, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def test_health_endpoint():
    """GET /health returns {"status": "ok"}."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/health")
    record("GET /health returns 200", resp.status_code == 200, f"got {resp.status_code}")
    body = resp.json()
    record("GET /health returns status ok", body.get("status") == "ok", f"got {body}")


def test_health_ready_endpoint_exists():
    """GET /health/ready returns a readiness response."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/health/ready")
    record(
        "GET /health/ready returns 200",
        resp.status_code == 200,
        f"got {resp.status_code}",
    )
    body = resp.json()
    record(
        "GET /health/ready has status field",
        "status" in body,
        f"keys: {list(body.keys())}",
    )
    record(
        "GET /health/ready has checks field",
        "checks" in body,
        f"keys: {list(body.keys())}",
    )
    record(
        "GET /health/ready checks has database",
        "database" in body.get("checks", {}),
        f"checks: {body.get('checks', {})}",
    )
    record(
        "GET /health/ready checks has ollama",
        "ollama" in body.get("checks", {}),
        f"checks: {body.get('checks', {})}",
    )


def test_cors_configuration():
    """CORS middleware is configured with proper settings."""
    from app.main import app

    cors_middleware = None
    for middleware in app.user_middleware:
        if hasattr(middleware, "cls") and "CORS" in middleware.cls.__name__:
            cors_middleware = middleware
            break

    record(
        "CORS middleware is configured",
        cors_middleware is not None,
        f"found: {cors_middleware is not None}",
    )

    if cors_middleware:
        kwargs = cors_middleware.kwargs
        record(
            "CORS credentials is False",
            kwargs.get("allow_credentials") is False,
            f"got {kwargs.get('allow_credentials')}",
        )
        origins = kwargs.get("allow_origins", [])
        record(
            "CORS does not use wildcard with credentials",
            "*" not in origins or kwargs.get("allow_credentials") is False,
            f"origins: {origins}, credentials: {kwargs.get('allow_credentials')}",
        )


def test_documents_list_endpoint():
    """GET /documents returns a list."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/documents")
    record(
        "GET /documents returns 200",
        resp.status_code == 200,
        f"got {resp.status_code}",
    )
    body = resp.json()
    record(
        "GET /documents returns a list",
        isinstance(body, list),
        f"type: {type(body).__name__}",
    )


def test_ask_endpoint_exists():
    """POST /documents/{id}/ask exists and validates document."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    import uuid

    fake_id = uuid.uuid4()
    resp = client.post(
        f"/documents/{fake_id}/ask",
        json={"query": "test"},
    )
    record(
        "POST /documents/{id}/ask returns 404 for missing doc",
        resp.status_code == 404,
        f"got {resp.status_code}",
    )


def test_environment_setting():
    """ENVIRONMENT setting is available in config."""
    from app.config import settings

    record(
        "ENVIRONMENT setting exists",
        hasattr(settings, "ENVIRONMENT"),
        f"ENVIRONMENT: {getattr(settings, 'ENVIRONMENT', 'MISSING')}",
    )
    record(
        "ENVIRONMENT defaults to development",
        settings.ENVIRONMENT == "development",
        f"got: {settings.ENVIRONMENT}",
    )


def main() -> int:
    print("\n=== Phase 12 Backend Tests ===\n")

    test_health_endpoint()
    test_health_ready_endpoint_exists()
    test_cors_configuration()
    test_documents_list_endpoint()
    test_ask_endpoint_exists()
    test_environment_setting()

    passed = sum(1 for _, p, _ in _results if p)
    total = len(_results)
    failed = total - passed

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{total} passed, {failed} failed")
    print(f"{'='*50}\n")

    for name, p, detail in _results:
        if not p:
            print(f"  FAILED: {name} — {detail}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
