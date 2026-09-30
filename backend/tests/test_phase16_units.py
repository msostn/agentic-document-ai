"""Phase 16 — production hardening unit tests.

Covers the two hardening guarantees that are cheap to assert in isolation:

* settings validation catches malformed configuration at startup instead of
  at request time (spec section 7.1),
* unhandled exceptions answer with the same JSON ``{"detail": ...}`` contract
  as every other error, correlated with the request ID, and without leaking a
  stack trace (spec section 14.2 / 8.3),
* upstream model payload content never reaches a client-visible error body.
"""

from __future__ import annotations

import logging
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config import Settings
from app.llm.ollama_client import _describe_payload, _parse_chat_response
from app.llm.ollama_client import OllamaResponseError
from app.main import app


def build_settings(**overrides) -> Settings:
    """Build a Settings instance without reading backend/.env or the shell."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------------------
# Settings validation (spec 7.1)
# ---------------------------------------------------------------------------


def test_phase15_baseline_values_are_preserved():
    """Hardening must not move the Phase 15 retrieval baseline."""
    settings = build_settings()
    assert settings.CHUNK_SIZE == 500
    assert settings.CHUNK_OVERLAP == 100
    assert settings.RAG_MIN_SIMILARITY == 0.30
    assert settings.RETRIEVAL_TOP_K_DEFAULT == 5
    assert settings.RETRIEVAL_TOP_K_MAX == 20
    assert settings.AGENT_MAX_ITERATIONS <= 3
    assert settings.AGENT_MAX_TOOL_CALLS <= 3


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"CHUNK_SIZE": 0}, "CHUNK_SIZE"),
        ({"CHUNK_OVERLAP": -1}, "CHUNK_OVERLAP"),
        ({"CHUNK_OVERLAP": 500, "CHUNK_SIZE": 500}, "smaller than CHUNK_SIZE"),
        ({"MIN_CHUNK_SIZE": 0}, "MIN_CHUNK_SIZE"),
        ({"MIN_CHUNK_SIZE": 500, "CHUNK_SIZE": 500}, "smaller than CHUNK_SIZE"),
        ({"MAX_UPLOAD_SIZE_MB": 0}, "MAX_UPLOAD_SIZE_MB"),
        ({"MAX_QUERY_LENGTH": 0}, "MAX_QUERY_LENGTH"),
        ({"EMBEDDING_BATCH_SIZE": 0}, "EMBEDDING_BATCH_SIZE"),
        ({"RETRIEVAL_TOP_K_DEFAULT": 0}, "RETRIEVAL_TOP_K_DEFAULT"),
        ({"RETRIEVAL_TOP_K_DEFAULT": 21, "RETRIEVAL_TOP_K_MAX": 20}, "RETRIEVAL_TOP_K_MAX"),
        ({"RAG_CONTEXT_MAX_CHARS": 0}, "RAG_CONTEXT_MAX_CHARS"),
        ({"RAG_MIN_SIMILARITY": 1.5}, "RAG_MIN_SIMILARITY"),
        ({"OLLAMA_TIMEOUT_SECONDS": 0}, "OLLAMA_TIMEOUT_SECONDS"),
        ({"OLLAMA_NUM_PREDICT": 0}, "OLLAMA_NUM_PREDICT"),
        ({"AGENT_MAX_ITERATIONS": 4}, "AGENT_MAX_ITERATIONS"),
        ({"AGENT_MAX_ITERATIONS": 0}, "AGENT_MAX_ITERATIONS"),
        ({"AGENT_MAX_TOOL_CALLS": 4}, "AGENT_MAX_TOOL_CALLS"),
        ({"AGENT_MAX_TOOL_CALLS": 0}, "AGENT_MAX_TOOL_CALLS"),
        ({"ENVIRONMENT": "   "}, "ENVIRONMENT"),
        ({"LOG_LEVEL": "VERBOSE"}, "LOG_LEVEL"),
        ({"ALLOWED_ORIGINS": "localhost:5173"}, "http(s) origins"),
    ],
)
def test_invalid_settings_are_rejected_at_startup(overrides, expected_fragment):
    with pytest.raises(ValidationError) as excinfo:
        build_settings(**overrides)
    assert expected_fragment in str(excinfo.value)


def test_empty_allowed_origins_disables_cors_instead_of_failing():
    """An empty allowlist is a valid (fully restrictive) configuration."""
    settings = build_settings(ALLOWED_ORIGINS="")
    assert settings.allowed_origins_list == []


def test_valid_settings_still_load_and_split_origins():
    settings = build_settings(
        ALLOWED_ORIGINS="http://localhost:5173, https://docs.example.com"
    )
    assert settings.allowed_origins_list == [
        "http://localhost:5173",
        "https://docs.example.com",
    ]
    assert isinstance(settings.LOG_LEVEL, str)


# ---------------------------------------------------------------------------
# Unhandled exceptions keep the JSON error contract (spec 14.2 / 8.3)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    def _boom() -> None:
        raise RuntimeError("internal detail: SELECT * FROM documents")

    app.add_api_route(
        "/__phase16_boom", _boom, methods=["GET"], include_in_schema=False
    )
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "path", None) != "/__phase16_boom"
        ]


def test_unhandled_exception_returns_json_detail_contract(client):
    response = client.get("/__phase16_boom")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Internal server error."}
    assert "Traceback" not in response.text
    assert "SELECT * FROM documents" not in response.text


def test_unhandled_exception_is_logged_with_request_correlation(
    client, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.main"):
        response = client.get("/__phase16_boom")

    records = [r for r in caplog.records if r.getMessage() == "unhandled_exception"]
    assert records, "the unhandled exception must be logged server-side"
    record = records[-1]
    assert record.exc_info is not None, "the server log must keep the stack trace"
    # The log line and the response must carry the same correlation ID.
    assert record.request_id == response.headers["X-Request-ID"]
    assert UUID(record.request_id).version == 4
    assert record.method == "GET"
    assert record.path == "/__phase16_boom"
    assert record.error_type == "RuntimeError"


def test_unknown_route_returns_json_404(client):
    response = client.get("/__phase16_definitely_missing")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------------------
# Upstream model payload content must not reach error bodies (spec 14.2)
# ---------------------------------------------------------------------------


def test_describe_payload_reports_structure_not_content():
    description = _describe_payload(
        {
            "model": "qwen3:4b",
            "message": {"role": "assistant", "reasoning": "TOP_SECRET_REASONING"},
        }
    )
    assert "TOP_SECRET_REASONING" not in description
    assert "message_keys" in description
    assert "payload_keys" in description


def test_malformed_chat_response_error_excludes_model_content():
    payload = {"model": "qwen3:4b", "response": "TOP_SECRET_MODEL_OUTPUT"}
    with pytest.raises(OllamaResponseError) as excinfo:
        _parse_chat_response(payload)
    message = str(excinfo.value)
    assert "TOP_SECRET_MODEL_OUTPUT" not in message
    assert "payload_keys" in message


def test_describe_payload_handles_non_dict_payloads():
    assert _describe_payload(["not", "a", "dict"]) == "payload_type=list"
    assert "payload_type=str" in _describe_payload("garbage from upstream")
