"""Phase 13 backend tests: deployment readiness, CORS, logging, health status codes."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_results: list[tuple[str, bool, str]] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    _results.append((name, passed, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


def test_cors_allowed_origins_parsing():
    """ALLOWED_ORIGINS is parsed into a list correctly."""
    from app.config import Settings

    s = Settings(ALLOWED_ORIGINS="http://localhost:5173,http://localhost:3000", DATABASE_URL="postgresql://u:p@h:5432/db")
    record(
        "CORS: single origin parsed",
        s.allowed_origins_list == ["http://localhost:5173", "http://localhost:3000"],
        f"got {s.allowed_origins_list}",
    )

    s2 = Settings(ALLOWED_ORIGINS="http://localhost:5173", DATABASE_URL="postgresql://u:p@h:5432/db")
    record(
        "CORS: single origin",
        s2.allowed_origins_list == ["http://localhost:5173"],
        f"got {s2.allowed_origins_list}",
    )

    s3 = Settings(ALLOWED_ORIGINS=" http://a.com , http://b.com ", DATABASE_URL="postgresql://u:p@h:5432/db")
    record(
        "CORS: whitespace trimmed",
        s3.allowed_origins_list == ["http://a.com", "http://b.com"],
        f"got {s3.allowed_origins_list}",
    )


def test_cors_empty_string():
    """Empty ALLOWED_ORIGINS yields empty list."""
    from app.config import Settings

    s = Settings(ALLOWED_ORIGINS="", DATABASE_URL="postgresql://u:p@h:5432/db")
    record(
        "CORS: empty string yields empty list",
        s.allowed_origins_list == [],
        f"got {s.allowed_origins_list}",
    )


def test_cors_default_value():
    """ALLOWED_ORIGINS defaults to localhost:5173."""
    from app.config import Settings

    s = Settings(DATABASE_URL="postgresql://u:p@h:5432/db")
    record(
        "CORS: default is localhost:5173",
        s.allowed_origins_list == ["http://localhost:5173"],
        f"got {s.allowed_origins_list}",
    )


def test_log_level_setting():
    """LOG_LEVEL setting is available and defaults to INFO."""
    from app.config import settings

    record(
        "LOG_LEVEL setting exists",
        hasattr(settings, "LOG_LEVEL"),
        f"LOG_LEVEL: {getattr(settings, 'LOG_LEVEL', 'MISSING')}",
    )
    record(
        "LOG_LEVEL defaults to INFO",
        settings.LOG_LEVEL == "INFO",
        f"got: {settings.LOG_LEVEL}",
    )


def test_health_returns_200():
    """GET /health always returns 200."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/health")
    record(
        "Health: always returns 200",
        resp.status_code == 200,
        f"got {resp.status_code}",
    )
    body = resp.json()
    record(
        "Health: status ok",
        body.get("status") == "ok",
        f"got {body}",
    )


def test_readiness_returns_200_when_ok():
    """GET /health/ready returns 200 when all deps are available."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/health/ready")
    body = resp.json()
    all_ok = all(v == "ok" for v in body.get("checks", {}).values())
    record(
        "Readiness: returns 200 when all deps OK",
        (resp.status_code == 200) == all_ok,
        f"status={resp.status_code}; all_ok={all_ok}; checks={body.get('checks')}",
    )


def test_readiness_returns_503_when_degraded():
    """GET /health/ready returns 503 when Ollama is unreachable."""
    import httpx as httpx_mod
    from fastapi.testclient import TestClient
    from app.main import app

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            raise httpx_mod.ConnectError("refused")
        def __exit__(self, *a):
            pass

    with patch("httpx.Client", MockClient):
        client = TestClient(app)
        resp = client.get("/health/ready")
        body = resp.json()
        record(
            "Readiness: returns 503 when Ollama down",
            resp.status_code == 503,
            f"got {resp.status_code}",
        )
        record(
            "Readiness: degraded status when Ollama down",
            body.get("status") == "degraded",
            f"got {body.get('status')}",
        )
        record(
            "Readiness: ollama marked unavailable",
            body.get("checks", {}).get("ollama") == "unavailable",
            f"got {body.get('checks', {}).get('ollama')}",
        )


def test_readiness_database_unavailable():
    """Readiness returns 503 when database is unreachable."""
    from fastapi.testclient import TestClient
    from app.main import app

    class FailingConn:
        def __enter__(self):
            raise RuntimeError("DB connection refused")
        def __exit__(self, *a):
            pass

    class FailingEngine:
        def connect(self):
            return FailingConn()

    with patch("app.main.engine", FailingEngine()):
        client = TestClient(app)
        resp = client.get("/health/ready")
        body = resp.json()
        record(
            "Readiness: returns 503 when DB down",
            resp.status_code == 503,
            f"got {resp.status_code}",
        )
        record(
            "Readiness: database marked unavailable",
            body.get("checks", {}).get("database") == "unavailable",
            f"got {body.get('checks', {}).get('database')}",
        )


def test_readiness_has_structure():
    """Readiness response has correct structure."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    resp = client.get("/health/ready")
    body = resp.json()
    record(
        "Readiness: has status field",
        "status" in body,
        f"keys: {list(body.keys())}",
    )
    record(
        "Readiness: has checks field",
        "checks" in body,
        f"keys: {list(body.keys())}",
    )
    record(
        "Readiness: status is ok or degraded",
        body.get("status") in ("ok", "degraded"),
        f"got {body.get('status')}",
    )


def test_environment_default():
    """ENVIRONMENT defaults to development."""
    from app.config import settings

    record(
        "ENVIRONMENT defaults to development",
        settings.ENVIRONMENT == "development",
        f"got: {settings.ENVIRONMENT}",
    )


def main() -> int:
    print("\n=== Phase 13 Backend Tests ===\n")

    test_cors_allowed_origins_parsing()
    test_cors_empty_string()
    test_cors_default_value()
    test_log_level_setting()
    test_health_returns_200()
    test_readiness_returns_200_when_ok()
    test_readiness_returns_503_when_degraded()
    test_readiness_database_unavailable()
    test_readiness_has_structure()
    test_environment_default()

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
