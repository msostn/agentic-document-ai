"""Phase 15 retrieval quality verification.

Run from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\test_phase15_retrieval_quality.py

Exercises the real local embedding model (all-MiniLM-L6-v2) and the real
PostgreSQL + pgvector database, matching the repository's established
standalone-script test convention. No SQLite, no mocks of the core
retrieval/embedding behavior.

Coverage (Phase 15 spec section 14):
  the case-005 failure mode after the fix, chunk-boundary regression for the
  chunking change, similarity-vs-threshold behaviour for every Phase 14
  answerable question (not a single hard-coded case), unrelated-question
  rejection, document scoping, source metadata correctness, Phase 9 context
  status behaviour, and the Phase 15 configuration record.
"""

import dataclasses
import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import pymupdf  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.rag.chunker import chunk_document  # noqa: E402
from app.rag.context import build_rag_context  # noqa: E402
from app.rag.embeddings import embed_texts  # noqa: E402
from app.rag.retriever import RetrievalResult, retrieve_relevant_chunks  # noqa: E402
from app.schemas.rag import RAGContextStatus  # noqa: E402
from app.services import document_service, ingestion_service  # noqa: E402
from create_phase14_sample_document import FILENAME, SAMPLE_TEXT  # noqa: E402

results: list[tuple[str, bool, str]] = []
created_document_ids: list[uuid.UUID] = []

THRESHOLD = 0.30
EXPECTED_CHUNK_SIZE = 500
EXPECTED_CHUNK_OVERLAP = 100
EXPECTED_TOP_K_DEFAULT = 5
EXPECTED_TOP_K_MAX = 20
SAMPLE_SECTION_3_SENTENCE = (
    "Section 3. Waiting period. A waiting period of thirty (30) days applies "
    "to all claims arising from pre-existing conditions."
)

# Phase 14 answerable questions plus the Phase 15 paraphrase of case-005.
# Every one of them must clear the threshold: the fix has to be general, not
# a case-005 exception.
ANSWERABLE_QUERIES = [
    ("case-001", "What is the hospitalization deductible?"),
    ("case-002", "How much notice does the policyholder have to give to cancel the policy?"),
    ("case-003", "What is the maximum annual coverage limit?"),
    ("case-004", "Within how many days must a claim be filed?"),
    ("case-005", "Is there a waiting period for pre-existing conditions?"),
    ("case-015", "How long does a policyholder wait before claims for pre-existing conditions are covered?"),
]

# Questions that must stay below the threshold and therefore be rejected.
REFUSAL_QUERIES = [
    ("case-006", "What is the weather forecast for tomorrow?"),
    ("case-007", "Who won the 2022 FIFA World Cup final?"),
    ("case-019", "What is the capital of France?"),
    ("case-020", "Who wrote the novel Pride and Prejudice?"),
    ("case-021", "How do I bake a chocolate cake?"),
]

DOC_A_FILENAME = f"phase15_retrieval_a_{uuid.uuid4().hex[:8]}.pdf"
DOC_B_FILENAME = f"phase15_retrieval_b_{uuid.uuid4().hex[:8]}.pdf"

DOC_A_TEXT = SAMPLE_TEXT

DOC_B_TEXT = (
    "Riverside Office Lease Agreement\n"
    "Clause 7. Cancellation policy. The tenant may cancel this lease "
    "agreement only by giving sixty (60) days written notice to the "
    "landlord, together with a termination fee equal to one month of rent.\n"
    "Clause 8. Rent review. The annual rent review is conducted each "
    "January and may increase by no more than the published index rate.\n"
    "Clause 9. Maintenance. The landlord is responsible for structural "
    "repairs while the tenant maintains the interior finishings.\n"
)

_doc_a_id: uuid.UUID | None = None
_doc_b_id: uuid.UUID | None = None
_sample_chunks: list | None = None
_sample_vectors: list[list[float]] | None = None


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


def make_pdf(pages: list[str]) -> bytes:
    """Build an in-memory multi-page text PDF."""
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def ingest_fixture(filename: str, pages: list[str]) -> uuid.UUID:
    """Create and ingest a fixture document, registering it for cleanup."""
    db = SessionLocal()
    try:
        doc = document_service.create_document(db, filename=filename, file_type="pdf")
        document_id = doc.id
    finally:
        db.close()
    created_document_ids.append(document_id)

    pdf_bytes = make_pdf(pages)
    db = SessionLocal()
    try:
        ingestion_service.ingest_document(db, document_id, pdf_bytes)
    finally:
        db.close()
    return document_id


