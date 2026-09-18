"""Phase 11 search_document tool adapter.

Adapts the model-facing search_document tool to the existing Phase 9
``build_rag_context`` function. The model provides only a ``query``;
the backend injects the authoritative ``document_id`` from the URL path.

This module never calls Phase 8 directly. It never re-implements
retrieval, embedding, similarity filtering, or context budgeting.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.rag.context import build_rag_context
from app.schemas.rag import RAGContextResult, RAGContextStatus


# Tool schema in OpenAI-compatible format for Ollama /api/chat
SEARCH_DOCUMENT_TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "search_document",
        "description": (
            "Search the currently selected document for evidence relevant "
            "to a query. Returns ranked, cited excerpts from the document, "
            "or a status indicating why no usable evidence was found. This "
            "is the only source of document knowledge available to you."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "A focused search query describing the specific "
                        "evidence needed."
                    ),
                }
            },
            "required": ["query"],
        },
    },
}


def _build_tool_result_payload(result: RAGContextResult) -> dict[str, Any]:
    """Convert a RAGContextResult into the tool result payload.

    Only included chunks are included in the chunks list.
    context_text is only included when status is OK.
    """
    included_chunks = [
        {
            "chunk_id": str(c.chunk_id),
            "chunk_index": c.chunk_index,
            "page_number": c.page_number,
            "similarity": round(c.similarity, 4),
            "included": c.included,
        }
        for c in result.chunks
        if c.included
    ]

    payload: dict[str, Any] = {
        "status": result.status.value,
        "chunks": included_chunks,
    }

    if result.status == RAGContextStatus.OK and result.context_text:
        payload["context_text"] = result.context_text

    return payload


def execute_search_document(
    db: Session,
    *,
    document_id: uuid.UUID,
    query: str,
) -> tuple[str, list[dict]]:
    """Execute the search_document tool.

    This is the backend-side execution of the model's tool request.
    It calls Phase 9's ``build_rag_context`` with the server-authoritative
    ``document_id`` — the model never controls document scope.

    Args:
        db: Database session.
        document_id: Server-authoritative document ID (from URL path).
        query: The model's search query.

    Returns:
        A tuple of (tool_result_json_string, included_chunks_metadata).
        The JSON string is what gets sent back to the model as a tool
        result message. The chunks metadata is for backend source
        aggregation.
    """
    rag_result = build_rag_context(
        db,
        document_id=document_id,
        query=query,
    )

    payload = _build_tool_result_payload(rag_result)
    tool_result_str = json.dumps(payload, ensure_ascii=False)

    included_chunks_metadata = [
        {
            "chunk_id": c.chunk_id,
            "chunk_index": c.chunk_index,
            "page_number": c.page_number,
            "similarity": c.similarity,
        }
        for c in rag_result.chunks
        if c.included
    ]

    return tool_result_str, included_chunks_metadata
