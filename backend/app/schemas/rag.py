"""Pydantic schemas for RAG context orchestration (Phase 9)."""

from __future__ import annotations

import enum
from uuid import UUID

from pydantic import BaseModel


class RAGContextStatus(str, enum.Enum):
    """Status of a RAG context result."""

    OK = "ok"
    INVALID_QUERY = "invalid_query"
    DOCUMENT_NOT_READY = "document_not_ready"
    NO_CHUNKS_RETRIEVED = "no_chunks_retrieved"
    BELOW_SIMILARITY_THRESHOLD = "below_similarity_threshold"
    NO_CHUNK_FITS_BUDGET = "no_chunk_fits_budget"


class RAGContextChunk(BaseModel):
    """A single chunk in the RAG context result."""

    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    page_number: int
    content: str
    rank: int
    distance: float
    similarity: float
    included: bool


class RAGContextResult(BaseModel):
    """Structured result from RAG context orchestration."""

    document_id: UUID
    query: str
    status: RAGContextStatus
    message: str
    chunks: list[RAGContextChunk]
    context_text: str | None
    total_retrieved: int
    total_included: int
    total_characters: int
    similarity_threshold_used: float
    max_chars_used: int
    top_similarity: float | None


class AskRequest(BaseModel):
    """Request schema for the /ask endpoint."""

    query: str


class AnswerSource(BaseModel):
    """A single authoritative source chunk for an answer."""

    chunk_id: UUID
    chunk_index: int
    page_number: int


class AnswerResponse(BaseModel):
    """Structured response from grounded answer generation (Phase 10)."""

    document_id: UUID
    query: str
    answer: str | None
    sources: list[AnswerSource]
    context_status: str
    model: str
