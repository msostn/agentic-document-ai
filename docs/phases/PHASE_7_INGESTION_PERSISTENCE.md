# PHASE 7 — DOCUMENT INGESTION & PERSISTENCE

> Status: Specification (design only — no implementation code included except tiny illustrative pseudocode).
> Audience: OpenCode (implementing agent) and the project owner (reviewer).
> Save this file as: `docs/phases/PHASE_7_INGESTION_PERSISTENCE.md`

---

## 0. Repository Rule (read this first)

**The actual repository is the source of truth, not this document.**

Before writing or modifying any code, OpenCode MUST inspect:

- `ARCHITECTURE.md`
- `docs/phases/PHASE_5_CHUNKING.md`
- `docs/phases/PHASE_6_EMBEDDINGS.md`
- `backend/app/config.py`
- `backend/app/database.py`
- `backend/app/models/` (especially `Document` and `DocumentChunk`)
- `backend/app/rag/`
- `backend/app/routes/`
- `backend/app/services/`
- `backend/scripts/`
- `requirements.txt`
- the existing test suite and its fixtures/conventions

Wherever this specification's assumptions (field names, file locations, existing helper functions) conflict with what is actually in the repository, **the repository wins**. OpenCode must report the discrepancy in its final summary rather than silently forcing this spec's assumptions into place, and must not repeat the Phase 6 mistake of assuming something exists (a dependency, a field, a helper function) without checking.

---

## 1. Phase Objective

Connect the already-working, purely in-memory pipeline:

```
PDF → pages (Phase 4) → chunks (Phase 5) → embeddings (Phase 6)
```

to persistent storage, so that after Phase 7:

```
uploaded PDF → pages → chunks → embeddings → rows in `document_chunks` (Supabase/pgvector)
```

At the end of Phase 7, a successfully ingested document has a `documents` row and a full, correctly-ordered set of `document_chunks` rows, each with a valid 384-dimensional embedding, ready for a future retrieval phase to query.

Phase 7 is a **plumbing and persistence** phase. It introduces no new intelligence — no search, no ranking, no LLM.

---

## 2. Current Architecture Context

Completed phases (assumed done; **verify against the repo**, do not re-implement):

| Phase | Delivers |
|---|---|
| 1 | FastAPI backend + React/Vite frontend scaffolding, config foundation |
| 2 | Frontend UI scaffold |
| 3 | Supabase Postgres + pgvector extension, `documents` and `document_chunks` tables, `DocumentChunk.embedding = Vector(384)` (NOT NULL), vector index intentionally **not yet created** |
| 4 | PDF upload endpoint(s), PyMuPDF page-preserving parsing, document CRUD/lifecycle endpoints, upload validation |
| 5 | Pure in-memory, per-page, word-bounded chunking; produces `ChunkRecord` objects (`document_id`, `chunk_index`, `page_number`, `content`); **no DB writes** |
| 6 | Local embedding service (`sentence-transformers/all-MiniLM-L6-v2`, 384-dim), lazy thread-safe singleton, batch-capable, order-preserving, plain Python list output; **no DB writes**, **no retrieval** |

Current data flow (in memory only, nothing persisted beyond the `documents` row and the raw file itself):

```
PDF → pages → ChunkRecord[] → embedding vectors (List[List[float]])
```

Known repository reality check (do not repeat the Phase 6 mistake): Phase 6 added `sentence-transformers` and `numpy` because they were **not** actually present despite an earlier spec assuming they were. **OpenCode must verify current `requirements.txt` state directly rather than trusting any phase document, including this one.**

Zero-cost constraint remains in force for Phase 7: no paid services, no LangChain/LangGraph, no Redis, no Celery, no distributed workers, no new heavyweight frameworks.

---

## 3. Phase 7 Scope

In scope:

1. A dedicated ingestion orchestration function/service that wires together the existing Phase 4 (parsing), Phase 5 (chunking), and Phase 6 (embedding) components.
2. Persistence of chunk + embedding data into the existing `document_chunks` table.
3. Document status/lifecycle tracking sufficient to know whether a document is ingested and retrievable.
4. Transactional guarantees: no partial/orphan chunk rows under any failure mode.
5. Wiring this orchestration into the API surface (extending the upload flow and/or adding a narrow ingestion endpoint).
6. Tests covering successful ingestion, failure modes, retries, and non-regression of Phases 4–6.

