"""Document management endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.agent.loop import run_agent_loop
from app.config import settings
from app.database import get_db
from app.llm.ollama_client import (
    OllamaConnectionError,
    OllamaError,
    OllamaModelUnavailableError,
    OllamaResponseError,
    OllamaTimeoutError,
    generate,
)
from app.rag.context import build_rag_context
from app.rag.exceptions import (
    DocumentEmptyError,
    DocumentIngestionFailedError,
    DocumentNotFoundError,
    DocumentNotReadyError,
    InvalidQueryError,
    InvalidTopKError,
)
from app.rag.parser import NoExtractableTextError, PDFParsingError
from app.rag.prompt import build_prompts
from app.rag.retriever import retrieve_relevant_chunks
from app.schemas.document import (
    DocumentResponse,
    RetrievalResultSchema,
    SearchRequest,
    SearchResponse,
)
from app.schemas.rag import (
    AnswerResponse,
    AnswerSource,
    AskRequest,
    RAGContextStatus,
)
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


@router.post("/{document_id}/search", response_model=SearchResponse)
def search_document(
    document_id: uuid.UUID,
    request: SearchRequest,
    db: Session = Depends(get_db),
) -> SearchResponse:
    try:
        results = retrieve_relevant_chunks(
            db, document_id, request.query, request.top_k
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DocumentNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentIngestionFailedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DocumentEmptyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except InvalidTopKError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from app.config import settings

    resolved_top_k = (
        request.top_k
        if request.top_k is not None
        else settings.RETRIEVAL_TOP_K_DEFAULT
    )

    return SearchResponse(
        document_id=document_id,
        query=request.query,
        top_k=resolved_top_k,
        count=len(results),
        results=[RetrievalResultSchema.model_validate(r) for r in results],
    )


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


@router.post("/{document_id}/ask", response_model=AnswerResponse)
def ask_document(
    document_id: uuid.UUID,
    request: AskRequest,
    db: Session = Depends(get_db),
) -> AnswerResponse:
    """Answer a question about a document using the Phase 11 agent loop.

    Runs a bounded, tool-using agent that performs mandatory initial
    evidence retrieval, allows model-driven follow-up searches, and
    produces a grounded answer with backend-verified sources.
    """
    # Validate document exists and is accessible (Phase 10 compatibility)
    doc = document_service.get_document(db, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    # Validate query (reuse Phase 9 validation)
    if not isinstance(request.query, str) or not request.query.strip():
        return AnswerResponse(
            document_id=document_id,
            query=request.query,
            answer=None,
            sources=[],
            context_status=RAGContextStatus.INVALID_QUERY.value,
            model=settings.OLLAMA_MODEL,
        )

    # Run the Phase 11 agent loop
    try:
        agent_result = run_agent_loop(
            db, document_id=document_id, query=request.query
        )
    except OllamaConnectionError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ollama service unavailable: {exc}",
        ) from exc
    except OllamaTimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=f"Ollama generation timed out: {exc}",
        ) from exc
    except OllamaModelUnavailableError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc
    except OllamaResponseError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ollama response error: {exc}",
        ) from exc
    except OllamaError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Ollama error: {exc}",
        ) from exc

    # Map context_status for the API response
    context_status = agent_result.context_status

    # Construct sources
    sources = [
        AnswerSource(
            chunk_id=s["chunk_id"],
            chunk_index=s["chunk_index"],
            page_number=s["page_number"],
        )
        for s in agent_result.sources
    ]

    return AnswerResponse(
        document_id=document_id,
        query=request.query,
        answer=agent_result.answer,
        sources=sources,
        context_status=context_status,
        model=settings.OLLAMA_MODEL,
    )