def ensure_fixtures() -> tuple[uuid.UUID, uuid.UUID]:
    """Ingest the two scoping fixtures once and reuse them for every test."""
    global _doc_a_id, _doc_b_id
    if _doc_a_id is None:
        _doc_a_id = ingest_fixture(DOC_A_FILENAME, [DOC_A_TEXT])
    if _doc_b_id is None:
        _doc_b_id = ingest_fixture(DOC_B_FILENAME, [DOC_B_TEXT])
    return _doc_a_id, _doc_b_id


def sample_chunks() -> list:
    """Chunk the Phase 14 sample text with the live Phase 15 configuration."""
    global _sample_chunks
    if _sample_chunks is None:
        _sample_chunks = chunk_document(uuid.uuid4(), [(1, SAMPLE_TEXT)])
    return _sample_chunks


def sample_vectors() -> list[list[float]]:
    """Embed the sample chunks once and cache them."""
    global _sample_vectors
    if _sample_vectors is None:
        _sample_vectors = embed_texts([c.content for c in sample_chunks()])
    return _sample_vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def best_similarity(query: str) -> tuple[float, str]:
    """Cosine similarity between a query and its best-matching sample chunk."""
    chunks = sample_chunks()
    vectors = sample_vectors()
    query_vector = embed_texts([query])[0]
    scored = [_cosine(query_vector, vector) for vector in vectors]
    best = max(range(len(scored)), key=scored.__getitem__)
    return scored[best], chunks[best].content