---

## 4. Explicit Out-of-Scope Items

Phase 7 must **not** implement:

- Similarity/semantic search API or retrieval service
- The pgvector similarity index (deferred — see §6.3)
- RAG orchestration, LLM calls, Ollama integration
- The agent / tool-calling layer
- Chat endpoints or chat UI
- Source citation UI
- LangChain / LangGraph or any agent framework
- Background job infrastructure (Redis, Celery, queues) — ingestion is synchronous in this phase

If, during implementation, it becomes clear that any of the above is required to make Phase 7 itself work (it should not be), OpenCode must stop and flag this rather than silently implementing it.

---

## 5. Data Flow

### 5.1 End-to-end sequence

```
Client uploads PDF
        │
        ▼
[Phase 4] Document Service: validate file, save Document row
          (status = PENDING), store raw file / extract pages
        │
        ▼
[Phase 4] PyMuPDF parsing → List[PageText]  (reuse existing function; do not reimplement)
        │
        ▼
[Phase 5] chunk_document(pages, document_id, config) → List[ChunkRecord]
        │
        ├── zero chunks? ──────────────► mark document EMPTY, stop (no chunk writes)
        │
        ▼
[Phase 6] texts = [c.content for c in chunks]
          embeddings = embed_texts(texts)   # ONE batched call, not per-chunk
        │
        ├── embedding raises? ─────────► mark document FAILED, stop (no chunk writes)
        │
        ▼
[Phase 7 – NEW] build DocumentChunk rows: zip(chunks, embeddings) preserving order
        │
        ▼
[Phase 7 – NEW] single transaction:
          insert all DocumentChunk rows
          + update document.status = READY (+ chunk_count, processed_at)
          commit
        │
        ├── DB error during insert/commit? ─► rollback, mark document FAILED
        │                                      in a separate transaction, stop
        ▼
Document is READY — retrievable by a future phase
```

### 5.2 Illustrative pseudocode (not implementation — for clarity only)

```
def ingest_document(document_id, db):
    document = load_document_or_404(db, document_id)

    if document.status == READY and not force:
        return already_ready_result(document)

    pages = get_pages_for_document(document)         # Phase 4
    chunks = chunk_document(pages, document_id)       # Phase 5

    if len(chunks) == 0:
        mark_failed(db, document, reason="no_extractable_text", status=EMPTY)
        return empty_result(document)

    try:
        texts = [c.content for c in chunks]
        embeddings = embed_texts(texts)               # Phase 6, batched
    except Exception as e:
        mark_failed(db, document, reason=str(e), status=FAILED)
        raise

    chunk_rows = build_chunk_rows(document_id, chunks, embeddings)

    try:
        delete_existing_chunks(db, document_id)       # defensive, see §7 Q15
        db.add_all(chunk_rows)
        document.status = READY
        document.chunk_count = len(chunk_rows)
        document.processed_at = now()
        db.commit()
    except Exception as e:
        db.rollback()
        mark_failed(db, document, reason=str(e), status=FAILED)  # new, separate transaction
        raise

    return success_result(document, len(chunk_rows))
```

This is intentionally schematic. Exact function names, imports, and error types are decided by OpenCode based on the actual repository conventions.

---

## 6. Database Persistence Design

### 6.1 Fields

`DocumentChunk` is expected (per project notes) to already contain at least:

- `document_id` (FK → `documents.id`)
- `chunk_index`
- `page_number`
- `content`
- `embedding` (`Vector(384)`, NOT NULL)

**OpenCode must open the actual model file and confirm the real column names, types, and any additional required fields (e.g., a primary key, timestamps) before writing the mapping code.** Do not invent or rename columns to match this document if the repository differs — adapt the ingestion code to the repository.

A chunk row must only ever be constructed once its embedding vector exists — never insert a chunk with a null or placeholder embedding.

