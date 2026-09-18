"""Thin Ollama HTTP client for local LLM generation.

Uses the Ollama /api/chat endpoint with stream=false. The client is
provider-specific and knows nothing about PostgreSQL, documents, or
FastAPI routes.
"""

from __future__ import annotations

import httpx

from app.config import settings


class OllamaError(Exception):
    """Base class for Ollama client errors."""


class OllamaConnectionError(OllamaError):
    """Raised when Ollama cannot be reached."""


class OllamaTimeoutError(OllamaError):
    """Raised when a request to Ollama exceeds the configured timeout."""


class OllamaModelUnavailableError(OllamaError):
    """Raised when the configured model is not available on Ollama."""


class OllamaResponseError(OllamaError):
    """Raised when Ollama returns a malformed or empty response."""


def _build_request_payload(
    *,
    system_prompt: str,
    user_prompt: str,
) -> dict:
    """Build the JSON payload for Ollama /api/chat."""
    return {
        "model": settings.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {
            "temperature": settings.OLLAMA_TEMPERATURE,
            "num_predict": settings.OLLAMA_NUM_PREDICT,
        },
    }


def generate(
    *,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """Generate a response from the local Ollama model.

    Args:
        system_prompt: The system-level grounding instructions.
        user_prompt: The user-level prompt containing context and query.

    Returns:
        The generated answer text.

    Raises:
        OllamaConnectionError: Ollama cannot be reached.
        OllamaTimeoutError: Request timed out.
        OllamaModelUnavailableError: Configured model not available.
        OllamaResponseError: Malformed or empty response.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    payload = _build_request_payload(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )

    try:
        with httpx.Client(timeout=settings.OLLAMA_TIMEOUT_SECONDS) as client:
            response = client.post(url, json=payload)
    except httpx.ConnectError as exc:
        raise OllamaConnectionError(
            f"Cannot connect to Ollama at {settings.OLLAMA_BASE_URL}: {exc}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise OllamaTimeoutError(
            f"Ollama request timed out after {settings.OLLAMA_TIMEOUT_SECONDS}s: "
            f"{exc}"
        ) from exc

    if response.status_code == 404:
        raise OllamaModelUnavailableError(
            f"Model '{settings.OLLAMA_MODEL}' is not available on Ollama."
        )

    if response.status_code != 200:
        raise OllamaResponseError(
            f"Ollama returned status {response.status_code}: "
            f"{response.text[:200]}"
        )

    try:
        data = response.json()
    except Exception as exc:
        raise OllamaResponseError(
            f"Ollama returned invalid JSON: {exc}"
        ) from exc

    try:
        message = data["message"]
        text = message["content"]
    except (KeyError, TypeError) as exc:
        raise OllamaResponseError(
            f"Ollama response missing message.content: {data}"
        ) from exc

    if not isinstance(text, str) or not text.strip():
        raise OllamaResponseError(
            "Ollama returned empty or non-string content."
        )

    return text.strip()
