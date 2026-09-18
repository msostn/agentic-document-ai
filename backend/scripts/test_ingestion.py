"""Phase 7 ingestion & persistence verification.

Run from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\test_ingestion.py

Exercises the real local embedding model (all-MiniLM-L6-v2) and the real
PostgreSQL + pgvector database, matching the repository's established
standalone-script test convention (Phase 3 init script, Phase 5 chunker,
Phase 6 embeddings). No SQLite, no mocks of the core persistence/embedding
behavior. Created documents are removed from the database as each test runs
and again in a final cleanup pass.

Coverage (Phase 7 spec section 16):
  success path (rows/chunk_count/content/page/index/document_id/embedding
  384-dim/embedding ordering/READY), zero-chunk EMPTY path, scanned-PDF
  failure path, embedding-failure rollback, DB-failure rollback (real-state
  check), retry-after-failure, READY no-op without force, force re-ingestion
  replaces rather than duplicates, upload endpoint, ingest endpoint, and
  Phase 4 CRUD/validation regressions.
"""

import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import pymupdf  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.document_chunk import DocumentChunk  # noqa: E402
from app.rag.chunker import chunk_document  # noqa: E402
from app.rag.embeddings import EmbeddingGenerationError, embed_texts  # noqa: E402
from app.services import document_service  # noqa: E402
from app.services import ingestion_service  # noqa: E402

EMBEDDING_DIMENSION = 384
EMBEDDING_TOLERANCE = 1e-4

results: list[tuple[str, bool, str]] = []
created_document_ids: list[uuid.UUID] = []


def record(name: str, passed: bool, detail: str = "") -> None:
    results.append((name, passed, detail))


def cleanup_document(document_id: uuid.UUID) -> None:
    """Remove a document (chunks cascade) using a fresh session."""
    db = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if doc is not None:
            db.delete(doc)
            db.commit()
    finally:
        db.close()


def register(document_id: uuid.UUID) -> uuid.UUID:
    created_document_ids.append(document_id)
    return document_id


WORD_POOL = [
    "the", "document", "contains", "a", "detailed", "description", "of",
    "the", "insurance", "coverage", "and", "its", "terms", "conditions",
    "for", "all", "eligible", "members", "including", "deductibles",
    "premiums", "benefits", "limits", "exclusions", "as", "well", "as",
    "the", "procedures", "required", "to", "file", "a", "claim", "under",
    "this", "policy", "with", "the", "company", "office", "or", "online",
]


def make_prose(min_chars: int) -> str:
    words: list[str] = []
    total = 0
    i = 0
    size = len(WORD_POOL)
    while total < min_chars:
        for _ in range(11 + (i % 3)):
            word = WORD_POOL[i % size]
            words.append(word)
            total += len(word) + 1
            i += 1
        words[-1] = words[-1] + "."
        total += 1
    return " ".join(words)


def make_pdf(pages: list[str]) -> bytes:
    """Build an in-memory multi-page text PDF (empty strings = blank pages)."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def create_processing_document(filename: str = "phase7_test.pdf") -> Document:
    db = SessionLocal()
    try:
        doc = document_service.create_document(db, filename=filename, file_type="pdf")
    finally:
        db.close()
    return doc


def chunk_rows_for(db, document_id: uuid.UUID) -> list[DocumentChunk]:
    from sqlalchemy import select

    return list(
        db.execute(
            select(DocumentChunk)
            .where(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index.asc())
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Test 1 - successful multi-page ingestion
# ---------------------------------------------------------------------------

def test_01_successful_multi_page_ingestion() -> None:
    pdf_bytes = make_pdf([make_prose(1500), make_prose(1500), make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()
    try:
        result = ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()

        stored = chunk_rows_for(db, doc.id)
        fresh = db.get(Document, doc.id)

        from app.rag.parser import extract_text

        # The independently expected chunker output for the same extracted pages.
        extracted = extract_text(pdf_bytes)
        expected = chunk_document(
            doc.id,
            [(page.page_number, page.text) for page in extracted.pages],
        )

        ok = (
            result.status == "ready"
            and fresh is not None
            and fresh.status == "ready"
            and fresh.chunk_count == len(expected)
            and fresh.processed_at is not None
            and len(stored) == len(expected)
            and all(r.document_id == doc.id for r in stored)
            and [r.chunk_index for r in stored] == list(range(len(stored)))
            and [r.content for r in stored] == [c.content for c in expected]
            and [r.page_number for r in stored] == [c.page_number for c in expected]
            and all(r.embedding is not None for r in stored)
            and all(len(r.embedding) == EMBEDDING_DIMENSION for r in stored)
        )
        record(
            "TEST 1 - Successful multi-page ingestion",
            ok,
            f"{len(stored)} chunks; status={fresh.status if fresh else '?'}; "
            f"chunk_count={fresh.chunk_count if fresh else '?'}; "
            f"processed_at={fresh.processed_at if fresh else '?'}",
        )
        if ok:
            test_01_ordering_detail(stored)
    finally:
        db.close()


def test_01_ordering_detail(stored: list[DocumentChunk]) -> None:
    """Embeddings for chunk i must correspond to chunk i's content (not shuffled)."""
    max_deviation = 0.0
    for row in stored:
        individual = embed_texts(row.content)
        for a, b in zip(individual, row.embedding):
            max_deviation = max(max_deviation, abs(a - b))
    ok = max_deviation < EMBEDDING_TOLERANCE
    record(
        "TEST 1b - Embedding ordering corresponds to chunk ordering",
        ok,
        f"max abs deviation {max_deviation:.2e} vs individually embedded content",
    )


