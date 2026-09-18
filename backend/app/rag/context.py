"""Phase 9 RAG context orchestration.

Builds a structured, bounded, source-attributed context object from
Phase 8 retrieval results. Sits between retrieval (Phase 8) and
answer generation (Phase 10). Performs no database writes, no
re-embedding, no LLM calls.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.config import settings
from app.rag.exceptions import (
    DocumentEmptyError,
    DocumentIngestionFailedError,
    DocumentNotFoundError,
    DocumentNotReadyError,
    InvalidQueryError,
)
from app.rag.retriever import RetrievalResult, retrieve_relevant_chunks
from app.schemas.rag import (
    RAGContextChunk,
    RAGContextResult,
    RAGContextStatus,
)


def _validate_query(query: str) -> str:
    """Validate query is a non-empty string."""
    if not isinstance(query, str):
        raise InvalidQueryError(
            f"Query must be a string, got {type(query).__name__}."
        )
    stripped = query.strip()
    if not stripped:
        raise InvalidQueryError("Query cannot be empty or whitespace-only.")
    return stripped


def _validate_threshold(min_similarity: float) -> None:
    """Validate similarity threshold is within bounds."""
    if min_similarity < -1.0 or min_similarity > 1.0:
        raise ValueError(
            f"min_similarity must be between -1.0 and 1.0 "
            f"(got {min_similarity})"
        )


def _validate_max_chars(max_chars: int) -> None:
    """Validate character budget is positive."""
    if max_chars < 1:
        raise ValueError(
            f"max_context_chars must be greater than 0 (got {max_chars})"
        )


def _filter_by_threshold(
    chunks: list[RetrievalResult],
    min_similarity: float,
) -> list[RetrievalResult]:
    """Return chunks that meet or exceed the similarity threshold."""
    return [c for c in chunks if c.similarity >= min_similarity]


def _render_chunk_block(page_number: int, chunk_index: int, content: str) -> str:
    """Render a single chunk block in deterministic format."""
    return f"[Page {page_number} | Chunk {chunk_index}]\n{content}"


def _select_by_budget(
    chunks: list[RetrievalResult],
    max_chars: int,
) -> tuple[list[RetrievalResult], str]:
    """Select chunks that fit within the character budget.

    Returns (included_chunks, context_text). Never truncates a chunk.
    """
    included: list[RetrievalResult] = []
    parts: list[str] = []
    current_length = 0

    for chunk in chunks:
        block = _render_chunk_block(chunk.page_number, chunk.chunk_index, chunk.content)
        block_length = len(block)

        if included:
            separator_length = 2  # "\n\n"
            needed = separator_length + block_length
        else:
            needed = block_length

        if current_length + needed > max_chars:
            break

        included.append(chunk)
        if included and len(included) > 1:
            parts.append("\n\n")
            current_length += 2
        parts.append(block)
        current_length += block_length

    return included, "".join(parts)


def build_rag_context(
    db: Session,
    *,
    document_id: uuid.UUID,
    query: str,
    top_k: int | None = None,
    min_similarity: float | None = None,
    max_context_chars: int | None = None,
) -> RAGContextResult:
    """Build a structured RAG context for a document and query.

    This is the sole public entry point for Phase 9. It orchestrates
    query validation, retrieval (Phase 8), similarity filtering,
    character budget selection, and context formatting.

    Returns a RAGContextResult in all cases — never raises for domain
    outcomes (invalid query, no chunks, below threshold). Only
    infrastructure errors propagate.
    """
    effective_threshold = min_similarity if min_similarity is not None else settings.RAG_MIN_SIMILARITY
    effective_max_chars = max_context_chars if max_context_chars is not None else settings.RAG_CONTEXT_MAX_CHARS

    _validate_threshold(effective_threshold)
    _validate_max_chars(effective_max_chars)

    # Step 0: validate query
    try:
        validated_query = _validate_query(query)
    except InvalidQueryError:
        return RAGContextResult(
            document_id=document_id,
            query=query,
            status=RAGContextStatus.INVALID_QUERY,
            message="Query cannot be empty or whitespace-only.",
            chunks=[],
            context_text=None,
            total_retrieved=0,
            total_included=0,
            total_characters=0,
            similarity_threshold_used=effective_threshold,
            max_chars_used=effective_max_chars,
            top_similarity=None,
        )

    # Step 1: call Phase 8 retrieval
    try:
        retrieval_results = retrieve_relevant_chunks(
            db, document_id, validated_query, top_k=top_k
        )
    except (
        DocumentNotFoundError,
        DocumentNotReadyError,
        DocumentIngestionFailedError,
        DocumentEmptyError,
    ) as exc:
        return RAGContextResult(
            document_id=document_id,
            query=query,
            status=RAGContextStatus.DOCUMENT_NOT_READY,
            message=str(exc),
            chunks=[],
            context_text=None,
            total_retrieved=0,
            total_included=0,
            total_characters=0,
            similarity_threshold_used=effective_threshold,
            max_chars_used=effective_max_chars,
            top_similarity=None,
        )

    # Step 2: check zero chunks
    total_retrieved = len(retrieval_results)
    if total_retrieved == 0:
        return RAGContextResult(
            document_id=document_id,
            query=query,
            status=RAGContextStatus.NO_CHUNKS_RETRIEVED,
            message="Retrieval returned zero chunks for this document.",
            chunks=[],
            context_text=None,
            total_retrieved=0,
            total_included=0,
            total_characters=0,
            similarity_threshold_used=effective_threshold,
            max_chars_used=effective_max_chars,
            top_similarity=None,
        )

    top_similarity = retrieval_results[0].similarity

    # Step 3: filter by similarity threshold
    threshold_passed = _filter_by_threshold(retrieval_results, effective_threshold)

    if not threshold_passed:
        return RAGContextResult(
            document_id=document_id,
            query=query,
            status=RAGContextStatus.BELOW_SIMILARITY_THRESHOLD,
            message="No chunks met the similarity threshold.",
            chunks=[],
            context_text=None,
            total_retrieved=total_retrieved,
            total_included=0,
            total_characters=0,
            similarity_threshold_used=effective_threshold,
            max_chars_used=effective_max_chars,
            top_similarity=top_similarity,
        )

    # Step 4: build RAGContextChunk list for threshold-passed chunks
    rag_chunks: list[RAGContextChunk] = []
    for rank_idx, r in enumerate(threshold_passed):
        # Verify document isolation defensively
        if r.document_id != document_id:
            raise RuntimeError(
                f"Document isolation violated: chunk {r.chunk_id} belongs to "
                f"{r.document_id} but expected {document_id}."
            )
        rag_chunks.append(
            RAGContextChunk(
                chunk_id=r.chunk_id,
                document_id=r.document_id,
                chunk_index=r.chunk_index,
                page_number=r.page_number,
                content=r.content,
                rank=rank_idx + 1,
                distance=r.distance,
                similarity=r.similarity,
                included=False,
            )
        )

    # Step 5: budget selection
    included_retrieval, context_text = _select_by_budget(threshold_passed, effective_max_chars)

    if not included_retrieval:
        return RAGContextResult(
            document_id=document_id,
            query=query,
            status=RAGContextStatus.NO_CHUNK_FITS_BUDGET,
            message="Relevant chunks exist but none fit within the context budget.",
            chunks=rag_chunks,
            context_text=None,
            total_retrieved=total_retrieved,
            total_included=0,
            total_characters=0,
            similarity_threshold_used=effective_threshold,
            max_chars_used=effective_max_chars,
            top_similarity=top_similarity,
        )

    # Mark included chunks
    included_ids = {r.chunk_id for r in included_retrieval}
    for rc in rag_chunks:
        if rc.chunk_id in included_ids:
            rc.included = True

    return RAGContextResult(
        document_id=document_id,
        query=query,
        status=RAGContextStatus.OK,
        message="Context built successfully.",
        chunks=rag_chunks,
        context_text=context_text,
        total_retrieved=total_retrieved,
        total_included=len(included_retrieval),
        total_characters=len(context_text),
        similarity_threshold_used=effective_threshold,
        max_chars_used=effective_max_chars,
        top_similarity=top_similarity,
    )
