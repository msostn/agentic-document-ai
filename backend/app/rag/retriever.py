"""Phase 8 semantic vector retrieval service.

Provides document-scoped, pgvector-backed cosine similarity search. The
single public entry point ``retrieve_relevant_chunks()`` embeds a query
via the Phase 6 singleton, executes one pgvector similarity query filtered
by ``document_id``, and returns ranked chunk results — no writes, no
LLM calls, no answer generation.

Retrieval is read-only and performs no database writes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.embeddings import embed_texts
from app.rag.exceptions import (
    DocumentEmptyError,
    DocumentIngestionFailedError,
    DocumentNotFoundError,
    DocumentNotReadyError,
    InvalidQueryError,
    InvalidTopKError,
)

_STATUS_READY = "ready"
_STATUS_PROCESSING = "processing"
_STATUS_FAILED = "failed"
_STATUS_EMPTY = "empty"


@dataclass(frozen=True)
class RetrievalResult:
    """A single chunk result from semantic retrieval.

    - ``distance``: raw pgvector cosine distance (0 = identical, higher = less
      similar).
    - ``similarity``: convenience field, ``1 - distance`` (higher = more
      similar).
    """

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    page_number: int
    content: str
    distance: float
    similarity: float


def _get_ready_document(db: Session, document_id: uuid.UUID) -> Document:
    """Fetch a document by ID and verify it is in READY status.

    Raises appropriate exceptions for missing documents or documents in
    non-searchable states.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(
            f"Document {document_id} does not exist."
        )

    status = document.status
    if status == _STATUS_READY:
        return document

    if status in (_STATUS_PROCESSING,):
        raise DocumentNotReadyError(document_id, status)

    if status == _STATUS_FAILED:
        raise DocumentIngestionFailedError(document_id)

    if status == _STATUS_EMPTY:
        raise DocumentEmptyError(document_id)

    raise DocumentNotReadyError(document_id, status)


def _validate_query(query: str) -> str:
    """Validate and strip the query string.

    Returns the stripped query if valid. Raises ``InvalidQueryError`` for
    empty/whitespace-only strings or strings exceeding the configured
    maximum length.
    """
    if not isinstance(query, str):
        raise InvalidQueryError(
            f"Query must be a string, got {type(query).__name__}."
        )

    stripped = query.strip()
    if not stripped:
        raise InvalidQueryError("Query cannot be empty or whitespace-only.")

    if len(stripped) > settings.MAX_QUERY_LENGTH:
        raise InvalidQueryError(
            f"Query exceeds maximum length of {settings.MAX_QUERY_LENGTH} "
            f"characters (got {len(stripped)})."
        )

    return stripped


def _resolve_top_k(top_k: int | None) -> int:
    """Resolve and validate the top_k parameter.

    Returns the validated integer. Raises ``InvalidTopKError`` for values
    outside the allowed range.
    """
    if top_k is None:
        return settings.RETRIEVAL_TOP_K_DEFAULT

    if not isinstance(top_k, int) or isinstance(top_k, bool):
        raise InvalidTopKError(
            f"top_k must be an integer, got {type(top_k).__name__}."
        )

    if top_k < 1:
        raise InvalidTopKError(
            f"top_k must be at least 1 (got {top_k})."
        )

    if top_k > settings.RETRIEVAL_TOP_K_MAX:
        raise InvalidTopKError(
            f"top_k must be at most {settings.RETRIEVAL_TOP_K_MAX} "
            f"(got {top_k})."
        )

    return top_k


def _embed_query(query: str) -> list[float]:
    """Generate a query embedding using the Phase 6 singleton.

    Returns a 384-dimensional vector as a list of floats. Exactly one
    embedding call per retrieval request.
    """
    return embed_texts(query)  # type: ignore[return-value]


def _run_similarity_query(
    db: Session,
    document_id: uuid.UUID,
    query_embedding: list[float],
    top_k: int,
) -> list[RetrievalResult]:
    """Execute the single pgvector similarity query.

    The query is scoped to ``document_id`` via a WHERE clause and ordered
    by ascending cosine distance. The database performs the vector
    similarity; no chunks are loaded into Python for distance computation.
    """
    distance_expr = DocumentChunk.embedding.cosine_distance(query_embedding)

    rows = (
        db.execute(
            select(
                DocumentChunk,
                distance_expr.label("distance"),
            )
            .filter(DocumentChunk.document_id == document_id)
            .order_by(distance_expr)
            .limit(top_k)
        )
        .all()
    )

    results: list[RetrievalResult] = []
    for row in rows:
        chunk = row[0]
        distance = float(row[1])
        similarity = 1.0 - distance
        results.append(
            RetrievalResult(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                chunk_index=chunk.chunk_index,
                page_number=chunk.page_number,
                content=chunk.content,
                distance=distance,
                similarity=similarity,
            )
        )

    return results


def retrieve_relevant_chunks(
    db: Session,
    document_id: uuid.UUID,
    query: str,
    top_k: int | None = None,
) -> list[RetrievalResult]:
    """Retrieve the top-k most semantically relevant chunks for a query.

    This is the sole public entry point for Phase 8 retrieval. It is
    independent of FastAPI and importable as plain Python/SQLAlchemy.

    The retrieval flow:
        1. Look up document and verify READY status.
        2. Validate the query string.
        3. Resolve and validate top_k.
        4. Embed the query via the Phase 6 singleton (one call).
        5. Execute one pgvector similarity query scoped to document_id.
        6. Return ranked RetrievalResult list.

    Retrieval is read-only — no database writes are performed.
    """
    _get_ready_document(db, document_id)
    validated_query = _validate_query(query)
    resolved_top_k = _resolve_top_k(top_k)
    query_embedding = _embed_query(validated_query)
    return _run_similarity_query(
        db, document_id, query_embedding, resolved_top_k
    )
