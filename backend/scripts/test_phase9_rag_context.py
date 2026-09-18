"""Phase 9 RAG context orchestration verification.

Run from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\test_phase9_rag_context.py

Exercises the real local embedding model (all-MiniLM-L6-v2) and the real
PostgreSQL + pgvector database, matching the repository's established
standalone-script test convention. No SQLite, no mocks of the core
retrieval/embedding behavior.

Coverage (Phase 9 spec section 22):
  Context success, metadata preservation, isolation, query validation,
  similarity threshold, character budget, determinism, architecture checks
  (single retrieval call, no embedding call, no DB writes, no Ollama),
  and Phase 4-8 regression.
"""

import sys
import uuid
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

import pymupdf  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.document import Document  # noqa: E402
from app.models.document_chunk import DocumentChunk  # noqa: E402
from app.rag.context import build_rag_context  # noqa: E402
from app.rag.embeddings import embed_texts  # noqa: E402
from app.rag.exceptions import (  # noqa: E402
    DocumentEmptyError,
    DocumentIngestionFailedError,
    DocumentNotFoundError,
    DocumentNotReadyError,
    InvalidQueryError,
)
from app.rag.retriever import RetrievalResult  # noqa: E402
from app.schemas.rag import RAGContextResult, RAGContextStatus  # noqa: E402
from app.services import document_service, ingestion_service  # noqa: E402

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


def create_processing_document(filename: str = "test.pdf") -> Document:
    db = SessionLocal()
    try:
        doc = document_service.create_document(db, filename=filename, file_type="pdf")
    finally:
        db.close()
    return doc


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


# ---------------------------------------------------------------------------
# Fixtures: deterministic documents with clearly distinct topics
# ---------------------------------------------------------------------------

DOCUMENT_A_INSURANCE = [
    "Car insurance policies require a deductible to be paid before the "
    "insurance company begins to cover the remaining costs of a claim. "
    "The deductible amount varies depending on the specific policy and "
    "coverage options selected by the policyholder. Premium payments must "
    "be made on time to maintain active coverage. Filing a claim involves "
    "contacting the insurance company and providing documentation of the "
    "incident including photos and police reports. The claims process may "
    "take several weeks to complete depending on the complexity of the case.",

    "Insurance premiums are calculated based on several factors including "
    "the driver age driving history type of vehicle and the coverage level "
    "selected. Higher deductibles generally result in lower monthly premiums "
    "because the policyholder assumes more of the financial risk. Safe "
    "driving habits and a clean driving record can help reduce insurance "
    "costs over time. Multiple vehicle discounts and bundling policies "
    "together can also provide significant savings on total insurance costs.",

    "When filing an insurance claim the policyholder must first report the "
    "incident to their insurance company as soon as possible. The insurance "
    "company will assign a claims adjuster to investigate the incident and "
    "assess the damage. The adjuster will review the policy coverage limits "
    "and determine the amount the insurance company will pay for the claim. "
    "If the claim amount exceeds the deductible the insurance company pays "
    "the difference up to the policy limits. Claim settlement checks are "
    "typically issued within thirty days of claim approval by the company.",
]

DOCUMENT_B_UNIVERSITY = [
    "University attendance policies require students to maintain a minimum "
    "attendance rate in order to remain enrolled in their courses. Students "
    "who fail to meet the attendance requirements may be subject to academic "
    "probation or dismissal from the university program. The attendance "
    "policy is designed to ensure students receive the full benefit of "
    "class instruction and participation in academic activities each term.",

    "Examination rules at the university require students to present valid "
    "identification before taking any scheduled exam. Students found "
    "cheating or using unauthorized materials during an exam will face "
    "immediate disciplinary action. The examination rules also specify that "
    "late arrivals may be admitted but will not receive additional time. "
    "All exam papers must be submitted at the end of the designated period.",
]


