"""Document management endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

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
    """Answer a question about a document using grounded generation.

    Calls Phase 9 for context, then Ollama for generation. Sources are
    derived exclusively from Phase 9 chunks.
    """
    # Step 1: Build RAG context via Phase 9
    try:
        rag_result = build_rag_context(db, document_id=document_id, query=request.query)
    except (
        DocumentNotFoundError,
        DocumentNotReadyError,
        DocumentIngestionFailedError,
        DocumentEmptyError,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidQueryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Step 2: Check Phase 9 status — only OK proceeds to generation
    if rag_result.status != RAGContextStatus.OK:
        return AnswerResponse(
            document_id=document_id,
            query=request.query,
            answer=None,
            sources=[],
            context_status=rag_result.status.value,
            model=settings.OLLAMA_MODEL,
        )

    # Step 3: Build grounded prompt
    system_prompt, user_prompt = build_prompts(
        context_text=rag_result.context_text or "",
        query=request.query,
    )

    # Step 4: Call Ollama
    try:
        answer_text = generate(system_prompt=system_prompt, user_prompt=user_prompt)
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

    # Step 5: Construct authoritative sources from Phase 9 included chunks
    sources = [
        AnswerSource(
            chunk_id=c.chunk_id,
            chunk_index=c.chunk_index,
            page_number=c.page_number,
        )
        for c in rag_result.chunks
        if c.included
    ]

    return AnswerResponse(
        document_id=document_id,
        query=request.query,
        answer=answer_text,
        sources=sources,
        context_status=rag_result.status.value,
        model=settings.OLLAMA_MODEL,
    )