### 6.2 Insert strategy

- Build **all** `DocumentChunk` ORM objects in memory first (pure Python, no DB calls), preserving the exact order produced by chunking/embedding.
- Insert them with a single `db.add_all(chunk_rows)` followed by one `db.commit()`, alongside the `documents` row status update, so both succeed or fail together.
- Do not call `db.add()` + `db.commit()` per chunk — this is slow and creates many small transactions, increasing the chance of partial state.
- For the current expected document sizes (single large PDFs — policies, manuals, papers), a single `add_all`/`commit` is appropriate. Do not add batching-by-N-rows logic, a job queue, or streaming inserts "just in case." If a genuinely enormous document (e.g., tens of thousands of chunks) turns out to be a real, tested problem, that is a future-phase performance concern, not a Phase 7 requirement.

### 6.3 Vector index

**Decision: do not create the pgvector similarity index in Phase 7.** Reasons:

- Phase 7 performs no similarity search — the index would be unused until Phase 8.
- Index type/parameters (e.g., `ivfflat` vs `hnsw`, `lists` count) are tuned based on retrieval query patterns, which don't exist yet.
- The user's stated preference is to keep indexing in the retrieval phase, and there is no database-level correctness reason (e.g., NOT NULL constraints, uniqueness) that forces the index to exist earlier.

If, during implementation, OpenCode discovers a genuine reason the index is needed now (unlikely), it must stop and explain why before creating it, rather than adding it silently.

---

## 7. Ingestion Orchestration Design

Direct answers to the orchestration questions, for OpenCode to implement against (adapting names/paths to what actually exists):

1. **Where does orchestration live?** A new, dedicated module: `backend/app/services/ingestion_service.py` (confirm this directory/naming convention matches existing Phase 4/5/6 services before creating it).
2. **Dedicated ingestion service?** Yes. It should not live inside the route/controller file, and it should not be bolted onto the chunker or embedder modules — those stay single-purpose.
3. **Orchestration order:** parse (Phase 4) → chunk (Phase 5) → embed (Phase 6, batched) → persist (Phase 7). Each step is a separate function call into the existing modules; the ingestion service does not reimplement any of their logic.
4. **SQLAlchemy sessions:** one session is passed into the ingestion function (or opened at the top of it, following whatever pattern Phase 3/4 already established for request-scoped sessions). The same session is used for loading the document, writing chunk rows, and updating status, so they are part of the same transaction.
5. **Batch or individual inserts?** Batch — build all rows, `add_all`, one `commit` (see §6.2).
6. **Mapping embeddings → ChunkRecord:** `embed_texts` returns a list of vectors in the same order as the input `texts` list, which itself was built by iterating `chunks` in order. Use `zip(chunks, embeddings)` (or index-based `for i, chunk in enumerate(chunks)`) to pair each `ChunkRecord` with its embedding when constructing `DocumentChunk` rows. Assert `len(chunks) == len(embeddings)` before proceeding, and treat a mismatch as a hard failure (should never happen, but guard it).
7. **Preserving `document_id`:** the `Document` row already exists (created during upload, Phase 4) before ingestion runs. Its primary key is passed into `chunk_document(...)` (Phase 5 already accepts `document_id`) and copied onto every `DocumentChunk` row at construction time. Ingestion never creates a new `Document` row itself.
8. **Preserving page numbers / chunk indexes:** these come directly from `ChunkRecord.page_number` and `ChunkRecord.chunk_index` (Phase 5 output) and are copied verbatim onto `DocumentChunk` rows — no recomputation.
9. **Zero chunks produced:** treat as a distinct, non-exceptional outcome (not a crash). Mark the document with a status indicating no content was extracted (see §10) and return a result the caller can surface to the user (e.g., "This document appears to contain no extractable text — it may be a scanned image PDF"). No chunk rows are written.
10. **Embedding generation fails:** treat as a hard failure. No chunk rows are written. Document status is set to FAILED with a stored error message. The original exception is still raised/logged so the API layer can return an appropriate error response.
11. **Database insertion fails:** roll back the transaction that contained the chunk inserts and the status update. In a **new, separate transaction**, set the document status to FAILED with an error message (see §8 for why this must be a separate transaction). Re-raise so the API layer returns a 5xx.
12. **Transactional?** Yes — chunk inserts and the READY status update are one atomic unit (see §8).
13. **Preventing partial writes:** by construction — all `DocumentChunk` objects are built in memory before any `db.add`/`commit` call, and the insert + status update happen in a single transaction that is either fully committed or fully rolled back.
14. **Retried ingestion:** allowed and expected to be safe. See Q15 and §9 for the exact idempotency policy.
15. **Avoiding duplicate/orphan chunks:** because of atomic commit, a truly partial write should never happen. As defense-in-depth (e.g., someone retries after the process was killed mid-flight in some future async version), the ingestion function deletes any existing `document_chunks` rows for `document_id` before inserting the new set, whenever ingestion actually runs (i.e., it treats itself as "recompute the full chunk set for this document," not "append").
16. **Document status updates during ingestion?** Yes — see §10.
17. **What happens to the uploaded PDF after parsing?** Phase 7 does not change Phase 4's file storage behavior. If Phase 4 already persists the uploaded file (disk or otherwise) and this ingestion step runs within the same request using pages already extracted in memory, no re-parsing or file handling change is needed. If Phase 4 does *not* keep the file or the extracted pages around long enough for a decoupled ingestion step (see §11 API boundary options), ingestion must re-open the stored file via the existing Phase 4 parsing function — inspect the repository to determine which situation applies before deciding.