# ---------------------------------------------------------------------------
# Test 2 - parseable PDF that chunks to zero -> EMPTY
# ---------------------------------------------------------------------------

def test_02_zero_chunk_document_is_empty() -> None:
    # Two pages, each under MIN_CHUNK_SIZE (100 chars), combined over the
    # parser's MIN_EXTRACTABLE_CHARS threshold (50) so parsing succeeds.
    pdf_bytes = make_pdf(["Annual fee one hundred dollars.", "Coverage begins next month."])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()
    try:
        result = ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
        stored = chunk_rows_for(db, doc.id)
        fresh = db.get(Document, doc.id)

        ok = (
            result.status == "empty"
            and result.chunk_count == 0
            and result.error_message == ingestion_service.EMPTY_DOCUMENT_MESSAGE
            and fresh is not None
            and fresh.status == "empty"
            and fresh.chunk_count == 0
            and stored == []
        )
        record(
            "TEST 2 - Zero-chunk (parseable) document marked empty",
            ok,
            f"status={fresh.status if fresh else '?'}; "
            f"chunk_count={fresh.chunk_count if fresh else '?'}; "
            f"chunk rows={len(stored)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 3 - scanned/no-text PDF -> failed (Phase 4 contract preserved)
# ---------------------------------------------------------------------------

def test_03_no_text_pdf_is_failed() -> None:
    pdf_bytes = make_pdf([""])  # blank page, zero extractable text
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()
    try:
        raised = None
        try:
            ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        except Exception as exc:
            raised = exc
        db.expire_all()
        stored = chunk_rows_for(db, doc.id)
        fresh = db.get(Document, doc.id)

        from app.rag.parser import NoExtractableTextError

        ok = (
            isinstance(raised, NoExtractableTextError)
            and fresh is not None
            and fresh.status == "failed"
            and fresh.error_message is not None
            and len(fresh.error_message) > 0
            and stored == []
        )
        record(
            "TEST 3 - Scanned/no-text PDF -> failed, zero chunks, 422-shaped error",
            ok,
            f"raised={type(raised).__name__ if raised else None}; "
            f"status={(fresh.status if fresh else '?')!r}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 4 - embedding failure -> zero chunks persisted, status failed
# ---------------------------------------------------------------------------

def test_04_embedding_failure_leaves_no_chunks() -> None:
    pdf_bytes = make_pdf([make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()

    original_embed = ingestion_service.embed_texts

    def raising_embed(texts):
        raise EmbeddingGenerationError("simulated embedding failure")

    ingestion_service.embed_texts = raising_embed
    try:
        raised = None
        try:
            ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        except Exception as exc:
            raised = exc
    finally:
        ingestion_service.embed_texts = original_embed

    db.expire_all()
    stored = chunk_rows_for(db, doc.id)
    fresh = db.get(Document, doc.id)

    ok = (
        isinstance(raised, EmbeddingGenerationError)
        and stored == []
        and fresh is not None
        and fresh.status == "failed"
        and fresh.error_message is not None
        and fresh.error_message.startswith("Embedding generation failed:")
    )
    record(
        "TEST 4 - Embedding failure: zero persisted chunks, status failed",
        ok,
        f"raised={type(raised).__name__ if raised else None}; "
        f"chunk rows={len(stored)}; "
        f"status={(fresh.status if fresh else '?')!r}",
    )
    db.close()


# ---------------------------------------------------------------------------
# Test 5 - DB failure rolls back chunk persistence (real-state verification)
# ---------------------------------------------------------------------------

def test_05_db_failure_rolls_back() -> None:
    pdf_bytes = make_pdf([make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()

    original_commit = db.commit
    state = {"failed": False}

    def flaky_commit(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("simulated DB commit failure")
        return original_commit(*args, **kwargs)

    db.commit = flaky_commit
    raised = None
    try:
        try:
            ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        except Exception as exc:
            raised = exc
    finally:
        db.commit = original_commit

    verify = SessionLocal()
    try:
        stored = chunk_rows_for(verify, doc.id)
        fresh = verify.get(Document, doc.id)
    finally:
        verify.close()
    db.close()

    ok = (
        isinstance(raised, RuntimeError)
        and stored == []
        and fresh is not None
        and fresh.status == "failed"
        and fresh.error_message is not None
        and fresh.error_message.startswith("Failed to persist document chunks:")
    )
    record(
        "TEST 5 - DB commit failure: rolled back, zero chunk rows, status failed",
        ok,
        f"raised={type(raised).__name__ if raised else None}; "
        f"chunk rows={len(stored)} (real DB check); "
        f"status={(fresh.status if fresh else '?')!r}",
    )


def test_05b_db_failure_on_reingest_keeps_old_chunk_set() -> None:
    """A failed force re-run must not half-apply the delete/new-insert set."""
    pdf_bytes = make_pdf([make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()
    try:
        ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
    finally:
        db.close()

    db = SessionLocal()
    original_commit = db.commit
    state = {"failed": False}

    def flaky_commit(*args, **kwargs):
        if not state["failed"]:
            state["failed"] = True
            raise RuntimeError("simulated DB commit failure")
        return original_commit(*args, **kwargs)

    db.commit = flaky_commit
    raised = None
    try:
        try:
            ingestion_service.ingest_document(db, doc.id, pdf_bytes, force=True)
        except Exception as exc:
            raised = exc
    finally:
        db.commit = original_commit

    verify = SessionLocal()
    try:
        stored = chunk_rows_for(verify, doc.id)
        fresh = verify.get(Document, doc.id)
    finally:
        verify.close()
    db.close()

    # The delete of the old set happened inside the rolled-back transaction, so
    # the previous complete chunk set must still be intact (no partial rows).
    ok = (
        isinstance(raised, RuntimeError)
        and len(stored) > 0
        and [r.chunk_index for r in stored] == list(range(len(stored)))
        and fresh is not None
        and fresh.status == "failed"
    )
    record(
        "TEST 5b - DB failure during force re-ingest preserves old set",
        ok,
        f"raised={type(raised).__name__ if raised else None}; "
        f"old chunk rows still present={len(stored)}",
    )


# ---------------------------------------------------------------------------
# Test 6 - retry after failure succeeds
# ---------------------------------------------------------------------------

def test_06_retry_after_failure_succeeds() -> None:
    pdf_bytes = make_pdf([make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()

    original_embed = ingestion_service.embed_texts

    def raising_embed(texts):
        raise EmbeddingGenerationError("simulated first-run failure")

    ingestion_service.embed_texts = raising_embed
    try:
        try:
            ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        except EmbeddingGenerationError:
            pass
    finally:
        ingestion_service.embed_texts = original_embed
    db.expire_all()
    before = db.get(Document, doc.id)
    assert before is not None and before.status == "failed"

    # Retry with the real embedder.
    try:
        result = ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
        stored = chunk_rows_for(db, doc.id)
        fresh = db.get(Document, doc.id)

        ok = (
            result.status == "ready"
            and fresh is not None
            and fresh.status == "ready"
            and fresh.chunk_count == len(stored)
            and len(stored) > 0
            and [r.chunk_index for r in stored] == list(range(len(stored)))
        )
        record(
            "TEST 6 - Retry after failure succeeds, no duplicate/orphan rows",
            ok,
            f"status={(fresh.status if fresh else '?')!r}; "
            f"chunk rows={len(stored)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 7 - READY + force=False is a no-op
# ---------------------------------------------------------------------------

def test_07_ready_noop_without_force() -> None:
    pdf_bytes = make_pdf([make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()
    try:
        ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
        first_count = len(chunk_rows_for(db, doc.id))

        result = ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
        second_count = len(chunk_rows_for(db, doc.id))
        fresh = db.get(Document, doc.id)

        ok = (
            result.status == "ready"
            and result.chunk_count == first_count
            and first_count > 0
            and second_count == first_count
            and fresh is not None
            and fresh.status == "ready"
        )
        record(
            "TEST 7 - READY + force=False is a safe no-op",
            ok,
            f"chunk rows before={first_count}, after={second_count}",
        )
    finally:
        db.close()


def test_07b_ready_noop_without_force_endpoint() -> None:
    pdf_bytes = make_pdf([make_prose(1500)])
    db = SessionLocal()
    try:
        doc = document_service.create_document(
            db, filename="noop.pdf", file_type="pdf"
        )
    finally:
        db.close()
    register(doc.id)

    db = SessionLocal()
    try:
        ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
        first_count = len(chunk_rows_for(db, doc.id))
    finally:
        db.close()

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    response = client.post(f"/documents/{doc.id}/ingest")
    db = SessionLocal()
    try:
        second_count = len(chunk_rows_for(db, doc.id))
        fresh = db.get(Document, doc.id)
    finally:
        db.close()

    ok = (
        response.status_code == 200
        and response.json()["status"] == "ready"
        and second_count == first_count
        and fresh is not None
        and fresh.status == "ready"
    )
    record(
        "TEST 7b - Ingest endpoint on READY doc (no force) is a no-op",
        ok,
        f"http={response.status_code}; chunk rows before={first_count}, "
        f"after={second_count}",
    )


# ---------------------------------------------------------------------------
# Test 8 - force=True replaces, does not duplicate
# ---------------------------------------------------------------------------

def test_08_force_reingestion_replaces_chunk_set() -> None:
    pdf_bytes = make_pdf([make_prose(1500)])
    doc = create_processing_document()
    register(doc.id)
    db = SessionLocal()
    try:
        ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        db.expire_all()
        first_ids = sorted(r.id for r in chunk_rows_for(db, doc.id))
        first_count = len(first_ids)

        result = ingestion_service.ingest_document(db, doc.id, pdf_bytes, force=True)
        db.expire_all()
        stored = chunk_rows_for(db, doc.id)
        second_ids = sorted(r.id for r in stored)
        second_count = len(second_ids)
        fresh = db.get(Document, doc.id)

        ok = (
            result.status == "ready"
            and second_count == first_count
            and len({(r.document_id, r.chunk_index) for r in stored})
            == second_count
            and [r.chunk_index for r in stored] == list(range(second_count))
            and fresh is not None
            and fresh.chunk_count == second_count
        )
        record(
            "TEST 8 - force=True replaces the chunk set (no duplicates)",
            ok,
            f"row ids before={first_count}, after={second_count}; "
            f"new ids={set(first_ids) != set(second_ids)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 9/10 - upload endpoint (success + scanned 422) - Phase 4 contract
# ---------------------------------------------------------------------------

def test_09_upload_endpoint_success() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pdf_bytes = make_pdf([make_prose(1500), make_prose(1500)])
    response = client.post(
        "/documents/upload",
        files={"file": ("customer policy v2.pdf", pdf_bytes, "application/pdf")},
    )
    body = response.json()
    doc_id = body.get("id")
    if doc_id:
        register(doc_id)

    ok = (
        response.status_code == 201
        and body["status"] == "ready"
        and body["file_type"] == "pdf"
        and body["filename"] == "customer_policy_v2.pdf"
        and body.get("chunk_count", 0) > 0
        and body.get("processed_at") is not None
        and body.get("error_message") is None
    )
    record(
        "TEST 9 - Upload endpoint returns READY document with chunk_count",
        ok,
        f"http={response.status_code}; status={body.get('status')!r}; "
        f"chunk_count={body.get('chunk_count')}",
    )


def test_10_upload_endpoint_scanned_pdf_422() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pdf_bytes = make_pdf([""])
    response = client.post(
        "/documents/upload",
        files={"file": ("scan.pdf", pdf_bytes, "application/pdf")},
    )
    body = response.json()
    doc_id = (body.get("detail") or {}).get("document_id")
    if doc_id:
        register(doc_id)

    db = SessionLocal()
    try:
        fresh = db.get(Document, uuid.UUID(doc_id)) if doc_id else None
        row_ok = fresh is not None and fresh.status == "failed"
    finally:
        db.close()

    ok = (
        response.status_code == 422
        and "No extractable text" in (body.get("detail") or {}).get("message", "")
        and row_ok
    )
    record(
        "TEST 10 - Upload scanned PDF -> 422 + failed row (Phase 4 contract)",
        ok,
        f"http={response.status_code}; row status="
        f"{(fresh.status if fresh else '?')!r}",
    )


def test_10b_upload_endpoint_rejects_non_pdf() -> None:
    from io import BytesIO

    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    prior_count = _document_count()
    response = client.post(
        "/documents/upload",
        files={"file": ("notes.txt", BytesIO(b"hello world"), "text/plain")},
    )
    ok = response.status_code == 415 and _document_count() == prior_count
    record(
        "TEST 10b - Upload rejects non-PDF (415, no row)",
        ok,
        f"http={response.status_code}",
    )


def _document_count() -> int:
    from sqlalchemy import func, select

    db = SessionLocal()
    try:
        return int(db.execute(select(func.count()).select_from(Document)).scalar())
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Test 11 - ingest endpoint: retry without file -> 409 (no stored bytes)
# ---------------------------------------------------------------------------

def test_11_ingest_endpoint_requires_file_for_work() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pdf_bytes = make_pdf([make_prose(1500)])
    response = client.post(
        "/documents/upload",
        files={"file": ("policy.pdf", pdf_bytes, "application/pdf")},
    )
    doc_id = response.json()["id"]
    register(doc_id)

    # READY + force=True but no file -> the retry needs the bytes.
    reingest = client.post(f"/documents/{doc_id}/ingest", params={"force": "true"})
    ok = reingest.status_code == 409
    record(
        "TEST 11 - Ingest endpoint without file reports content unavailable",
        ok,
        f"http={reingest.status_code}; detail={reingest.json().get('detail', '')[:60]}",
    )


# ---------------------------------------------------------------------------
# Test 12 - ingest endpoint with file + force re-ingests cleanly
# ---------------------------------------------------------------------------

def test_12_ingest_endpoint_force_reingest() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pdf_bytes = make_pdf([make_prose(1500), make_prose(1500)])
    response = client.post(
        "/documents/upload",
        files={"file": ("policy.pdf", pdf_bytes, "application/pdf")},
    )
    doc_id = response.json()["id"]
    register(doc_id)
    first_chunk_count = response.json()["chunk_count"]

    reingest = client.post(
        f"/documents/{doc_id}/ingest",
        params={"force": "true"},
        files={"file": ("policy.pdf", pdf_bytes, "application/pdf")},
    )
    body = reingest.json()

    db = SessionLocal()
    try:
        stored = chunk_rows_for(db, uuid.UUID(doc_id))
    finally:
        db.close()

    ok = (
        reingest.status_code == 200
        and body["status"] == "ready"
        and len(stored) == first_chunk_count
        and len({(r.document_id, r.chunk_index) for r in stored}) == len(stored)
    )
    record(
        "TEST 12 - Ingest endpoint force re-ingest replaces cleanly",
        ok,
        f"http={reingest.status_code}; chunks before={first_chunk_count}, "
        f"after={len(stored)}",
    )


def test_12b_ingest_endpoint_retry_failed_document() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    # A scanned PDF fails at parse; the document row is left 'failed'.
    blank_pdf = make_pdf([""])
    response = client.post(
        "/documents/upload",
        files={"file": ("scan.pdf", blank_pdf, "application/pdf")},
    )
    doc_id = response.json()["detail"]["document_id"]
    register(doc_id)

    db = SessionLocal()
    try:
        fresh = db.get(Document, uuid.UUID(doc_id))
        failed_before = fresh is not None and fresh.status == "failed"
    finally:
        db.close()

    # Re-ingest the same (still textless) file -> still 422/parse-failed.
    retry = client.post(
        f"/documents/{doc_id}/ingest",
        files={"file": ("scan.pdf", blank_pdf, "application/pdf")},
    )
    db = SessionLocal()
    try:
        fresh2 = db.get(Document, uuid.UUID(doc_id))
        row_ok = fresh2 is not None and fresh2.status == "failed"
    finally:
        db.close()

    ok = failed_before and retry.status_code == 422 and row_ok
    record(
        "TEST 12b - Ingest endpoint retries a failed document cleanly",
        ok,
        f"http={retry.status_code}; status={(fresh2.status if fresh2 else '?')!r}",
    )


# ---------------------------------------------------------------------------
# Test 13 - pure build_chunk_rows helper
# ---------------------------------------------------------------------------

def test_13_build_chunk_rows_pure() -> None:
    from app.rag.chunker import ChunkRecord

    document_id = uuid.uuid4()
    chunks = [
        ChunkRecord(document_id=document_id, chunk_index=0, page_number=1, content="a"),
        ChunkRecord(document_id=document_id, chunk_index=1, page_number=2, content="b"),
    ]
    embeddings = [[0.1] * EMBEDDING_DIMENSION, [0.2] * EMBEDDING_DIMENSION]

    rows = ingestion_service.build_chunk_rows(document_id, chunks, embeddings)
    mismatched = (
        len(rows) != 2
        or rows[0].document_id != document_id
        or rows[0].chunk_index != 0
        or rows[0].page_number != 1
        or rows[0].content != "a"
        or rows[1].chunk_index != 1
        or rows[1].page_number != 2
        or rows[1].content != "b"
        or rows[0].embedding != embeddings[0]
        or rows[1].embedding != embeddings[1]
    )

    raised = False
    try:
        ingestion_service.build_chunk_rows(document_id, chunks, [embeddings[0]])
    except ValueError:
        raised = True

    ok = not mismatched and raised
    record(
        "TEST 13 - build_chunk_rows maps chunks+embeddings in order",
        ok,
        f"rows={len(rows)}; mismatch guard raised={raised}",
    )


# ---------------------------------------------------------------------------
# Test 14 - Phase 4 CRUD regression over the API
# ---------------------------------------------------------------------------

def test_14_document_crud_regression() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    pdf_bytes = make_pdf([make_prose(1500)])
    created = client.post(
        "/documents/upload",
        files={"file": ("crud.pdf", pdf_bytes, "application/pdf")},
    )
    doc_id = created.json()["id"]
    register(doc_id)

    listing = client.get("/documents")
    found = [d["id"] for d in listing.json() if d["id"] == doc_id]

    single = client.get(f"/documents/{doc_id}")
    missing = client.get("/documents/00000000-0000-0000-0000-000000000000")

    deleted = client.delete(f"/documents/{doc_id}")
    gone = client.get(f"/documents/{doc_id}")

    ok = (
        created.status_code == 201
        and listing.status_code == 200
        and len(found) == 1
        and single.status_code == 200
        and single.json()["status"] == "ready"
        and missing.status_code == 404
        and deleted.status_code == 204
        and gone.status_code == 404
    )
    record(
        "TEST 14 - Phase 4 CRUD regressions (list/get/404/delete) pass",
        ok,
        f"created={created.status_code}, list_found={len(found)}, "
        f"get={single.status_code}, missing={missing.status_code}, "
        f"delete={deleted.status_code}, gone={gone.status_code}",
    )


def main() -> None:
    print("Phase 7 ingestion & persistence verification")
    print("Pipeline: PDF -> pages -> chunks -> embeddings -> document_chunks")
    print(f"Embedding model: all-MiniLM-L6-v2, {EMBEDDING_DIMENSION} dim")
    print("Database: PostgreSQL + pgvector (live)")

    try:
        test_01_successful_multi_page_ingestion()
        test_02_zero_chunk_document_is_empty()
        test_03_no_text_pdf_is_failed()
        test_04_embedding_failure_leaves_no_chunks()
        test_05_db_failure_rolls_back()
        test_05b_db_failure_on_reingest_keeps_old_chunk_set()
        test_06_retry_after_failure_succeeds()
        test_07_ready_noop_without_force()
        test_07b_ready_noop_without_force_endpoint()
        test_08_force_reingestion_replaces_chunk_set()
        test_09_upload_endpoint_success()
        test_10_upload_endpoint_scanned_pdf_422()
        test_10b_upload_endpoint_rejects_non_pdf()
        test_11_ingest_endpoint_requires_file_for_work()
        test_12_ingest_endpoint_force_reingest()
        test_12b_ingest_endpoint_retry_failed_document()
        test_13_build_chunk_rows_pure()
        test_14_document_crud_regression()
    finally:
        print()
        print("Cleaning up created test documents...")
        for document_id in created_document_ids:
            cleanup_document(document_id)

    print()
    print(f"{'CHECK':<80}{'RESULT':<8}")
    print("-" * 90)
    failed = 0
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failed += 1
        print(f"{name:<80}{status:<8}")
        print(f"  -> {detail}")
    print("-" * 90)
    print(
        f"Total: {len(results)} checks, {len(results) - failed} passed, "
        f"{failed} failed"
    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()