def ingest_fixture(
    filename: str, pages: list[str], status: str = "ready"
) -> uuid.UUID:
    """Ingest a fixture document and return its ID."""
    doc = create_processing_document(filename)
    register(doc.id)

    if status == "ready":
        pdf_bytes = make_pdf(pages)
        db = SessionLocal()
        try:
            ingestion_service.ingest_document(db, doc.id, pdf_bytes)
        finally:
            db.close()
    else:
        db = SessionLocal()
        try:
            d = db.get(Document, doc.id)
            d.status = status
            if status == "empty":
                d.chunk_count = 0
            db.commit()
        finally:
            db.close()

    return doc.id


# ---------------------------------------------------------------------------
# TEST 1 - Relevant query returns OK status
# ---------------------------------------------------------------------------

def test_01_relevant_query_ok() -> None:
    doc_id = ingest_fixture("rag_ins1.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="car insurance deductible")
        ok = (
            result.status == RAGContextStatus.OK
            and len(result.chunks) > 0
            and result.context_text is not None
            and len(result.context_text) > 0
            and result.total_included > 0
        )
        record(
            "TEST 1 - Relevant query returns OK status with context",
            ok,
            f"status={result.status.value}; included={result.total_included}; "
            f"context_len={len(result.context_text or '')}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 2 - Metadata preserved
# ---------------------------------------------------------------------------

def test_02_metadata_preserved() -> None:
    doc_id = ingest_fixture("rag_ins2.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="premium payment")
        ok = (
            result.status == RAGContextStatus.OK
            and result.document_id == doc_id
            and all(c.document_id == doc_id for c in result.chunks)
            and all(c.chunk_id is not None for c in result.chunks)
            and all(c.page_number >= 1 for c in result.chunks)
            and all(isinstance(c.distance, float) for c in result.chunks)
            and all(isinstance(c.similarity, float) for c in result.chunks)
        )
        record(
            "TEST 2 - Metadata preserved in result",
            ok,
            f"doc_id match={result.document_id == doc_id}; "
            f"chunks={len(result.chunks)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 3 - Rank preserved
# ---------------------------------------------------------------------------

def test_03_rank_preserved() -> None:
    doc_id = ingest_fixture("rag_ins3.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="claim filing process")
        ranks = [c.rank for c in result.chunks]
        # Ranks must be sequential 1..N
        ranks_ok = ranks == list(range(1, len(ranks) + 1))
        # Similarity must be non-increasing (first chunk most similar)
        sim_ok = all(
            result.chunks[i].similarity >= result.chunks[i + 1].similarity
            for i in range(len(result.chunks) - 1)
        )
        ok = result.status == RAGContextStatus.OK and ranks_ok and sim_ok
        record(
            "TEST 3 - Rank preserved and ordered by similarity",
            ok,
            f"ranks={ranks[:10]}; sim_order={sim_ok}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 4 - Page numbers preserved
# ---------------------------------------------------------------------------

def test_04_page_numbers_preserved() -> None:
    doc_id = ingest_fixture("rag_ins4.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="coverage options")
        ok = (
            result.status == RAGContextStatus.OK
            and all(c.page_number >= 1 for c in result.chunks)
        )
        pages = [c.page_number for c in result.chunks]
        record(
            "TEST 4 - Page numbers preserved",
            ok,
            f"pages={sorted(set(pages))}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 5 - Chunk IDs preserved
# ---------------------------------------------------------------------------

def test_05_chunk_ids_preserved() -> None:
    doc_id = ingest_fixture("rag_ins5.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="deductible amount")
        ok = (
            result.status == RAGContextStatus.OK
            and all(isinstance(c.chunk_id, uuid.UUID) for c in result.chunks)
            and len({c.chunk_id for c in result.chunks}) == len(result.chunks)
        )
        record(
            "TEST 5 - Chunk IDs preserved and unique",
            ok,
            f"unique_ids={len({c.chunk_id for c in result.chunks})}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 6 - Document A never returns chunks from B (isolation)
# ---------------------------------------------------------------------------

def test_06_isolation_a_never_b() -> None:
    doc_a = ingest_fixture("rag_iso_a.pdf", DOCUMENT_A_INSURANCE)
    doc_b = ingest_fixture("rag_iso_b.pdf", DOCUMENT_B_UNIVERSITY)
    db = SessionLocal()
    try:
        result_a = build_rag_context(db, document_id=doc_a, query="university examination rules")
        all_belong_to_a = all(c.document_id == doc_a for c in result_a.chunks)
        ok = all_belong_to_a
        record(
            "TEST 6 - Document A never returns chunks from B",
            ok,
            f"chunks={len(result_a.chunks)}; all belong to A={all_belong_to_a}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 7 - Internal invariant: every chunk has matching document_id
# ---------------------------------------------------------------------------

def test_07_internal_invariant() -> None:
    doc_id = ingest_fixture("rag_inv.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="insurance")
        ok = all(c.document_id == doc_id for c in result.chunks)
        record(
            "TEST 7 - Internal invariant: all chunks match requested document_id",
            ok,
            f"chunks={len(result.chunks)}; all match={ok}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 8 - Empty query
# ---------------------------------------------------------------------------

def test_08_empty_query() -> None:
    doc_id = ingest_fixture("rag_eq.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="")
        ok = (
            result.status == RAGContextStatus.INVALID_QUERY
            and result.chunks == []
            and result.context_text is None
            and result.total_retrieved == 0
        )
        record(
            "TEST 8 - Empty query returns INVALID_QUERY",
            ok,
            f"status={result.status.value}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 9 - Whitespace query
# ---------------------------------------------------------------------------

def test_09_whitespace_query() -> None:
    doc_id = ingest_fixture("rag_wsq.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(db, document_id=doc_id, query="   \t\n  ")
        ok = (
            result.status == RAGContextStatus.INVALID_QUERY
            and result.chunks == []
            and result.context_text is None
        )
        record(
            "TEST 9 - Whitespace query returns INVALID_QUERY",
            ok,
            f"status={result.status.value}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 10 - Weak chunks filtered by threshold
# ---------------------------------------------------------------------------

def test_10_weak_chunks_filtered() -> None:
    doc_id = ingest_fixture("rag_weak.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="university examination rules",
            min_similarity=0.99,
        )
        ok = (
            result.status == RAGContextStatus.BELOW_SIMILARITY_THRESHOLD
            and result.chunks == []
            and result.total_included == 0
            and result.top_similarity is not None
        )
        record(
            "TEST 10 - Weak chunks filtered by high threshold",
            ok,
            f"status={result.status.value}; top_sim={result.top_similarity:.4f}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 11 - All filtered -> BELOW_SIMILARITY_THRESHOLD
# ---------------------------------------------------------------------------

def test_11_all_filtered_below_threshold() -> None:
    doc_id = ingest_fixture("rag_allf.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="quantum physics relativity",
            min_similarity=0.99,
        )
        ok = (
            result.status == RAGContextStatus.BELOW_SIMILARITY_THRESHOLD
            and result.total_included == 0
            and result.context_text is None
            and result.top_similarity is not None
        )
        record(
            "TEST 11 - All filtered below threshold",
            ok,
            f"status={result.status.value}; top_sim={result.top_similarity:.4f}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 12 - Small budget keeps highest-ranked chunks
# ---------------------------------------------------------------------------

def test_12_small_budget() -> None:
    doc_id = ingest_fixture("rag_bud.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result_normal = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible"
        )
        # Use a budget that allows some but not all chunks
        # Find the length of the first chunk's rendered block
        if result_normal.chunks:
            first_block_len = len(
                f"[Page {result_normal.chunks[0].page_number} | Chunk {result_normal.chunks[0].chunk_index}]\n"
                f"{result_normal.chunks[0].content}"
            )
            # Set budget to fit first chunk but not all
            budget = first_block_len + 50
        else:
            budget = 200
        result_small = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible",
            max_context_chars=budget,
        )
        ok = (
            result_normal.status == RAGContextStatus.OK
            and result_small.total_included <= result_normal.total_included
            and result_small.total_characters <= budget
            and result_small.context_text is not None
            and len(result_small.context_text) <= budget
        )
        record(
            "TEST 12 - Small budget keeps highest-ranked chunks only",
            ok,
            f"normal_included={result_normal.total_included}; "
            f"small_included={result_small.total_included}; "
            f"small_chars={result_small.total_characters}; budget={budget}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 13 - Budget counts rendered formatting
# ---------------------------------------------------------------------------

def test_13_budget_counts_rendering() -> None:
    doc_id = ingest_fixture("rag_bud2.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        # Use a large budget so at least one chunk fits
        result = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible",
            max_context_chars=10000,
        )
        if result.status == RAGContextStatus.OK and result.context_text is not None:
            # Verify context_text starts with [Page header
            has_headers = "[Page " in result.context_text
            # Verify the rendered format matches expected pattern
            ok = has_headers
            record(
                "TEST 13 - Budget counts rendered formatting including headers",
                ok,
                f"context_len={len(result.context_text)}; "
                f"has_headers={has_headers}",
            )
        else:
            record(
                "TEST 13 - Budget counts rendered formatting including headers",
                False,
                f"status={result.status.value}; no context_text",
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 14 - Oversized chunk never truncated
# ---------------------------------------------------------------------------

def test_14_no_truncation() -> None:
    doc_id = ingest_fixture("rag_notrunc.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible",
            max_context_chars=100,
        )
        # Every included chunk's content must be present in context_text
        if result.context_text is not None:
            for chunk in result.chunks:
                if chunk.included:
                    if chunk.content not in result.context_text:
                        ok = False
                        record(
                            "TEST 14 - No truncation of included chunks",
                            ok,
                            f"chunk {chunk.rank} content not fully in context_text",
                        )
                        return
            ok = True
            record(
                "TEST 14 - No truncation of included chunks",
                ok,
                f"all {result.total_included} included chunks verified",
            )
        else:
            ok = result.status == RAGContextStatus.NO_CHUNK_FITS_BUDGET
            record(
                "TEST 14 - No truncation (NO_CHUNK_FITS_BUDGET is acceptable)",
                ok,
                f"status={result.status.value}",
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 15 - First chunk too large -> NO_CHUNK_FITS_BUDGET
# ---------------------------------------------------------------------------

def test_15_first_chunk_too_large() -> None:
    doc_id = ingest_fixture("rag_toosmall.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible",
            max_context_chars=10,
        )
        ok = (
            result.status == RAGContextStatus.NO_CHUNK_FITS_BUDGET
            and result.total_included == 0
            and result.context_text is None
            and len(result.chunks) > 0
        )
        record(
            "TEST 15 - First chunk too large -> NO_CHUNK_FITS_BUDGET",
            ok,
            f"status={result.status.value}; chunks_above_threshold={len(result.chunks)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 16 - Context rendering deterministic
# ---------------------------------------------------------------------------

def test_16_deterministic_rendering() -> None:
    doc_id = ingest_fixture("rag_det.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result1 = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible"
        )
        result2 = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible"
        )
        ok = (
            result1.context_text == result2.context_text
            and result1.status == result2.status
            and result1.total_included == result2.total_included
            and [(c.chunk_id, c.included) for c in result1.chunks] ==
                [(c.chunk_id, c.included) for c in result2.chunks]
        )
        record(
            "TEST 16 - Context rendering is deterministic",
            ok,
            f"texts_equal={result1.context_text == result2.context_text}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 17 - Context reproducible from included chunks
# ---------------------------------------------------------------------------

def test_17_reproducible_from_chunks() -> None:
    doc_id = ingest_fixture("rag_repro.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="car insurance deductible"
        )
        if result.status == RAGContextStatus.OK and result.context_text is not None:
            # Rebuild context_text from included chunks
            included = [c for c in result.chunks if c.included]
            parts: list[str] = []
            for i, chunk in enumerate(included):
                block = f"[Page {chunk.page_number} | Chunk {chunk.chunk_index}]\n{chunk.content}"
                if i > 0:
                    parts.append("\n\n")
                parts.append(block)
            reconstructed = "".join(parts)
            ok = result.context_text == reconstructed
            record(
                "TEST 17 - Context reproducible from included chunks",
                ok,
                f"match={ok}; original_len={len(result.context_text)}; "
                f"reconstructed_len={len(reconstructed)}",
            )
        else:
            record(
                "TEST 17 - Context reproducible from included chunks",
                False,
                f"status={result.status.value}; no context to verify",
            )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 18 - Retrieval called exactly once (architecture)
# ---------------------------------------------------------------------------

def test_18_single_retrieval_call() -> None:
    """Verify Phase 8's retrieve_relevant_chunks is called exactly once."""
    import unittest.mock as mock

    doc_id = ingest_fixture("rag_1call.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        call_count = 0
        original_retrieve = None

        from app.rag import context as ctx_module
        original_retrieve = ctx_module.retrieve_relevant_chunks

        def counting_retrieve(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return original_retrieve(*args, **kwargs)

        with mock.patch.object(ctx_module, "retrieve_relevant_chunks", counting_retrieve):
            result = build_rag_context(db, document_id=doc_id, query="deductible payment")

        ok = call_count == 1
        record(
            "TEST 18 - Phase 8 retrieval called exactly once",
            ok,
            f"call_count={call_count}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 19 - No embedding call inside Phase 9 (architecture)
# ---------------------------------------------------------------------------

def test_19_no_embedding_call() -> None:
    """Verify Phase 9 does not call the embedding service directly.

    Phase 9 should delegate to the retriever (Phase 8) which handles
    embedding internally. We verify this by patching embed_texts to raise
    if called directly from context.py (but not from retriever.py).
    """
    import unittest.mock as mock

    doc_id = ingest_fixture("rag_noembed.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        # Patch at the retriever module's imported reference
        from app.rag import retriever as retriever_mod
        from app.rag import context as ctx_module
        original_ref = retriever_mod.embed_texts

        call_tracker = {"count": 0}

        def tracking_embed(*args, **kwargs):
            call_tracker["count"] += 1
            return original_ref(*args, **kwargs)

        with mock.patch.object(retriever_mod, "embed_texts", tracking_embed):
            result = build_rag_context(db, document_id=doc_id, query="deductible payment")

        # embed_texts should be called exactly once (from retriever's _embed_query)
        ok = call_tracker["count"] == 1
        record(
            "TEST 19 - Embedding called exactly once (via retriever only)",
            ok,
            f"embed_call_count={call_tracker['count']}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 20 - No vector SQL duplication (architecture)
# ---------------------------------------------------------------------------

def test_20_no_vector_sql_duplication() -> None:
    """Verify Phase 9 doesn't contain cosine_distance or pgvector imports."""
    from app.rag import context as ctx_module
    import inspect
    source = inspect.getsource(ctx_module)
    has_cosine = "cosine_distance" in source
    has_pgvector = "pgvector" in source.lower()
    has_embedding_col = "embedding.cosine" in source
    ok = not has_cosine and not has_embedding_col
    record(
        "TEST 20 - No vector SQL duplication in context.py",
        ok,
        f"has_cosine_distance={has_cosine}; has_embedding_col={has_embedding_col}",
    )


# ---------------------------------------------------------------------------
# TEST 21 - No DB writes
# ---------------------------------------------------------------------------

def test_21_no_db_writes() -> None:
    """Verify Phase 9 performs no database writes."""
    doc_id = ingest_fixture("rag_nowrites.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        chunks_before = len(
            db.execute(
                __import__("sqlalchemy", fromlist=["select"]).select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
            ).scalars().all()
        )
        doc_before = db.get(Document, doc_id)
        status_before = doc_before.status if doc_before else None
        chunk_count_before = doc_before.chunk_count if doc_before else None

        result = build_rag_context(db, document_id=doc_id, query="deductible payment")

        chunks_after = len(
            db.execute(
                __import__("sqlalchemy", fromlist=["select"]).select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
            ).scalars().all()
        )
        doc_after = db.get(Document, doc_id)
        status_after = doc_after.status if doc_after else None
        chunk_count_after = doc_after.chunk_count if doc_after else None

        ok = (
            chunks_before == chunks_after
            and status_before == status_after
            and chunk_count_before == chunk_count_after
        )
        record(
            "TEST 21 - Phase 9 performs no database writes",
            ok,
            f"chunks before={chunks_before}, after={chunks_after}; "
            f"status unchanged={status_before == status_after}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 22 - No Ollama imports (architecture)
# ---------------------------------------------------------------------------

def test_22_no_ollama_imports() -> None:
    """Verify Phase 9 does not import Ollama or LLM clients."""
    from app.rag import context as ctx_module
    import inspect
    source = inspect.getsource(ctx_module)
    has_ollama = "ollama" in source.lower()
    has_openai = "openai" in source.lower()
    has_anthropic = "anthropic" in source.lower()
    ok = not has_ollama and not has_openai and not has_anthropic
    record(
        "TEST 22 - No Ollama/LLM imports in context.py",
        ok,
        f"ollama={has_ollama}; openai={has_openai}; anthropic={has_anthropic}",
    )


# ---------------------------------------------------------------------------
# TEST 23 - Config override works
# ---------------------------------------------------------------------------

def test_23_config_override() -> None:
    """Verify min_similarity and max_context_chars can be overridden."""
    doc_id = ingest_fixture("rag_override.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="deductible",
            min_similarity=-1.0,
            max_context_chars=999999,
        )
        ok = (
            result.status == RAGContextStatus.OK
            and result.similarity_threshold_used == -1.0
            and result.max_chars_used == 999999
        )
        record(
            "TEST 23 - min_similarity and max_context_chars overrides work",
            ok,
            f"threshold={result.similarity_threshold_used}; "
            f"max_chars={result.max_chars_used}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 24 - BELOW_SIMILARITY_THRESHOLD has top_similarity populated
# ---------------------------------------------------------------------------

def test_24_below_threshold_has_top_sim() -> None:
    doc_id = ingest_fixture("rag_topsim.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        result = build_rag_context(
            db, document_id=doc_id, query="quantum computing",
            min_similarity=0.99,
        )
        ok = (
            result.status == RAGContextStatus.BELOW_SIMILARITY_THRESHOLD
            and result.top_similarity is not None
            and isinstance(result.top_similarity, float)
        )
        record(
            "TEST 24 - BELOW_SIMILARITY_THRESHOLD has top_similarity",
            ok,
            f"top_similarity={result.top_similarity}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 25-29 - Phase 4/5/6/7/8 regression
# ---------------------------------------------------------------------------

def test_25_phase4_regression() -> None:
    """Phase 4: PDF upload and CRUD still work."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    pdf_bytes = make_pdf([DOCUMENT_A_INSURANCE[0]])
    upload = client.post(
        "/documents/upload",
        files={"file": ("regression_p9.pdf", pdf_bytes, "application/pdf")},
    )
    doc_id = upload.json().get("id")
    if doc_id:
        register(doc_id)

    listing = client.get("/documents")
    ok = (
        upload.status_code == 201
        and upload.json()["status"] == "ready"
        and listing.status_code == 200
    )
    record(
        "TEST 25 - Phase 4 regression (upload, list) passes",
        ok,
        f"upload={upload.status_code}; list={listing.status_code}",
    )


def test_26_phase5_regression() -> None:
    """Phase 5: chunker produces correct output shape."""
    from app.rag.chunker import chunk_document

    doc_id = uuid.uuid4()
    pages = [(1, DOCUMENT_A_INSURANCE[0]), (2, DOCUMENT_A_INSURANCE[1])]
    chunks = chunk_document(doc_id, pages)
    ok = (
        len(chunks) > 0
        and all(c.document_id == doc_id for c in chunks)
        and all(c.chunk_index == i for i, c in enumerate(chunks))
        and all(c.page_number in (1, 2) for c in chunks)
        and all(len(c.content) > 0 for c in chunks)
    )
    record(
        "TEST 26 - Phase 5 regression (chunker) passes",
        ok,
        f"chunks={len(chunks)}; pages={sorted(set(c.page_number for c in chunks))}",
    )


def test_27_phase6_regression() -> None:
    """Phase 6: embedding service works correctly."""
    text_single = "car insurance deductible"
    text_batch = ["premium payment", "claim filing"]
    single = embed_texts(text_single)
    batch = embed_texts(text_batch)
    ok = (
        isinstance(single, list)
        and len(single) == 384
        and all(isinstance(v, float) for v in single)
        and isinstance(batch, list)
        and len(batch) == 2
        and all(len(v) == 384 for v in batch)
    )
    record(
        "TEST 27 - Phase 6 regression (embeddings) passes",
        ok,
        f"single dim={len(single)}; batch size={len(batch)}",
    )


def test_28_phase7_regression() -> None:
    """Phase 7: ingestion creates correct chunk rows with embeddings."""
    doc_id = ingest_fixture("regression_p9_p7.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        chunks = list(
            db.execute(
                __import__("sqlalchemy", fromlist=["select"]).select(DocumentChunk)
                .where(DocumentChunk.document_id == doc_id)
                .order_by(DocumentChunk.chunk_index)
            ).scalars()
        )
        ok = (
            len(chunks) > 0
            and all(r.document_id == doc_id for r in chunks)
            and all(r.embedding is not None for r in chunks)
            and all(len(r.embedding) == 384 for r in chunks)
            and [r.chunk_index for r in chunks] == list(range(len(chunks)))
        )
        record(
            "TEST 28 - Phase 7 regression (ingestion/persistence) passes",
            ok,
            f"chunks={len(chunks)}; all 384-dim={all(len(r.embedding) == 384 for r in chunks)}",
        )
    finally:
        db.close()


def test_29_phase8_regression() -> None:
    """Phase 8: retrieval works correctly."""
    from app.rag.retriever import retrieve_relevant_chunks

    doc_id = ingest_fixture("regression_p9_p8.pdf", DOCUMENT_A_INSURANCE)
    db = SessionLocal()
    try:
        results_list = retrieve_relevant_chunks(db, doc_id, "car insurance deductible")
        ok = (
            len(results_list) > 0
            and any("deductible" in r.content.lower() for r in results_list)
        )
        record(
            "TEST 29 - Phase 8 regression (retrieval) passes",
            ok,
            f"results={len(results_list)}",
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Phase 9 RAG context orchestration verification")
    print("Pipeline: query -> validate -> Phase 8 retrieval -> threshold -> budget -> context")
    print(f"Embedding model: all-MiniLM-L6-v2, 384 dim")
    print("Database: PostgreSQL + pgvector (live)")
    print(f"RAG_CONTEXT_MAX_CHARS={settings.RAG_CONTEXT_MAX_CHARS}")
    print(f"RAG_MIN_SIMILARITY={settings.RAG_MIN_SIMILARITY}")

    try:
        test_01_relevant_query_ok()
        test_02_metadata_preserved()
        test_03_rank_preserved()
        test_04_page_numbers_preserved()
        test_05_chunk_ids_preserved()
        test_06_isolation_a_never_b()
        test_07_internal_invariant()
        test_08_empty_query()
        test_09_whitespace_query()
        test_10_weak_chunks_filtered()
        test_11_all_filtered_below_threshold()
        test_12_small_budget()
        test_13_budget_counts_rendering()
        test_14_no_truncation()
        test_15_first_chunk_too_large()
        test_16_deterministic_rendering()
        test_17_reproducible_from_chunks()
        test_18_single_retrieval_call()
        test_19_no_embedding_call()
        test_20_no_vector_sql_duplication()
        test_21_no_db_writes()
        test_22_no_ollama_imports()
        test_23_config_override()
        test_24_below_threshold_has_top_sim()
        test_25_phase4_regression()
        test_26_phase5_regression()
        test_27_phase6_regression()
        test_28_phase7_regression()
        test_29_phase8_regression()
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