---

## 8. Transaction and Rollback Strategy

Core principle: **the chunk rows and the "ingestion succeeded" status update are two effects that must happen together, and the "ingestion failed" status update is a fact that must survive the rollback.**

This means two separate transactions, not one:

1. **Transaction B (the work):** delete-existing-chunks-for-this-document (if any) + insert new chunk rows + set `status=READY` (+ `chunk_count`, `processed_at`). Committed together. On any exception here, `db.rollback()` is called, which undoes all of it, including the status change attempted inside it.
2. **Transaction C (the failure record):** only entered if Transaction B failed. A fresh, small transaction that sets `status=FAILED` and stores an error message, then commits. This must be structurally separate from Transaction B — if it were inside the same transaction/session state that was just rolled back, the failure status would be rolled back too, and the document would be left looking like it's still `PENDING` with no explanation.

Practical implication for OpenCode: after calling `db.rollback()`, make sure the session is in a clean state (per whatever session-management pattern the repo already uses — e.g., a fresh session, or the same session after rollback if that's safe with the existing setup) before writing the FAILED status.

The document row itself (`status=PENDING`) is created earlier, during upload (Phase 4), in its own already-committed transaction — Phase 7 does not need to (and should not) re-architect that part.

---

## 9. Error Handling

| Condition | Chunks written? | Document status | API-visible behavior |
|---|---|---|---|
| Successful parse/chunk/embed/persist | Yes, all of them | `READY` | 200/201 with success payload (chunk count, status) |
| Zero chunks after chunking (e.g., scanned/image-only PDF, empty file) | No | `EMPTY` (or equivalent — see §10) | Success-shaped response indicating no content was extracted, not a server error |
| Embedding generation raises | No | `FAILED` + error message stored | 5xx (or documented error shape) with a clear message; no chunk rows exist |
| DB error during chunk insert/commit | No (rolled back) | `FAILED` + error message stored | 5xx; no partial chunk rows exist |
| Re-ingestion requested while status is already `READY` | No (no-op) unless a `force=true` flag/param is passed | unchanged (or `READY` again after a forced re-run) | 200 with a "already ingested" style response, or full re-run if forced |
| Re-ingestion requested while status is `FAILED`/`EMPTY`/`PENDING` | Yes, on retry | `READY`, `FAILED`, or `EMPTY` depending on outcome | Same as a fresh ingestion |

All error messages stored on the document should be short, non-sensitive strings suitable for display (e.g., "No extractable text found in document" or "Embedding generation failed: <short reason>") — do not persist full stack traces into a user-facing column; log those separately via the project's existing logging setup instead.

---

## 10. Document Lifecycle / Status Behavior

**First step: inspect the actual `Document` model.** Determine whether it already has some/all of: `status`, `filename`, `page_count`, `chunk_count`, `created_at`/`updated_at`, `processed_at`, `error_message`.

- **If these fields already exist**, reuse them exactly as-is. Only extend an existing status enum if the existing values genuinely cannot represent Phase 7's states, and explain why in the implementation summary.
- **If a `status` field does not exist at all**, this is a genuinely required minimal schema addition, because without it there is no way to know whether a document is safe to retrieve from later, or whether ingestion succeeded, failed, or is still pending. Add:
  - `status` (string or small enum: `pending`, `ready`, `failed`, `empty` — reuse `PROCESSING` only if a decoupled/async endpoint path is actually used, see §11)
  - `error_message` (nullable text, short)
  - `chunk_count` (nullable integer)
  - `processed_at` (nullable timestamp)
- Do **not** add unrelated convenience fields "while you're in there." Only what Phase 7 needs to function correctly.
- Follow whatever schema-change mechanism Phase 3 already used to create `documents`/`document_chunks` (raw SQL script under `backend/scripts/`, SQLAlchemy `metadata.create_all`, etc.) — do not introduce Alembic or another migration framework unless the repository already uses one. If it's genuinely unclear, report this and propose the smallest change consistent with Phase 3's approach.

Status transitions:

```
PENDING  →  READY     (successful ingestion, chunks > 0)
PENDING  →  EMPTY     (zero extractable chunks)
PENDING  →  FAILED    (embedding or DB error)
READY    →  READY     (forced re-ingestion succeeds again)
FAILED   →  READY/FAILED/EMPTY  (retry)
EMPTY    →  READY/FAILED/EMPTY  (retry, e.g. after re-parsing improvements)
```

---

## 11. API Boundary

**Decision: extend the existing Phase 4 upload endpoint to trigger ingestion synchronously (Option A), and additionally expose a narrow, explicit ingestion endpoint for retries/testing.**

Rationale:

- The desired UX is "upload → parsed/chunked/embedded/persisted → ready," in one user-facing action. Extending the upload endpoint delivers that directly without inventing a second required client-side step.
- If parsing already happens in-memory during the upload request (per Phase 4), calling ingestion in the same request avoids re-opening/re-parsing the file, which is simpler and cheaper.
- A separate `POST /documents/{document_id}/ingest` endpoint (calling the same `ingest_document(...)` service function) is added regardless, so that: retries after a `FAILED`/`EMPTY` result don't require re-uploading the file, tests can exercise ingestion in isolation from the upload/validation logic, and this becomes the natural seam for a future async version (Phase 8+ could make *this* endpoint fire-and-forget without touching the upload endpoint's contract).

