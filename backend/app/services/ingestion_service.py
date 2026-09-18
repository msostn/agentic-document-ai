"""Phase 7 document ingestion orchestration.

Wires together the existing pipeline stages into one persistent flow:

    PDF bytes → parse (Phase 4 parser) → chunk (Phase 5 chunker)
              → embed (Phase 6 embedder, ONE batched call)
              → persist rows in ``document_chunks`` (+ READY status)

This module is the only orchestration layer. It does not reimplement parsing,
chunking or embedding; it calls the existing components in order and persists
the combined result in a single transaction. Transactions:

- Transaction B (the work): delete any existing chunks for the document,
  insert the full new chunk set, set ``status = 'ready'`` (+ ``chunk_count``,
  ``processed_at``), commit once. On any failure the whole transaction is
  rolled back — no partial chunk rows can survive.
- Transaction C (the failure record): only entered when the work transaction
  failed. A fresh, separate transaction that sets ``status = 'failed'`` with
  a stored error message. Kept structurally separate so the FAILED status
  survives the rollback of Transaction B.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.document_chunk import DocumentChunk
from app.rag.chunker import ChunkRecord, chunk_document
from app.rag.embeddings import embed_texts
from app.rag.parser import extract_text

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_EMPTY = "empty"

EMPTY_DOCUMENT_MESSAGE = (
    "No text content could be chunked for this document "
    "(every page is below the minimum chunk size)."
)

_MAX_ERROR_MESSAGE_LENGTH = 500


@dataclass(frozen=True)
class IngestionResult:
    """Result of an ingestion run, suitable for the API layer to surface."""

    status: str
    chunk_count: int
    error_message: str | None = None


class IngestionContentUnavailable(Exception):
    """Raised when ingestion needs PDF bytes but none are available.

    Phase 4 deliberately does not persist uploaded PDF files, so a retry via
    ``POST /documents/{id}/ingest`` must supply the file again. This exception
    is the API surface for that repository reality.
    """


def _short_reason(reason: object) -> str:
    """Turn any exception-like value into a short, displayable string."""
    text = str(reason).strip()
    if not text:
        text = type(reason).__name__
    return text[:_MAX_ERROR_MESSAGE_LENGTH]


def _set_failed(db: Session, document_id: uuid.UUID, reason: str) -> None:
    """Record a FAILED status in its own, separate committed transaction.

    Re-fetches the document inside whatever session state currently exists
    (including right after a rollback) so the failure record is never part of
    a transaction that was rolled back.
    """
    document = db.get(Document, document_id)
    if document is None:
        return
    document.status = STATUS_FAILED
    document.error_message = _short_reason(reason)
    document.processed_at = datetime.now(timezone.utc)
    db.commit()


def _set_empty(db: Session, document_id: uuid.UUID) -> None:
    """Mark a document EMPTY (no chunkable content) in its own transaction."""
    document = db.get(Document, document_id)
    if document is None:
        return
    document.status = STATUS_EMPTY
    document.error_message = EMPTY_DOCUMENT_MESSAGE
    document.chunk_count = 0
    document.processed_at = datetime.now(timezone.utc)
    db.commit()


def build_chunk_rows(
    document_id: uuid.UUID,
    chunks: list[ChunkRecord],
    embeddings: list[list[float]],
) -> list[DocumentChunk]:
    """Pure helper: pair chunks with their embeddings into DocumentChunk rows.

    Preserves the exact ordering produced by the chunker/embedder: row *i*
    carries ``chunks[i]``'s metadata and ``embeddings[i]``. Raises ``ValueError``
    if the two lists have different lengths (a hard failure — must never
    happen with the existing pipeline, but guarded).

    No database access.
    """
    if not isinstance(chunks, list) or not isinstance(embeddings, list):
        raise TypeError("chunks and embeddings must be lists")
    if len(chunks) != len(embeddings):
        raise ValueError(
            f"Chunk/embedding count mismatch: {len(chunks)} chunks but "
            f"{len(embeddings)} embeddings."
        )

    rows: list[DocumentChunk] = []
    for chunk, embedding in zip(chunks, embeddings):
        rows.append(
            DocumentChunk(
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                page_number=chunk.page_number,
                content=chunk.content,
                embedding=list(embedding),
            )
        )
    return rows


def ingest_document(
    db: Session,
    document_id: uuid.UUID,
    pdf_bytes: bytes | None,
    *,
    force: bool = False,
) -> IngestionResult:
    """Run the full ingestion pipeline for one document.

    - Already ``ready`` and ``force=False``: safe no-op.
    - ``force=True`` or not ready: recompute the complete chunk set and
      replace any existing chunks for this document (never append).

    Transitions:
        any        → ``ready``   (chunks > 0, persisted transactionally)
        any        → ``empty``   (parsed, but zero chunks produced)
        any        → ``failed``  (parse/embed/persist error, separate tx)

    Parse-level failures (corrupt PDF / no extractable text) mark the document
    ``failed`` — matching Phase 4's upload contract — and re-raise the parser
    exception so callers can map it to an HTTP error.

    Raises:
        IngestionContentUnavailable: if PDF bytes are required to do work but
            none were provided.
        app.rag.parser.PDFParsingError / NoExtractableTextError: after the
            document has been marked failed.
        Exception: any embedding or persistence failure, after the document
            has been marked failed and the work transaction rolled back.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} does not exist.")

    if document.status == STATUS_READY and not force:
        return IngestionResult(
            status=document.status,
            chunk_count=document.chunk_count or 0,
            error_message=document.error_message,
        )

    if pdf_bytes is None:
        raise IngestionContentUnavailable(
            "No PDF content is available for this document. "
            "Uploaded files are not retained; re-upload the PDF to re-ingest "
            "this document."
        )

    # Phase 4 — parse.
    try:
        extraction = extract_text(pdf_bytes)
    except Exception as exc:
        _set_failed(db, document_id, str(exc))
        raise

    pages = [(page.page_number, page.text) for page in extraction.pages]

    # Phase 5 — chunk.
    chunks = chunk_document(document_id, pages)

    if not chunks:
        _set_empty(db, document_id)
        return IngestionResult(
            status=STATUS_EMPTY,
            chunk_count=0,
            error_message=EMPTY_DOCUMENT_MESSAGE,
        )

    # Phase 6 — embed once, batched, order-preserving.
    texts = [chunk.content for chunk in chunks]
    try:
        embeddings = embed_texts(texts)
    except Exception as exc:
        _set_failed(
            db,
            document_id,
            f"Embedding generation failed: {_short_reason(exc)}",
        )
        raise
    len_check = len(chunks) == len(embeddings)
    if not len_check:
        _set_failed(
            db,
            document_id,
            f"Embedding count mismatch: {len(chunks)} chunks, "
            f"{len(embeddings)} embeddings.",
        )
        raise ValueError(
            f"Chunk/embedding count mismatch: {len(chunks)} chunks but "
            f"{len(embeddings)} embeddings."
        )

    # Phase 7 — build rows in memory, then persist atomically.
    chunk_rows = build_chunk_rows(document_id, chunks, embeddings)

    try:
        db.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document_id)
        )
        db.add_all(chunk_rows)
        document.status = STATUS_READY
        document.chunk_count = len(chunk_rows)
        document.processed_at = datetime.now(timezone.utc)
        document.error_message = None
        db.commit()
    except Exception as exc:
        # Undo the entire work transaction (deletes + inserts + status).
        db.rollback()
        _set_failed(
            db,
            document_id,
            f"Failed to persist document chunks: {_short_reason(exc)}",
        )
        raise

    return IngestionResult(
        status=STATUS_READY,
        chunk_count=len(chunk_rows),
        error_message=None,
    )