def find_sample_document() -> Document | None:
    db = SessionLocal()
    try:
        return (
            db.query(Document)
            .filter(Document.filename == FILENAME, Document.status == "ready")
            .first()
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 1-3 - Phase 15 configuration record
# ---------------------------------------------------------------------------

def test_01_configuration() -> None:
    chunking_ok = (
        settings.CHUNK_SIZE == EXPECTED_CHUNK_SIZE
        and settings.CHUNK_OVERLAP == EXPECTED_CHUNK_OVERLAP
        and settings.MIN_CHUNK_SIZE < settings.CHUNK_SIZE
        and settings.CHUNK_OVERLAP < settings.CHUNK_SIZE
    )
    record(
        "TEST 1 - Chunking configuration (500/100) is active",
        chunking_ok,
        f"size={settings.CHUNK_SIZE} overlap={settings.CHUNK_OVERLAP} "
        f"min={settings.MIN_CHUNK_SIZE}",
    )

    threshold_ok = abs(settings.RAG_MIN_SIMILARITY - THRESHOLD) < 1e-9
    record(
        "TEST 2 - RAG_MIN_SIMILARITY unchanged at 0.30 (not lowered)",
        threshold_ok,
        f"RAG_MIN_SIMILARITY={settings.RAG_MIN_SIMILARITY}",
    )

    topk_ok = (
        settings.RETRIEVAL_TOP_K_DEFAULT == EXPECTED_TOP_K_DEFAULT
        and settings.RETRIEVAL_TOP_K_MAX == EXPECTED_TOP_K_MAX
    )
    record(
        "TEST 3 - Retrieval top-k configuration unchanged",
        topk_ok,
        f"default={settings.RETRIEVAL_TOP_K_DEFAULT} "
        f"max={settings.RETRIEVAL_TOP_K_MAX}",
    )


# ---------------------------------------------------------------------------
# TEST 4-6 - Chunking regression for the Phase 15 change
# ---------------------------------------------------------------------------

def test_04_chunk_granularity() -> None:
    chunks = sample_chunks()
    too_large = [c for c in chunks if len(c.content) > settings.CHUNK_SIZE]
    record(
        "TEST 4 - Sample document splits into finer chunks within budget",
        len(chunks) >= 3 and not too_large,
        f"chunks={len(chunks)} (Phase 14 had 2) "
        f"lengths={[len(c.content) for c in chunks]}",
    )


def test_05_answer_sentence_not_split() -> None:
    chunks = sample_chunks()
    contained = [c for c in chunks if SAMPLE_SECTION_3_SENTENCE in c.content]
    record(
        "TEST 5 - Section 3 answer sentence lives in a single chunk",
        len(contained) == 1,
        f"chunks containing the full sentence={len(contained)}",
    )


def test_06_content_preserved() -> None:
    chunks = sample_chunks()
    joined = " ".join(c.content for c in chunks)
    missing = [
        marker
        for marker in (
            "Section 1.",
            "Section 2.",
            "Section 3.",
            "Section 4.",
            "Section 5.",
            "Section 6.",
        )
        if marker not in joined
    ]
    record(
        "TEST 6 - All sections survive chunking (no dropped content)",
        not missing,
        f"missing={missing or 'none'}",
    )


# ---------------------------------------------------------------------------
# TEST 7-8 - Answerable questions must clear the threshold
# ---------------------------------------------------------------------------

def test_07_answerable_queries_clear_threshold() -> None:
    for case_id, query in ANSWERABLE_QUERIES:
        similarity, best_content = best_similarity(query)
        cleared = similarity >= settings.RAG_MIN_SIMILARITY
        record(
            f"TEST 7 - {case_id} clears the similarity threshold",
            cleared,
            f"best_similarity={similarity:.4f} "
            f"threshold={settings.RAG_MIN_SIMILARITY}",
        )
        if case_id == "case-005":
            record(
                "TEST 8 - case-005 best chunk contains the answer text",
                cleared and "pre-existing conditions" in best_content,
                f"best_chunk_len={len(best_content)}",
            )


# ---------------------------------------------------------------------------
# TEST 14 - Unrelated questions must stay below the threshold
# ---------------------------------------------------------------------------

def test_14_refusals_stay_below_threshold() -> None:
    for case_id, query in REFUSAL_QUERIES:
        similarity, _ = best_similarity(query)
        record(
            f"TEST 14 - {case_id} stays below the similarity threshold",
            similarity < settings.RAG_MIN_SIMILARITY,
            f"best_similarity={similarity:.4f} "
            f"threshold={settings.RAG_MIN_SIMILARITY}",
        )


# ---------------------------------------------------------------------------
# TEST 19-21 - Document scoping and source metadata
# ---------------------------------------------------------------------------

def test_19_document_scoping() -> None:
    doc_a, doc_b = ensure_fixtures()

    db = SessionLocal()
    try:
        results_a = retrieve_relevant_chunks(
            db, doc_a, "What is the cancellation policy?", top_k=5
        )
        results_b = retrieve_relevant_chunks(
            db, doc_b, "What is the cancellation policy?", top_k=5
        )
    finally:
        db.close()

    ids_a = {r.chunk_id for r in results_a}
    ids_b = {r.chunk_id for r in results_b}

    record(
        "TEST 19 - Document A retrieval never returns Document B chunks",
        bool(results_a)
        and all(r.document_id == doc_a for r in results_a)
        and not (ids_a & ids_b),
        f"a_results={len(results_a)} b_results={len(results_b)} overlap=0",
    )
    record(
        "TEST 20 - Document B retrieval never returns Document A chunks",
        bool(results_b)
        and all(r.document_id == doc_b for r in results_b)
        and not (ids_a & ids_b),
        f"b_results={len(results_b)} a_results={len(results_a)} overlap=0",
    )


def test_21_source_metadata() -> None:
    doc_a, _ = ensure_fixtures()

    db = SessionLocal()
    try:
        results = retrieve_relevant_chunks(
            db,
            doc_a,
            "Is there a waiting period for pre-existing conditions?",
            top_k=5,
        )
    finally:
        db.close()

    expected_fields = {
        "chunk_id",
        "document_id",
        "chunk_index",
        "page_number",
        "content",
        "distance",
        "similarity",
    }
    fields = {f.name for f in dataclasses.fields(RetrievalResult)}
    metadata_ok = bool(results) and all(
        r.document_id == doc_a
        and isinstance(r.chunk_index, int)
        and r.page_number >= 1
        and r.content
        and abs((1.0 - r.distance) - r.similarity) < 1e-9
        for r in results
    )
    record(
        "TEST 21 - Source metadata is backend-derived and complete",
        metadata_ok and fields == expected_fields,
        f"fields={sorted(fields)} results={len(results)}",
    )


# ---------------------------------------------------------------------------
# TEST 22-24 - Phase 9 context statuses with the Phase 15 chunking
# ---------------------------------------------------------------------------

def test_22_context_statuses() -> None:
    doc_a, _ = ensure_fixtures()

    db = SessionLocal()
    try:
        ok_result = build_rag_context(
            db,
            document_id=doc_a,
            query="Is there a waiting period for pre-existing conditions?",
        )
        reject_result = build_rag_context(
            db, document_id=doc_a, query="What is the capital of France?"
        )
    finally:
        db.close()

    record(
        "TEST 22 - case-005 now builds a grounded context (status ok)",
        ok_result.status == RAGContextStatus.OK
        and len(ok_result.chunks) > 0
        and (ok_result.top_similarity or 0.0) >= settings.RAG_MIN_SIMILARITY,
        f"status={ok_result.status.value} chunks={len(ok_result.chunks)} "
        f"top={ok_result.top_similarity}",
    )
    record(
        "TEST 23 - Out-of-scope question still returns below_similarity_threshold",
        reject_result.status == RAGContextStatus.BELOW_SIMILARITY_THRESHOLD
        and len(reject_result.chunks) == 0
        and reject_result.context_text is None,
        f"status={reject_result.status.value} "
        f"chunks={len(reject_result.chunks)}",
    )
    record(
        "TEST 24 - Grounded context carries page/chunk metadata",
        bool(ok_result.chunks)
        and all(c.page_number >= 1 and c.chunk_index >= 0 for c in ok_result.chunks),
        f"pages={[c.page_number for c in ok_result.chunks]}",
    )


# ---------------------------------------------------------------------------
# TEST 25 - The live evaluation document, when present
# ---------------------------------------------------------------------------

def test_25_live_sample_document() -> None:
    doc = find_sample_document()
    if doc is None:
        print(
            f"  note: {FILENAME} is not loaded; run "
            "scripts/create_phase14_sample_document.py first. "
            "Fixture coverage above still applies."
        )
        return

    db = SessionLocal()
    try:
        results = retrieve_relevant_chunks(
            db,
            doc.id,
            "Is there a waiting period for pre-existing conditions?",
            top_k=5,
        )
    finally:
        db.close()

    best = max((r.similarity for r in results), default=float("nan"))
    record(
        "TEST 25 - Loaded sample document answers case-005 above threshold",
        bool(results) and best >= settings.RAG_MIN_SIMILARITY,
        f"best_similarity={best:.4f} chunks_returned={len(results)}",
    )


def main() -> None:
    print("Phase 15 retrieval quality verification")
    print(
        f"Embedding model: {settings.EMBEDDING_MODEL}, "
        f"device={settings.EMBEDDING_DEVICE}"
    )
    print(
        f"CHUNK_SIZE={settings.CHUNK_SIZE} "
        f"CHUNK_OVERLAP={settings.CHUNK_OVERLAP} "
        f"MIN_CHUNK_SIZE={settings.MIN_CHUNK_SIZE}"
    )
    print(
        f"RAG_MIN_SIMILARITY={settings.RAG_MIN_SIMILARITY} "
        f"RETRIEVAL_TOP_K_DEFAULT={settings.RETRIEVAL_TOP_K_DEFAULT}"
    )
    print("Database: PostgreSQL + pgvector (live)")
    print()

    try:
        test_01_configuration()
        test_04_chunk_granularity()
        test_05_answer_sentence_not_split()
        test_06_content_preserved()
        test_07_answerable_queries_clear_threshold()
        test_14_refusals_stay_below_threshold()
        test_19_document_scoping()
        test_21_source_metadata()
        test_22_context_statuses()
        test_25_live_sample_document()
    finally:
        print()
        print("Cleaning up created test documents...")
        for document_id in created_document_ids:
            cleanup_document(document_id)

    print()
    print(f"{'CHECK':<72}{'RESULT':<8}")
    print("-" * 100)
    failed = 0
    for name, passed, detail in results:
        status = "PASS" if passed else "FAIL"
        if not passed:
            failed += 1
        print(f"{name:<72}{status:<8}")
        print(f"  -> {detail}")
    print("-" * 100)
    print(
        f"Total: {len(results)} checks, {len(results) - failed} passed, "
        f"{failed} failed"
    )
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