**Fallback if the repository disagrees:** if, on inspection, the Phase 4 upload endpoint already defers work (e.g., it currently returns before parsing completes, or parsing/pages are not available in the same request context for some reason), do not force Option A. Instead, keep the upload endpoint as-is and rely on the new `POST /documents/{document_id}/ingest` endpoint as the sole trigger (Option B), with the frontend (unchanged in this phase) simply not calling it yet — that wiring is a later concern. If this fallback is used, state clearly in the implementation summary why Option A was not chosen.

Either way, the ingestion **logic itself lives in one place** (`ingestion_service.py`), never duplicated between the upload route and the ingest route.

---

## 12. File-by-File Implementation Plan

*(Paths below are best guesses per the stated project structure — confirm each against the actual repository before creating/modifying.)*

- **New:** `backend/app/services/ingestion_service.py` — orchestration function(s): `ingest_document`, and small private helpers (`_build_chunk_rows`, `_persist_success`, `_mark_failed`, etc.).
- **Modify:** the Phase 4 upload route (likely `backend/app/routes/documents.py`) — after the existing Document-row-creation logic, call `ingestion_service.ingest_document(...)` and include ingestion outcome (status, chunk_count) in the response.
- **New (same route file or a new one):** `POST /documents/{document_id}/ingest` endpoint calling the same service function, for retries/testing.
- **Modify (only if fields are missing):** `backend/app/models/document.py` (or wherever `Document` is defined) — add `status`/`error_message`/`chunk_count`/`processed_at` only if genuinely absent (§10).
- **Modify (only if schema changed):** the schema-creation mechanism used by Phase 3 (raw SQL script in `backend/scripts/`, or equivalent), to add any new columns.
- **Modify (if a response schema exists):** `backend/app/schemas/document.py` (or equivalent Pydantic schema) — surface `status`/`chunk_count`/`error_message` in API responses if they're new.
- **New:** a Phase 7 test file following the existing naming convention (e.g., `backend/tests/test_ingestion.py` or `test_phase7_ingestion.py` — match whatever Phase 5/6 used).
- **Optional but recommended:** update `ARCHITECTURE.md` to reflect the now-persisted pipeline stage.
- **This document** should be saved as `docs/phases/PHASE_7_INGESTION_PERSISTENCE.md`.

