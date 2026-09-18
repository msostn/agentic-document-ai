"""Phase 11 bounded agent loop.

Implements a bounded, auditable agent loop that orchestrates:
1. Mandatory initial search (before any model decision)
2. Ollama native tool-calling (model decides to search again or answer)
3. Backend-executed tool logic (search_document via Phase 9)
4. Source aggregation across all tool calls
5. Final answer validation (grounding check)

The loop is bounded by MAX_AGENT_ITERATIONS (Ollama calls) and
MAX_TOOL_CALLS (search_document executions). Both default to 3.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.agent.prompt import build_agent_system_prompt
from app.agent.tools import (
    SEARCH_DOCUMENT_TOOL_SCHEMA,
    execute_search_document,
)
from app.config import settings
from app.llm.ollama_client import (
    ChatResponse,
    OllamaConnectionError,
    OllamaError,
    OllamaModelUnavailableError,
    OllamaResponseError,
    OllamaTimeoutError,
    chat_with_tools,
)
from app.schemas.rag import RAGContextStatus

logger = logging.getLogger(__name__)

NO_EVIDENCE_MESSAGE = (
    "I couldn't find enough information in the document to answer that."
)


@dataclass
class AgentState:
    """Request-scoped mutable state for the agent loop.

    Exists only for the duration of a single /ask request.
    """

    document_id: uuid.UUID
    query: str
    messages: list[dict] = field(default_factory=list)
    tool_call_count: int = 0
    iteration_count: int = 0
    collected_chunks: list[dict] = field(default_factory=list)
    saw_ok_status: bool = False
    last_non_ok_status: str | None = None
    iteration_limit_reached: bool = False


@dataclass
class AgentResult:
    """Final result from the agent loop."""

    answer: str | None
    sources: list[dict]
    context_status: str
    saw_ok_status: bool


def _build_tool_result_message(tool_result_json: str) -> dict:
    """Build an Ollama tool-result message."""
    return {
        "role": "tool",
        "content": tool_result_json,
    }


def _build_assistant_text_response(text: str) -> dict:
    """Build an assistant text-only message (no tool calls)."""
    return {
        "role": "assistant",
        "content": text,
    }


def _build_tool_limit_notice() -> str:
    """Build the message telling the model no more searches are available."""
    return (
        "You have reached the maximum number of allowed searches. "
        "You must now answer using only the evidence you have already "
        "gathered. Do not attempt to call search_document again."
    )


def _aggregate_sources(
    collected_chunks: list[dict],
) -> list[dict]:
    """Deduplicate and aggregate sources from all tool calls.

    Deduplication key: chunk_id. When duplicates differ in similarity,
    keep the one with higher similarity.
    """
    best_by_id: dict[str, dict] = {}
    for chunk in collected_chunks:
        cid = str(chunk["chunk_id"])
        if cid not in best_by_id:
            best_by_id[cid] = chunk
        elif chunk.get("similarity", 0) > best_by_id[cid].get("similarity", 0):
            best_by_id[cid] = chunk

    deduped = list(best_by_id.values())

    deduped.sort(key=lambda c: str(c["chunk_id"]))

    return [
        {
            "chunk_id": c["chunk_id"],
            "chunk_index": c["chunk_index"],
            "page_number": c["page_number"],
        }
        for c in deduped
    ]


def _validate_and_finalize(state: AgentState) -> AgentResult:
    """Validate the agent's final state and produce the result.

    If saw_ok_status is False, overrides any model answer with the
    no-evidence message and empty sources.

    If iteration_limit_reached is True, the model did not produce a
    final answer (its last response was a tool call request). Return
    the no-evidence fallback with accumulated sources.
    """
    sources = _aggregate_sources(state.collected_chunks) if state.saw_ok_status else []

    if not state.saw_ok_status:
        return AgentResult(
            answer=NO_EVIDENCE_MESSAGE,
            sources=[],
            context_status=state.last_non_ok_status or "no_evidence",
            saw_ok_status=False,
        )

    if state.iteration_limit_reached:
        return AgentResult(
            answer=NO_EVIDENCE_MESSAGE,
            sources=sources,
            context_status="ok",
            saw_ok_status=True,
        )

    last_msg = state.messages[-1] if state.messages else {}
    answer_text = last_msg.get("content", "").strip()

    if not answer_text:
        answer_text = NO_EVIDENCE_MESSAGE

    return AgentResult(
        answer=answer_text,
        sources=sources,
        context_status="ok",
        saw_ok_status=True,
    )


def _get_last_status_from_chunks(collected_chunks: list[dict]) -> str | None:
    """Get the last non-OK status from collected chunk metadata."""
    for chunk in reversed(collected_chunks):
        status = chunk.get("_status")
        if status and status != "ok":
            return status
    return None


def _record_search_result(
    state: AgentState,
    tool_result_json: str,
    included_chunks_metadata: list[dict],
    status: str,
) -> None:
    """Record the result of a search into agent state."""
    for chunk_meta in included_chunks_metadata:
        chunk_record = dict(chunk_meta)
        chunk_record["_status"] = status
        state.collected_chunks.append(chunk_record)

    if status == "ok":
        state.saw_ok_status = True
    else:
        state.last_non_ok_status = status


def run_agent_loop(
    db: Session,
    *,
    document_id: uuid.UUID,
    query: str,
) -> AgentResult:
    """Execute the bounded Phase 11 agent loop.

    Flow:
    1. Mandatory first search (before any model decision).
    2. Loop: send messages to Ollama with tool schemas.
       - If model answers (no tool_calls): validate and return.
       - If model requests tool: execute (if within limits), append
         result, continue loop.
       - If limit hit: terminate with controlled response.
    3. Never exceed MAX_AGENT_ITERATIONS Ollama calls or
       MAX_TOOL_CALLS tool executions.

    Args:
        db: Database session.
        document_id: Server-authoritative document ID.
        query: User's original question.

    Returns:
        AgentResult with answer, sources, and context_status.
    """
    state = AgentState(
        document_id=document_id,
        query=query,
    )

    system_prompt = build_agent_system_prompt()
    state.messages.append({"role": "system", "content": system_prompt})
    state.messages.append({"role": "user", "content": query})

    # --- Step 1: Mandatory first search ---
    try:
        tool_result_json, included_meta = execute_search_document(
            db, document_id=document_id, query=query
        )
    except Exception as exc:
        logger.exception("Agent mandatory first search failed")
        raise

    # Parse status from tool result
    try:
        tool_payload = json.loads(tool_result_json)
        first_status = tool_payload.get("status", "error")
    except (json.JSONDecodeError, ValueError):
        first_status = "error"

    state.tool_call_count += 1
    _record_search_result(state, tool_result_json, included_meta, first_status)

    state.messages.append(_build_tool_result_message(tool_result_json))

    # --- Step 1b: Early termination if no evidence from mandatory search ---
    if first_status != "ok":
        return _validate_and_finalize(state)

    # --- Step 2: Agent loop (only entered when mandatory search found evidence) ---
    while state.iteration_count < settings.AGENT_MAX_ITERATIONS:
        state.iteration_count += 1

        try:
            response: ChatResponse = chat_with_tools(
                messages=state.messages,
                tools=[SEARCH_DOCUMENT_TOOL_SCHEMA],
            )
        except (OllamaConnectionError, OllamaTimeoutError, OllamaModelUnavailableError):
            raise
        except OllamaError as exc:
            raise OllamaResponseError(str(exc)) from exc

        # Check if model requested tool calls
        if response.message.tool_calls:
            # Process tool calls
            for tool_call in response.message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = tool_call.function.arguments

                if tool_name != "search_document":
                    # Unknown tool: reject and tell model
                    error_msg = (
                        f"Unknown tool '{tool_name}'. "
                        "Only search_document is available."
                    )
                    state.messages.append(
                        _build_assistant_text_response(response.message.content or "")
                    )
                    state.messages.append(
                        _build_tool_result_message(
                            json.dumps({"status": "error", "error": error_msg})
                        )
                    )
                    continue

                # Extract query from arguments (ignore any document_id)
                model_query = tool_args.get("query", "")
                if not isinstance(model_query, str) or not model_query.strip():
                    error_msg = (
                        "Invalid search_document arguments: "
                        "'query' must be a non-empty string."
                    )
                    state.messages.append(
                        _build_assistant_text_response(response.message.content or "")
                    )
                    state.messages.append(
                        _build_tool_result_message(
                            json.dumps({"status": "error", "error": error_msg})
                        )
                    )
                    continue

                # Check tool call limit
                if state.tool_call_count >= settings.AGENT_MAX_TOOL_CALLS:
                    notice = _build_tool_limit_notice()
                    state.messages.append(
                        _build_assistant_text_response(response.message.content or "")
                    )
                    state.messages.append(
                        _build_tool_result_message(
                            json.dumps({
                                "status": "tool_limit_reached",
                                "message": notice,
                            })
                        )
                    )
                    # Don't increment tool_call_count — no tool was executed
                    continue

                # Execute the tool
                try:
                    tool_result_json, included_meta = execute_search_document(
                        db, document_id=document_id, query=model_query
                    )
                except Exception as exc:
                    logger.exception("Agent tool execution failed")
                    error_msg = f"Tool execution error: {type(exc).__name__}"
                    state.messages.append(
                        _build_assistant_text_response(response.message.content or "")
                    )
                    state.messages.append(
                        _build_tool_result_message(
                            json.dumps({"status": "error", "error": error_msg})
                        )
                    )
                    continue

                try:
                    tool_payload = json.loads(tool_result_json)
                    search_status = tool_payload.get("status", "error")
                except (json.JSONDecodeError, ValueError):
                    search_status = "error"

                state.tool_call_count += 1
                _record_search_result(
                    state, tool_result_json, included_meta, search_status
                )

                state.messages.append(
                    _build_tool_result_message(tool_result_json)
                )

        else:
            # Model produced a final answer (no tool calls)
            state.messages.append(
                _build_assistant_text_response(response.message.content or "")
            )
            return _validate_and_finalize(state)

    # --- Step 3: Iteration limit reached ---
    # ABSOLUTE LIMIT: no more Ollama calls. Use accumulated evidence.
    state.iteration_limit_reached = True
    logger.warning(
        "Agent iteration limit reached: %d iterations, %d tool calls",
        state.iteration_count,
        state.tool_call_count,
    )

    return _validate_and_finalize(state)
