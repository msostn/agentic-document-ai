"""Retrieval-specific exceptions for Phase 8 semantic vector retrieval."""

from __future__ import annotations


class DocumentNotFoundError(Exception):
    """Raised when the requested document_id does not exist."""


class DocumentNotReadyError(Exception):
    """Raised when the document exists but is not in a searchable state."""

    def __init__(self, document_id: object, status: str) -> None:
        self.document_id = document_id
        self.status = status
        super().__init__(
            f"Document {document_id} is not ready for search "
            f"(current status: {status})."
        )


class DocumentIngestionFailedError(Exception):
    """Raised when the document's ingestion has failed."""

    def __init__(self, document_id: object) -> None:
        self.document_id = document_id
        super().__init__(
            f"Document {document_id} ingestion has failed and "
            f"is not searchable."
        )


class DocumentEmptyError(Exception):
    """Raised when the document has no searchable content."""

    def __init__(self, document_id: object) -> None:
        self.document_id = document_id
        super().__init__(
            f"Document {document_id} has no searchable content "
            f"(empty after ingestion)."
        )


class InvalidQueryError(Exception):
    """Raised when the query string is invalid (empty, whitespace, too long)."""


class InvalidTopKError(Exception):
    """Raised when the top_k parameter is out of the allowed range."""