Do not touch Phase 5's chunker internals, Phase 6's embedding service internals, or the frontend, beyond what's explicitly listed above.

---

## 13. Function/Class Responsibilities

- `ingest_document(document_id, db, *, force: bool = False) -> IngestionResult`
  Loads the document; short-circuits if already `READY` and `force` is false; runs parse→chunk→embed; on zero chunks marks `EMPTY` and returns; on embedding failure marks `FAILED`, re-raises; otherwise builds chunk rows, deletes any pre-existing chunks for this `document_id`, inserts the new set and marks `READY` in one transaction; on DB failure rolls back and marks `FAILED` in a separate transaction, re-raises. Returns a small result object (status, chunk_count, error_message).

- `_build_chunk_rows(document_id, chunks: list[ChunkRecord], embeddings: list[list[float]]) -> list[DocumentChunk]`
  Pure function, no DB access. Asserts equal lengths, zips in order, constructs ORM objects. Easily unit-testable in isolation.

- `_mark_failed(db, document, reason: str, status=FAILED) -> None`
  Opens/uses a fresh transaction to set status + error message and commit, independent of any transaction that was just rolled back.

- `IngestionResult` (simple dataclass or Pydantic model): `status`, `chunk_count`, `error_message: str | None`.

Exact signatures should follow the repository's existing conventions (type hints style, sync vs. session-per-request pattern, naming) rather than this document's guesses.

---

## 14. Configuration Changes

None are expected to be required. Ingestion reuses:

- Phase 5's existing chunk-size/overlap configuration
- Phase 6's existing embedding batch-size configuration

If OpenCode finds a genuine need for a new setting (e.g., a toggle for whether upload auto-triggers ingestion), it should be added to the existing `Pydantic Settings` config object following its current pattern, with a sensible default, and called out explicitly in the implementation summary — do not add it silently.

---

## 15. Dependency Changes

None expected. Phase 7 uses only what Phases 3, 4, and 6 already require: FastAPI, SQLAlchemy, psycopg, pgvector, PyMuPDF, sentence-transformers, numpy.

Before finalizing, OpenCode must check `requirements.txt` directly (per the Phase 6 lesson) rather than assuming these are present, and report if anything expected is actually missing rather than silently adding packages.

Do not add an ORM migration framework (e.g., Alembic) unless one is already in use.

---

## 16. Test Plan

Follow the repository's existing test conventions (fixtures for a test DB session, a sample small multi-page PDF fixture, etc. — reuse Phase 4/5/6 fixtures rather than duplicating them). Use the real local embedding model for embedding-dependent tests, as already established in Phase 6; do not mock away the core behavior being tested.

Suggested test functions (names indicative, adapt to convention):

