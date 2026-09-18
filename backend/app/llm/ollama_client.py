"""Thin Ollama HTTP client for local LLM generation.

Uses the Ollama /api/chat endpoint with stream=false. The client is
provider-specific and knows nothing about PostgreSQL, documents, or
FastAPI routes.

Phase 11 extends this module with ``chat_with_tools`` for native Ollama
tool-calling support. The existing ``generate`` function (Phase 10) is
preserved unchanged.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from typing import Any

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


# ---------------------------------------------------------------------------
# Phase 10: single-shot generation (preserved, unchanged)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Phase 11: tool-calling chat
# ---------------------------------------------------------------------------


@dataclass
class ToolCallFunction:
    """A single tool call extracted from an Ollama assistant message."""

    name: str
    arguments: dict[str, Any]


@dataclass
class ToolCall:
    """Wrapper around a tool call from Ollama."""

    function: ToolCallFunction


@dataclass
class ChatMessage:
    """A parsed message from Ollama /api/chat."""

    role: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ChatResponse:
    """Parsed response from Ollama /api/chat with tool-calling support."""

    message: ChatMessage
    done: bool = True
    model: str = ""


def _parse_tool_call_arguments(raw_arguments: Any) -> dict[str, Any]:
    """Defensively parse tool call arguments from Ollama.

    Ollama may return arguments as a JSON string or as a dict.
    Returns a parsed dict, or raises ValueError for unparseable input.
    """
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if isinstance(raw_arguments, str):
        try:
            parsed = _json.loads(raw_arguments)
        except (_json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                f"Malformed tool call arguments (JSON parse failed): {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"Tool call arguments must be a JSON object, got {type(parsed).__name__}"
            )
        return parsed
    raise ValueError(
        f"Unexpected tool call arguments type: {type(raw_arguments).__name__}"
    )


def _parse_chat_response(data: dict) -> ChatResponse:
    """Parse the Ollama /api/chat JSON response into a ChatResponse.

    Validates the response structure defensively.
    """
    try:
        message_data = data["message"]
    except (KeyError, TypeError) as exc:
        raise OllamaResponseError(
            f"Ollama response missing 'message' field: {data}"
        ) from exc

    if not isinstance(message_data, dict):
        raise OllamaResponseError(
            f"Ollama 'message' is not a dict: {type(message_data).__name__}"
        )

    role = message_data.get("role", "")
    content = message_data.get("content", "")

    if not isinstance(content, str):
        content = str(content) if content is not None else ""

    tool_calls: list[ToolCall] = []
    raw_tool_calls = message_data.get("tool_calls")
    if raw_tool_calls is not None:
        if not isinstance(raw_tool_calls, list):
            raise OllamaResponseError(
                f"Ollama 'tool_calls' is not a list: {type(raw_tool_calls).__name__}"
            )
        for idx, tc in enumerate(raw_tool_calls):
            if not isinstance(tc, dict):
                raise OllamaResponseError(
                    f"Tool call at index {idx} is not a dict: {type(tc).__name__}"
                )
            func_data = tc.get("function")
            if not isinstance(func_data, dict):
                raise OllamaResponseError(
                    f"Tool call at index {idx} missing 'function' dict"
                )
            name = func_data.get("name", "")
            if not isinstance(name, str) or not name:
                raise OllamaResponseError(
                    f"Tool call at index {idx} has empty or non-string 'name'"
                )
            try:
                parsed_args = _parse_tool_call_arguments(func_data.get("arguments", {}))
            except ValueError as exc:
                raise OllamaResponseError(
                    f"Tool call '{name}' has malformed arguments: {exc}"
                ) from exc
            tool_calls.append(
                ToolCall(
                    function=ToolCallFunction(
                        name=name,
                        arguments=parsed_args,
                    )
                )
            )

    return ChatResponse(
        message=ChatMessage(
            role=role,
            content=content,
            tool_calls=tool_calls,
        ),
        done=data.get("done", True),
        model=data.get("model", ""),
    )


def chat_with_tools(
    *,
    messages: list[dict],
    tools: list[dict],
) -> ChatResponse:
    """Send a multi-message chat request with tool-calling support.

    This is the Phase 11 entry point for the agent loop. It sends
    the full message history plus tool schemas to Ollama and returns
    a parsed ChatResponse that may contain tool calls.

    Args:
        messages: Full conversation message list (system, user, assistant,
            tool messages in Ollama format).
        tools: Tool schema list in OpenAI-compatible format.

    Returns:
        A parsed ChatResponse.

    Raises:
        OllamaConnectionError: Ollama cannot be reached.
        OllamaTimeoutError: Request timed out.
        OllamaModelUnavailableError: Configured model not available.
        OllamaResponseError: Malformed or empty response.
    """
    url = f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
    payload = {
        "model": settings.OLLAMA_MODEL,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {
            "temperature": settings.OLLAMA_TEMPERATURE,
            "num_predict": settings.OLLAMA_NUM_PREDICT,
        },
    }

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

    return _parse_chat_response(data)
