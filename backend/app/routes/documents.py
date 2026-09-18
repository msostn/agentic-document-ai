"""Document management endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.database import get_db
from app.rag.parser import NoExtractableTextError, PDFParsingError
from app.schemas.document import DocumentResponse
from app.services import document_service, ingestion_service

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post("/upload", response_model=DocumentResponse, status_code=201)
async def upload_document(
    file: UploadFile,
    db: Session = Depends(get_db),
) -> DocumentResponse:
    if not file.filename or not document_service.validate_file_type(
        file.filename, file.content_type
    ):
        raise HTTPException(
            status_code=415,
            detail="Only PDF files are supported.",
        )

    content = await file.read()

    if not document_service.validate_file_size(content):
        limit = document_service.get_upload_limit_mb()
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {limit} MB.",
        )

    sanitized = document_service.sanitize_filename(file.filename)

    doc = document_service.create_document(db, filename=sanitized, file_type="pdf")

    try:
        ingestion_service.ingest_document(db, doc.id, content)
    except PDFParsingError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "document_id": str(doc.id)},
        ) from exc
    except NoExtractableTextError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "document_id": str(doc.id)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Document ingestion failed during embedding or "
                "persistence.",
                "document_id": str(doc.id),
            },
        ) from exc

    return DocumentResponse.model_validate(doc)


@router.post("/{document_id}/ingest", response_model=DocumentResponse)
def ingest_document_route(
    document_id: uuid.UUID,
    file: UploadFile | None = File(None),
    force: bool = Query(False),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    doc = document_service.get_document(db, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    pdf_bytes: bytes | None = None
    if file is not None:
        if not file.filename or not document_service.validate_file_type(
            file.filename, file.content_type
        ):
            raise HTTPException(
                status_code=415,
                detail="Only PDF files are supported.",
            )
        pdf_bytes = file.file.read()
        if not document_service.validate_file_size(pdf_bytes):
            limit = document_service.get_upload_limit_mb()
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds maximum size of {limit} MB.",
            )

    try:
        ingestion_service.ingest_document(db, document_id, pdf_bytes, force=force)
    except ingestion_service.IngestionContentUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PDFParsingError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "document_id": str(document_id)},
        ) from exc
    except NoExtractableTextError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "document_id": str(document_id)},
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Document ingestion failed during embedding or "
                "persistence.",
                "document_id": str(document_id),
            },
        ) from exc

    return DocumentResponse.model_validate(doc)


@router.get("", response_model=list[DocumentResponse])
def list_documents(db: Session = Depends(get_db)) -> list[DocumentResponse]:
    docs = document_service.list_documents(db)
    return [DocumentResponse.model_validate(d) for d in docs]


@router.get("/{document_id}", response_model=DocumentResponse)
def get_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> DocumentResponse:
    doc = document_service.get_document(db, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return DocumentResponse.model_validate(doc)


@router.delete("/{document_id}", status_code=204)
def delete_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> None:
    doc = document_service.get_document(db, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    document_service.delete_document(db, doc)