1. `test_ingest_creates_document_and_chunk_rows` — ingest a small known multi-page PDF; assert a `documents` row and the expected number of `document_chunks` rows exist.
2. `test_ingest_chunk_content_matches_chunker_output` — chunk content in the DB matches what Phase 5's chunker would produce for the same input.
3. `test_ingest_page_numbers_correct` — each persisted chunk's `page_number` matches its source page.
4. `test_ingest_chunk_indexes_sequential_and_ordered` — `chunk_index` values are correct and monotonically increasing in insertion/query order.
5. `test_ingest_embeddings_persisted_and_384_dim` — every persisted `embedding` is non-null and has length 384.
6. `test_ingest_embeddings_match_order` — embedding for chunk *i* corresponds to chunk *i*'s content (not shuffled).
7. `test_ingest_document_id_foreign_key_correct` — all chunk rows reference the correct `document_id`.
8. `test_ingest_marks_document_ready` — status transitions to `READY` with a populated `chunk_count`/`processed_at`.
9. `test_ingest_empty_document_no_chunks` — a PDF with no extractable text results in zero chunk rows and an `EMPTY`-equivalent status, not a crash.
10. `test_ingest_embedding_failure_rolls_back` — monkeypatch/force the embedding step to raise; assert zero chunk rows exist afterward and status is `FAILED` with an error message.
11. `test_ingest_db_failure_rolls_back` — force a DB-level failure during insert (e.g., a monkeypatched commit that raises, or an intentionally invalid row); assert zero partial chunk rows and status is `FAILED`.
12. `test_ingest_retry_after_failure_succeeds` — after a forced failure, calling ingestion again on the same `document_id` succeeds cleanly with no duplicate/orphan rows.
13. `test_ingest_reingest_ready_document_is_noop_without_force` — calling ingestion again on an already-`READY` document without `force=True` does not duplicate chunks.
14. `test_ingest_reingest_with_force_replaces_chunks` — with `force=True`, old chunks are removed and replaced, not appended to.
15. `test_upload_endpoint_response_shape_unchanged_or_extended_compatibly` — existing Phase 4 upload contract still works; any new fields are additive.
16. Full existing Phase 4 test suite still passes.
17. Full existing Phase 5 test suite still passes.
18. Full existing Phase 6 test suite still passes.

