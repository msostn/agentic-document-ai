"""Document business logic: validation, sanitization, orchestration, DB operations."""

from __future__ import annotations

import re
import uuid
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.document import Document
from app.rag.parser import (
    NoExtractableTextError,
    PDFParsingError,
    extract_text,
)

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\-]")


def sanitize_filename(raw_name: str) -> str:
    """Sanitize a user-supplied filename for safe storage.

    - Strips path components (uses basename only).
    - Replaces unsafe characters with underscores.
    - Enforces a 255-character maximum.
    - Ensures the result ends with .pdf.
    - Falls back to a UUID-based name if sanitization produces nothing usable.
    """
    name = PurePosixPath(raw_name.replace("\\", "/")).name

    name = _SAFE_FILENAME_RE.sub("_", name)

    name = name.strip("._-")

    if not name:
        name = f"document_{uuid.uuid4()}"

    if len(name) > 255:
        name = name[:255]

    if not name.lower().endswith(".pdf"):
        name = f"{name}.pdf"

    return name


def validate_file_type(filename: str, content_type: str | None = None) -> bool:
    """Return True if the file looks like a PDF.

    The filename extension is the primary check. If a Content-Type header is
    present and is unambiguously not PDF-like, reject early. Ambiguous or
    missing Content-Types are allowed through — the authoritative check is
    PyMuPDF opening the file during extraction.
    """
    if not filename.lower().endswith(".pdf"):
        return False

    ct = (content_type or "").strip().lower()
    ambiguous = {"", "application/octet-stream", "binary/octet-stream"}
    if ct in ambiguous or "pdf" in ct:
        return True

    return False


def validate_file_size(content: bytes) -> bool:
    """Return True if the content is within the configured upload size limit."""
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    return len(content) <= max_bytes


def get_upload_limit_mb() -> int:
    """Return the configured max upload size in MB."""
    return settings.MAX_UPLOAD_SIZE_MB


def create_document(db: Session, filename: str, file_type: str) -> Document:
    """Create a Document row with status='processing' and commit immediately."""
    doc = Document(filename=filename, file_type=file_type, status="processing")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def finalize_document(db: Session, document: Document, pdf_bytes: bytes) -> Document:
    """Run extraction and update the document status to ready or failed.

    Every code path resolves the document to a terminal state before returning.
    Parser errors are re-raised (after the row is resolved) so callers can
    distinguish "corrupted PDF" from "no extractable text".
    """
    try:
        extract_text(pdf_bytes)
        document.status = "ready"
    except Exception:
        document.status = "failed"
        raise
    finally:
        db.commit()
        db.refresh(document)

    return document


def list_documents(db: Session) -> list[Document]:
    """Return all documents ordered by created_at descending."""
    result = db.execute(
        select(Document).order_by(Document.created_at.desc())
    )
    return list(result.scalars().all())


def get_document(db: Session, document_id: uuid.UUID) -> Document | None:
    """Return a single document by ID, or None."""
    return db.get(Document, document_id)


def delete_document(db: Session, document: Document) -> None:
    """Delete a document. Chunks cascade at the DB level."""
    db.delete(document)
    db.commit()
