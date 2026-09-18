"""Pydantic schemas for document responses and search."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class DocumentResponse(BaseModel):
    id: UUID
    filename: str
    file_type: str
    status: str
    created_at: datetime
    chunk_count: int | None = None
    error_message: str | None = None
    processed_at: datetime | None = None

    model_config = {"from_attributes": True}


class SearchRequest(BaseModel):
    query: str
    top_k: int | None = None


class RetrievalResultSchema(BaseModel):
    chunk_id: UUID
    document_id: UUID
    chunk_index: int
    page_number: int
    content: str
    distance: float
    similarity: float


class SearchResponse(BaseModel):
    document_id: UUID
    query: str
    top_k: int
    count: int
    results: list[RetrievalResultSchema]