No external paid services are used in any test. Tests that need a Postgres+pgvector instance should use whatever local/test database setup Phase 3/4 already established (do not introduce SQLite as a substitute, since it can't represent `Vector(384)`).

---

## 17. Acceptance Criteria

Phase 7 is complete when:

- [ ] Uploading a real, small, multi-page, text-based PDF results in a `documents` row with status `READY` and the correct `chunk_count`.
- [ ] The corresponding `document_chunks` rows exist, each with correct `document_id`, `chunk_index`, `page_number`, `content`, and a 384-dimensional, non-null `embedding`.
- [ ] Chunk ordering in the database matches the original chunking order.
- [ ] A document with no extractable text produces zero chunk rows and a clearly distinguishable, non-`READY` status — not an unhandled exception.
- [ ] A forced embedding failure leaves zero chunk rows for that document and sets status `FAILED` with an error message.
- [ ] A forced database failure during persistence leaves zero partial chunk rows and sets status `FAILED`.
- [ ] Retrying ingestion after a failure succeeds without leaving duplicate or orphaned chunk rows.
- [ ] Re-ingesting an already-`READY` document without a force flag is a safe no-op; with a force flag, it fully replaces the chunk set.
- [ ] No similarity search, retrieval, agent, LLM, or chat functionality has been introduced.
- [ ] The pgvector similarity index has not been created in this phase (unless explicitly justified otherwise).
- [ ] All Phase 4, 5, and 6 tests still pass unmodified in behavior.
- [ ] All new Phase 7 tests pass.
- [ ] No new paid dependency or service has been introduced.
- [ ] `git diff` shows only Phase-7-scoped changes; nothing has been committed.

---

## 18. Manual Verification Steps

1. Start the backend locally with the existing dev workflow.
2. Upload a small, real, text-based, multi-page PDF (e.g., 3–5 pages) via the upload endpoint (curl, the API docs UI, or the frontend if wired up).
3. Confirm the response indicates success and includes a chunk count.
4. Query the database directly:
   ```sql
   select id, status, chunk_count, processed_at from documents order by created_at desc limit 1;
   select chunk_index, page_number, left(content, 50), vector_dims(embedding)
   from document_chunks
   where document_id = '<the document id>'
   order by chunk_index;
   ```
5. Confirm `vector_dims(embedding)` is `384` for every row, and `chunk_index` values are sequential starting at 0 (or 1, per existing convention).
6. Upload a PDF with no extractable text (e.g., a scanned image with no OCR layer, or a blank PDF). Confirm the document ends up in the `EMPTY`-equivalent status with zero chunk rows, and the API response communicates this clearly rather than erroring opaquely.
7. Call the retry/ingest endpoint again on the same document_id that previously succeeded, without a force flag; confirm no duplicate chunks appear.
8. Call it again with a force flag (if implemented); confirm the old chunks are gone and a fresh, equally-sized set exists.
9. Confirm existing document listing/get/delete endpoints (Phase 4) still behave as before.

---

## 19. Phase 8 Integration Boundary

Phase 8 (retrieval) can rely on the following contract, and nothing more, from Phase 7:

- Any `documents` row with `status = READY` has one or more corresponding `document_chunks` rows.
- Every such `document_chunks` row has a non-null, 384-dimensional `embedding`, plus correct `document_id`, `chunk_index`, `page_number`, and `content`.
- A document that is not `READY` (`PENDING`, `FAILED`, `EMPTY`) should not be treated as searchable by a future retrieval phase; Phase 8 is responsible for checking/enforcing this, not Phase 7.
- No vector index exists yet — Phase 8 owns deciding on and creating it based on real retrieval query patterns.

Phase 7 exposes no retrieval-shaped function; Phase 8 will build its own search function directly against `document_chunks` using this data.

---

## 20. Risks / Edge Cases

- **Long-running synchronous requests:** embedding a very large document inside a single HTTP request could be slow. Acceptable for this MVP phase; not solved here. Flag as a known limitation for a future async phase rather than solving it now.
- **Concurrent ingestion requests for the same document:** two simultaneous calls to ingest the same `document_id` could race. Not addressed with locking in this phase (out of scope for a zero-infra MVP) — note it as a known limitation.
- **Scanned/image-only PDFs:** likely produce zero extractable text; handled via the `EMPTY` path, not a crash.
- **Malformed/corrupt PDFs:** should already be rejected by Phase 4 validation; Phase 7 assumes it only ever receives documents that passed that validation.
- **Very large chunk counts:** a single `add_all`/commit should still work for realistic document sizes (policies, manuals, papers); if this becomes a real, measured problem, that's a future optimization, not a Phase 7 requirement.
- **Embedding model memory/CPU cost:** already addressed by Phase 6's batching and singleton design; Phase 7 just needs to call it correctly (one batched call, not per-chunk).
- **Schema drift risk:** if the actual `Document`/`DocumentChunk` models differ meaningfully from what's assumed here, OpenCode must adapt to reality and report the difference rather than forcing this spec's field names.

---

## 21. Exact OpenCode Implementation Instructions

1. Inspect the repository files listed in §0 before writing any code. Confirm or correct every assumption in this document (model fields, existing service function names/signatures, test conventions, upload endpoint behavior) against what's actually there.
2. If something in this spec conflicts with the real repository, follow the real repository and note the discrepancy in your final summary.
3. Implement **Phase 7 only** — nothing from §4's out-of-scope list.
4. Create/modify only the files described in §12, adjusted for actual paths/names found in step 1.
5. Implement the two-phase transaction strategy from §8 exactly (success transaction vs. separate failure-recording transaction) — this is the part most likely to be gotten wrong if rushed.
6. Write and run the full test suite described in §16, including confirming Phases 4, 5, and 6 tests still pass unmodified.
7. Verify no Phase 8+ functionality (search, retrieval, agent, LLM, chat, vector index) was introduced, even incidentally.
8. Run `git diff` and review it for scope creep.
9. Run `git diff --check` to catch whitespace/conflict-marker issues.
10. Run `git status` to confirm only the expected files changed.
11. Do **not** commit. Leave the working tree for the project owner to review and commit.
12. Provide a final summary that includes: files changed, any schema changes made and why, any point where this specification was adapted to match repository reality, and confirmation that all tests (existing + new) pass.
