# PHASE 8 — SEMANTIC VECTOR RETRIEVAL

> Status: SPECIFICATION ONLY. No code has been written. This document is intended to be saved as `docs/phases/PHASE_8_SEMANTIC_RETRIEVAL.md` and handed to the implementing coding agent ("OpenCode").

---

## 0. Repository-Reality Rule (read first)

This specification was written **without direct access to the actual repository**. It is derived strictly from the Phase 1–7 summaries provided by the project owner. Every field name, function name, module path, and settings key below is a **best-effort convention**, not a guarantee.

**Before writing any code, OpenCode MUST:**

1. Read `ARCHITECTURE.md` and `docs/phases/PHASE_5_CHUNKING.md`, `PHASE_6_EMBEDDINGS.md`, `PHASE_7_INGESTION_PERSISTENCE.md`.
2. Read `backend/app/config.py`, `backend/app/database.py`, `backend/app/models/`, `backend/app/rag/`, `backend/app/routes/`, `backend/app/services/`, `backend/scripts/`.
3. Read `requirements.txt` and existing tests.
4. Reconcile every named entity in this spec (model field names, exception names, config keys, module paths, HTTP paths) against what actually exists.

**Wherever this document's assumed name conflicts with the real repository, the repository wins.** This document flags every such assumption explicitly with `[VERIFY]`.

---

## 1. Phase Objective

Introduce the first retrieval capability of the system: given a `document_id` and a natural-language `query`, return the top-k most semantically relevant chunks of that specific document, using the embeddings already persisted in Phase 7 and the embedding model already implemented in Phase 6.

Phase 8 produces **retrieved context**, not an answer. It is the bridge between "chunks with embeddings sitting in Postgres" and "an LLM/agent that will eventually consume them."

---

## 2. Current Architecture Context

Persisted pipeline as of Phase 7:

```
PDF → PyMuPDF → pages → Phase 5 chunker → ChunkRecord[] → Phase 6 embedding service → document_chunks (Supabase Postgres + pgvector)
```

Each `READY` document has, per chunk, at minimum:
- `document_id`
- `chunk_index`
- `page_number`
- `content`
- `embedding` (`Vector(384)`, `NOT NULL`)

Phase 3 explicitly deferred creating a vector similarity index. Phase 8 is the first phase that queries `document_chunks` by vector similarity, so it is also the natural point to add that index.

No retrieval, no query embedding, and no similarity search exist yet. Phase 8 adds exactly these three things and nothing else.

---

## 3. Phase 8 Scope

In scope:
- A retrieval service function that takes `(db, document_id, query, top_k)` and returns ranked chunk results.
- Query embedding via the existing Phase 6 singleton embedding service (no second embedding path).
- A single pgvector similarity query, scoped to one document, executed in the database.
- A pgvector index (HNSW, see §9) to make that query efficient.
- Document-status gating (only `READY` documents are searchable).
- Centralized, validated `top_k` configuration.
- Query validation (empty/whitespace/oversized).
- A narrow, optional HTTP endpoint for manual verification.
- Tests, including index-existence verification and cross-document isolation proof.

Out of scope: see §4.

---

## 4. Explicit Out-of-Scope Items

Phase 8 MUST NOT implement any of the following. If OpenCode finds itself writing code for any of these, it should stop and flag it:

- LLM calls, Ollama integration, or any generation step
- RAG "answer" construction, prompt templates for final answers
- Agent or tool-calling logic
- Chat endpoints, conversation memory, multi-turn state
- Source-citation UI or frontend chat components
- LangChain / LangGraph
- Reranking models, hybrid search, BM25/keyword search
- Query expansion or multi-query retrieval
- Redis, Celery, or any background worker
- A new/second embedding implementation
- Alembic or any new migration framework
- Any Python-side manual cosine-similarity computation over chunks loaded into memory

The only output of Phase 8 is: **query → relevant document chunks.**

---

## 5. Retrieval Data Flow

```
Caller (service call or HTTP route)
   │
   ├─ 1. Look up document by document_id
   │       → not found?            → DocumentNotFoundError
   │       → status != READY?      → DocumentNotReadyError / DocumentIngestionFailedError / DocumentEmptyError
   │
   ├─ 2. Validate query string (empty / whitespace / too long) → InvalidQueryError
   │
   ├─ 3. Resolve top_k (default / bounds) → InvalidTopKError
   │
   ├─ 4. Embed query via Phase 6 embedding service singleton
   │       → produces a single 384-dim vector
   │
   ├─ 5. Execute ONE pgvector similarity query:
   │       WHERE document_chunks.document_id = :document_id
   │       ORDER BY embedding <=> :query_embedding
   │       LIMIT :top_k
   │
   └─ 6. Map rows → RetrievalResult[] → return to caller
```

Exactly one embedding call and exactly one database query per retrieval request (see §17, Performance).

---

## 6. Query Embedding Flow

The query MUST be embedded using the exact same singleton object introduced in Phase 6 (`sentence-transformers/all-MiniLM-L6-v2`, 384 dimensions, lazy thread-safe singleton). Do not instantiate a second model, and do not re-implement batching logic for a single string.

`[VERIFY]` Phase 6's public embedding interface. It is described as "batch embedding, order-preserving." OpenCode must confirm the exact callable, e.g. something shaped like:

```
embedding_service.embed([query])[0]   # if the service only exposes batch embedding
# or
embedding_service.embed_query(query)  # if a single-item convenience method exists
```

Whichever exists in the repository, Phase 8 must call it **as-is** — do not add a new method to the embedding service unless a single-string convenience wrapper is genuinely absent, in which case add the smallest possible wrapper (e.g. `embed_one(text) -> np.ndarray`) that delegates to the existing batch method, rather than duplicating model-loading or normalization logic.

If Phase 6 applies any normalization (e.g., L2-normalizing embeddings before storage), the query embedding must go through the identical normalization path, or cosine distance comparisons will be meaningless.

---

## 7. pgvector Similarity-Search Design

The search is a single SQLAlchemy-expressed query against `document_chunks`, filtered by `document_id` and ordered by vector distance, with a `LIMIT`.

Conceptual SQLAlchemy (illustrative, not final code — adapt to actual model/session patterns):

```python
distance_expr = DocumentChunk.embedding.cosine_distance(query_embedding)

rows = (
    db.query(DocumentChunk, distance_expr.label("distance"))
    .filter(DocumentChunk.document_id == document_id)
    .order_by(distance_expr)
    .limit(top_k)
    .all()
)
```

`[VERIFY]` The pgvector-python version in `requirements.txt` supports the `.cosine_distance()` comparator on the SQLAlchemy `Vector` column (available in `pgvector` >= 0.2.x SQLAlchemy integration). If the installed version predates comparator support, fall back to raw operator syntax via `text()`/`literal_column()` using the `<=>` operator, scoped with a bound parameter — never string-format the query embedding into SQL.

The `WHERE document_id = :document_id` clause and the `ORDER BY` clause are part of the **same single query**. This is what guarantees document isolation happens in the database, not in Python (see §10).

---

## 8. Distance / Similarity Semantics

- **Operator used:** pgvector's cosine distance operator `<=>`, exposed via `.cosine_distance()` in SQLAlchemy.
- **Definition:** pgvector's `<=>` returns `1 - cosine_similarity`. Therefore `distance = 0` means identical direction; `distance = 2` is maximally dissimilar; higher = less similar.
- **Canonical field returned to callers:** `distance` (raw pgvector cosine distance, float).
- **Convenience field also returned:** `similarity = 1 - distance`, computed in Python purely as a presentation transform on the already-returned scalar — this is NOT a second similarity computation over vectors, just arithmetic on the single float already returned by Postgres.
- **No other terminology** ("score", "relevance", "confidence") should be introduced without clearly mapping it to one of the two fields above. Internal code and API responses should consistently use `distance` and `similarity` fields, defined as above, and docstrings/response schema descriptions must state the direction of "better" explicitly (lower distance / higher similarity = more relevant).

---

## 9. Vector Index Decision and Design

**Decision: HNSW, over IVFFlat, for this MVP.**

Rationale:
- IVFFlat requires choosing a `lists` parameter tuned to expected row count, and its recall quality is poor until the index is trained on a representative amount of data — awkward for a system whose table size is small and grows incrementally per upload.
- HNSW builds incrementally, needs no `lists` tuning, and gives good recall immediately, which matches an MVP with an unknown/small/growing number of chunks.
- Both index types are ANN indexes over the *whole* `document_chunks` table; because Phase 8 always filters by `document_id` in the same query, for small per-document chunk counts Postgres's planner may reasonably choose a sequential scan instead of the index for a given document. This is expected and acceptable at MVP scale (see §26, Risks) — the index still matters as the corpus grows across many documents.

**DDL concept** (exact SQL, conceptual — apply via whatever mechanism Phase 3 used to create tables/extensions, see below):

```sql
CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw_cosine
ON document_chunks
USING hnsw (embedding vector_cosine_ops);
```

- **Index type:** HNSW
- **Distance metric / operator class:** `vector_cosine_ops` (must match the `.cosine_distance()` / `<=>` operator used in queries — mismatched operator class means the index cannot be used for this query shape)
- **Column:** `document_chunks.embedding`
- **Uses default HNSW build parameters** (`m`, `ef_construction`) — do not introduce a tuning strategy; MVP scale does not warrant it. If pgvector's installed version requires explicit parameters, use its documented defaults, not custom values.

**How it is created:** `[VERIFY]` Phase 3 must already have some mechanism that created `document_chunks`, `documents`, and enabled the `pgvector` extension (likely a startup schema-init routine, e.g. inside `backend/app/database.py`, or a one-off script in `backend/scripts/`). OpenCode should:
1. Locate that exact mechanism.
2. Add the `CREATE INDEX IF NOT EXISTS ...` statement to it, using the same idempotent style already used for tables/extensions (so re-running init is always safe).
3. Do NOT introduce Alembic or any other migration framework to do this — Phase 3 already established the schema-management pattern; Phase 8 extends it, it doesn't replace it.
4. If the existing mechanism only runs once at first-ever setup and there is no "re-run against an existing database" story, add a minimal idempotent script (e.g. `backend/scripts/create_vector_index.py`) that connects using the existing `database.py` engine/session and executes the DDL with `IF NOT EXISTS`, callable manually and safe to re-run.

**No index migration framework, no downtime strategy, no tuning strategy** — this is intentionally the simplest correct MVP index.

---

## 10. Document Isolation Strategy

Document isolation is the single most important correctness/security property of Phase 8.

Rule: `document_id` filtering happens **inside** the same SQL statement that performs the similarity ordering — never as a post-filter in Python, never as a separate "fetch all, then narrow" step.

Concretely:
- The retrieval service accepts `document_id` as a required, explicit parameter.
- The SQLAlchemy query in §7 always has `.filter(DocumentChunk.document_id == document_id)` composed with the same `.order_by(distance_expr)` in a single query object, so Postgres itself never considers rows belonging to other documents as candidates.
- The service never issues a global (unfiltered) similarity query under any code path, including error/fallback paths.
- This is tested explicitly (see §23, test #5) with two documents whose chunks are semantically close enough that a bug would plausibly leak results across documents — a top-k search on Document A must never return a Document B chunk, and vice versa, regardless of query content.

---

## 11. Document Status Handling

`[VERIFY]` Exact lifecycle states and field name from Phase 7 (assumed here: `Document.status` is one of `PENDING`, `PROCESSING`, `READY`, `FAILED`, `EMPTY`, or repository's real equivalents/casing — e.g. it may be a string, an Enum, or use different names like `INGESTING`/`COMPLETE`). Map behavior to whatever the real states are:

| Actual document state | Retrieval behavior |
|---|---|
| `READY` (or equivalent success state) | Proceed with retrieval |
| `PENDING` / `PROCESSING` (or equivalent) | Reject: not yet ready |
| `FAILED` (or equivalent) | Reject: ingestion failed |
| `EMPTY` (or equivalent "no content extracted") | Reject: no searchable content |
| document_id not found at all | Reject: not found |

Proposed service-level exceptions (module-level, defined alongside the retriever, or in an existing `app/rag/exceptions.py` if the repo already has an exceptions module pattern — `[VERIFY]`):

- `DocumentNotFoundError`
- `DocumentNotReadyError` (carries the actual current status for a useful error message)
- `DocumentIngestionFailedError`
- `DocumentEmptyError`

These are distinct from the "READY but zero chunks unexpectedly" case in §13, which is NOT an exception — it's a normal empty-result response, because the document's status legitimately claims readiness even if something anomalous left it without chunks.

The route layer (§16) maps these to HTTP status codes, e.g.: not found → 404; not ready / failed / empty → 409 (conflict with current resource state) or 422 — `[VERIFY]` and follow whatever status-code convention Phase 7's existing document routes already use for similar lifecycle rejections, for consistency.

---

## 12. Top-K Configuration and Validation

**Defaults (centralize in the existing Pydantic Settings pattern — `[VERIFY]` naming convention in `backend/app/config.py`):**

- `RETRIEVAL_TOP_K_DEFAULT = 5`
- `RETRIEVAL_TOP_K_MAX = 20`

**Behavior:**

| Input | Behavior |
|---|---|
| `top_k` omitted / `None` | Use `RETRIEVAL_TOP_K_DEFAULT` (5) |
| `top_k = 1` | Valid; return up to 1 result |
| Valid larger `top_k` (`1 < top_k <= MAX`) | Valid; return up to that many |
| `top_k = 0` | Rejected — `InvalidTopKError`; zero is not a meaningful request and is more likely a caller bug than an intent to receive nothing |
| Negative `top_k` | Rejected — `InvalidTopKError` |
| `top_k > RETRIEVAL_TOP_K_MAX` | Rejected — `InvalidTopKError`, with the max stated in the message |

Design choice: **reject rather than silently clamp** an out-of-range `top_k`. Silent clamping hides caller bugs (e.g., a frontend accidentally requesting `top_k=100000`) behind a "successful" response that looks fine but isn't what was asked for. If the existing repository already has an established clamp-not-reject convention elsewhere for similar bounded parameters, follow that convention instead for consistency — `[VERIFY]`.

---

## 13. Query Validation

| Input | Behavior |
|---|---|
| Empty string `""` | Rejected — `InvalidQueryError` |
| Whitespace-only (`"   "`, after `.strip()`) | Rejected — `InvalidQueryError` |
| Normal query | Proceed |
| Very long query | See below |

Reuse whatever validation Phase 6's embedding service already performs (`[VERIFY]` — Phase 6 is described only as "batch embedding, order-preserving," so it's unclear whether it validates empty strings itself). Do not duplicate tokenizer-level truncation logic: `sentence-transformers` models already truncate input internally to the model's max sequence length, and Phase 8 should rely on that rather than reimplementing token counting.

Phase 8 adds one additional, simple guard **above** the embedding layer: reject queries exceeding a configurable character cap (e.g. `MAX_QUERY_LENGTH = 8000` characters, centralized in Settings) with `InvalidQueryError`, purely to reject obviously-abusive payloads before they reach the model — not to enforce semantic truncation, which the model already does.

---

## 14. No-Result Behavior

All of the following return a **valid, empty `results: []` response** — never an exception, never a 500:

- Document is `READY` but has zero chunks (an anomaly, not a status rejection — see §11).
- `top_k` exceeds the number of chunks actually available for that document (SQL `LIMIT` naturally returns fewer rows; no error).
- The similarity query executes successfully but returns zero rows for any other reason.

Only the categories in §11 (bad document_id / bad status) and §§12–13 (bad `top_k` / bad query) are treated as request errors. Every other "nothing came back" case is a normal, empty result.

---

## 15. Retrieval Result Model

A small, explicit result type — a `dataclass` or `pydantic.BaseModel`, matching whatever style `app/rag/` already uses (`[VERIFY]`):

```python
class RetrievalResult:
    chunk_id: Optional[int | UUID]   # only if DocumentChunk actually exposes a PK — [VERIFY]
    document_id: int | UUID          # [VERIFY] actual PK type used in Phase 3/7 models
    chunk_index: int
    page_number: int
    content: str
    distance: float                  # raw pgvector cosine distance, 0 = identical
    similarity: float                # 1 - distance, for convenience
```

- `chunk_id` is included only if `DocumentChunk` actually has a natural primary key field to expose (`[VERIFY]` — Phase 5's `ChunkRecord` as described does not list an `id`, but the persisted `document_chunks` table likely has one; use whatever the ORM model actually calls it).
- The raw embedding vector is **never** included in the result object or any API response — it's large, not useful to callers, and the security section explicitly prohibits exposing it without a compelling reason.

---

## 16. Retrieval Service Design

**Location:** `backend/app/rag/retriever.py` (matches the existing `backend/app/rag/` module referenced for repository inspection) — `[VERIFY]` this folder's existing contents/conventions (e.g., does it already hold the embedding service from Phase 6? If so, follow its import/singleton-access pattern exactly).

**Primary entry point (conceptual signature — adapt to repo conventions for session typing, ID typing, and sync/async):**

```python
def retrieve_relevant_chunks(
    db: Session,
    document_id: <repo's actual ID type>,
    query: str,
    top_k: Optional[int] = None,
) -> list[RetrievalResult]:
    ...
```

**Internal responsibilities, as small private helpers within the same module (not necessarily separate files):**

- `_get_ready_document(db, document_id) -> Document` — fetch document, raise the appropriate status exception from §11.
- `_validate_query(query: str) -> str` — strip/validate per §13, return the cleaned query.
- `_resolve_top_k(top_k: Optional[int]) -> int` — apply defaults/bounds per §12.
- `_embed_query(query: str) -> np.ndarray` — delegate to the Phase 6 singleton per §6.
- `_run_similarity_query(db, document_id, query_embedding, top_k) -> list[RetrievalResult]` — the single SQLAlchemy query from §7, mapped into result objects with `distance`/`similarity` per §8.

`retrieve_relevant_chunks` orchestrates these in the order shown in §5 and returns the final list. It has no FastAPI, HTTP, or frontend dependency — it is importable and testable as plain Python/SQLAlchemy.

---

## 17. API Endpoint Design

A narrow endpoint for manual verification is appropriate and low-risk:

```
POST /documents/{document_id}/search
```

`[VERIFY]` whether the existing document routes file (in `backend/app/routes/`) is the right place to add this, or whether a new `retrieval.py`/`search.py` routes module better matches repo conventions (e.g., if routes are already split by concern rather than by resource).

**Request schema:**

```python
class SearchRequest(BaseModel):
    query: str
    top_k: Optional[int] = None
```

**Response schema:**

```python
class RetrievalResultSchema(BaseModel):
    chunk_id: Optional[int | UUID]
    document_id: int | UUID
    chunk_index: int
    page_number: int
    content: str
    distance: float
    similarity: float

class SearchResponse(BaseModel):
    document_id: int | UUID
    query: str
    top_k: int          # the resolved value actually used
    count: int
    results: list[RetrievalResultSchema]
```

**Route responsibilities only:**
1. Parse/validate the request body via the Pydantic schema (FastAPI does this automatically).
2. Call `retrieve_relevant_chunks(db, document_id, request.query, request.top_k)`.
3. Catch the service-level exceptions from §11–§13 and translate to HTTP errors with clear messages.
4. Serialize the returned `RetrievalResult` list into `SearchResponse`.

No business logic, no query construction, and no embedding calls in the route itself — all of that lives in `retriever.py`.

Performance (Phase 8 requirement, restated for the route): the route makes exactly one call into the retrieval service, which performs exactly one embedding call and one database query. Do not add caching at this layer.

---

## 18. Database / Schema / Index Changes

- No new tables.
- No new columns on `documents` or `document_chunks`.
- One new index: HNSW on `document_chunks.embedding` using `vector_cosine_ops`, as specified in §9.
- Added via the existing schema-initialization mechanism established in Phase 3, kept idempotent (`IF NOT EXISTS`).
- No Alembic, no new migration framework.

---

## 19. Configuration Changes

Add to the existing Pydantic Settings class in `backend/app/config.py` (`[VERIFY]` naming/casing convention already used, e.g. all-caps vs. lower_snake, and whether settings are grouped/prefixed):

- `RETRIEVAL_TOP_K_DEFAULT: int = 5`
- `RETRIEVAL_TOP_K_MAX: int = 20`
- `MAX_QUERY_LENGTH: int = 8000`

No other configuration changes. Do not add settings for LLM/Ollama in this phase — that belongs to Phase 9+.

---

## 20. Dependency Changes

**Expectation: no new dependencies.** Phase 8 should be implementable with what Phase 3/6/7 already require:

- FastAPI
- SQLAlchemy
- psycopg (or psycopg2/psycopg3, whichever Phase 3 used)
- `pgvector` (python package, SQLAlchemy `Vector` type + comparator support)
- `sentence-transformers`
- `numpy`
- PyMuPDF (unused directly in Phase 8, but already present)

`[VERIFY]` `requirements.txt` for:
1. The exact `pgvector` package version, to confirm `.cosine_distance()` SQLAlchemy comparator support (see §7's fallback note if it's missing).
2. That `numpy` is already present (needed to hold the query embedding array before passing it into the query parameter).

If either check fails, OpenCode should explicitly report the gap rather than silently adding a new dependency — the project owner should approve any actual new dependency, even a minor version bump.

---

## 21. File-by-File Implementation Plan

`[VERIFY]` every path below against the actual repo layout before creating/editing anything.

| File | Change |
|---|---|
| `backend/app/rag/retriever.py` | **New.** Core retrieval service (§16). |
| `backend/app/rag/exceptions.py` (or wherever Phase 7's document-status exceptions already live) | **New or extend.** Add `DocumentNotFoundError`, `DocumentNotReadyError`, `DocumentIngestionFailedError`, `DocumentEmptyError`, `InvalidQueryError`, `InvalidTopKError` — reuse existing exception base class/hierarchy if one exists. |
| `backend/app/schemas/` (or inline in routes, per existing convention) | **New or extend.** `SearchRequest`, `SearchResponse`, `RetrievalResultSchema` (§17). |
| `backend/app/routes/documents.py` (or new `retrieval.py`) | **Extend or new.** `POST /documents/{document_id}/search` route (§17). |
| `backend/app/config.py` | **Extend.** Add the three settings from §19. |
| Wherever Phase 3's schema-init/DDL lives (`backend/app/database.py` or `backend/scripts/`) | **Extend, or add** `backend/scripts/create_vector_index.py`. Add the idempotent HNSW index DDL (§9). |
| `ARCHITECTURE.md` | **Extend.** Add the retrieval stage to the pipeline diagram (§27). |
| `docs/phases/PHASE_8_SEMANTIC_RETRIEVAL.md` | **New.** This document. |
| `backend/tests/test_phase8_retrieval.py` (matching existing `test_phaseN_*` naming, `[VERIFY]`) | **New.** Test suite (§23). |

No changes to Phase 4/5/6/7 files unless a genuine bug is discovered while inspecting them — if so, report it separately rather than silently fixing it as part of Phase 8.

---

## 22. Function / Class Responsibilities Summary

- `retrieve_relevant_chunks(db, document_id, query, top_k)` — public orchestrator; the only function other phases should import.
- `_get_ready_document`, `_validate_query`, `_resolve_top_k`, `_embed_query`, `_run_similarity_query` — private helpers, each with one job, each independently unit-testable.
- `RetrievalResult` — plain data container, no behavior.
- `DocumentNotFoundError` / `DocumentNotReadyError` / `DocumentIngestionFailedError` / `DocumentEmptyError` / `InvalidQueryError` / `InvalidTopKError` — exceptions, no behavior beyond carrying context (e.g. current status, offending value).
- `SearchRequest` / `SearchResponse` / `RetrievalResultSchema` — Pydantic I/O contracts for the route only; never imported by the service layer (service layer must not depend on API schemas).
- Route handler — thin: parse → call service → catch → serialize.

---

## 23. Test Plan

All tests run against the actual Supabase Postgres + pgvector instance (`[VERIFY]` the existing test-database setup/fixtures from Phases 3–7) — **not SQLite** for anything touching vector columns.

**Fixture:** a small, deterministic set of chunks across two documents with clearly distinct topics, e.g.:

- Document A (`insurance`): chunks about *car insurance deductible*, *premium payment*, *claim filing*.
- Document B (`university`): chunks about *attendance policy*, *examination rules*.

1. A query about "deductible" against Document A returns the deductible chunk near the top.
2. Results are ordered by ascending distance / descending similarity, correctly.
3. Each result contains `document_id`, `chunk_index`, `page_number`, `content`, `distance`, `similarity` (and `chunk_id` if applicable).
4. Raw embedding vectors are never present in returned results.
5. **Isolation:** the same query executed with Document A's id never returns any Document B chunk, and vice versa.
6. A `READY` document can be searched successfully.
7. A `PENDING`/`PROCESSING` document raises `DocumentNotReadyError`.
8. A `FAILED` document raises `DocumentIngestionFailedError`.
9. An `EMPTY` document raises `DocumentEmptyError`.
10. `top_k=N` returns at most N results.
11. `top_k` greater than available chunks returns all available chunks, no error.
12. Invalid `top_k` (`0`, negative, `> MAX`) raises `InvalidTopKError`.
13. Empty query raises `InvalidQueryError`.
14. Whitespace-only query raises `InvalidQueryError`.
15. A scenario producing zero DB rows returns `results == []`, no exception.
16. The query embedding is produced via the Phase 6 embedding service (assert same model/singleton is used, e.g. via a spy/mock or by checking output matches a known Phase 6 embedding for the same string).
17. Retrieval performs no writes — assert `document_chunks`/`documents` row counts and contents are unchanged before/after a retrieval call.
18. The HNSW index exists on `document_chunks.embedding` (catalog inspection, see §24).
19–22. Existing Phase 4, 5, 6, and 7 test suites still pass unmodified (regression).

---

## 24. Manual Verification Procedure

1. Start the backend against the real Supabase Postgres instance.
2. Upload a real multi-page text-heavy PDF; confirm it reaches `READY` via the existing Phase 7 flow (`POST /documents/{id}/ingest` if not already automatic).
3. Call `POST /documents/{document_id}/search` with a query clearly related to a specific section of the document; confirm the top result's `content`/`page_number` plausibly matches.
4. Call the same endpoint with an unrelated `document_id` from a different, also-`READY` document; confirm results only ever come from the requested document.
5. Call with `top_k` omitted, `top_k=1`, `top_k` above `RETRIEVAL_TOP_K_MAX`, `top_k=0`, and a negative value; confirm behavior matches §12's table.
6. Call with an empty string and a whitespace-only string; confirm `InvalidQueryError` → appropriate HTTP error.
7. Call against a document still `PENDING`/`PROCESSING` and one `FAILED`; confirm the correct rejection in each case.
8. Inspect Postgres catalogs directly (exact query depends on Postgres version, conceptually):
   ```sql
   SELECT indexname, indexdef
   FROM pg_indexes
   WHERE tablename = 'document_chunks';
   ```
   Confirm an HNSW index using `vector_cosine_ops` on the `embedding` column is present.

---

## 25. Acceptance Criteria

- [ ] `retrieve_relevant_chunks` exists, is importable independent of FastAPI, and matches the responsibilities in §16.
- [ ] Query embedding reuses the Phase 6 singleton with no second embedding implementation.
- [ ] Similarity search is a single pgvector query per request, scoped by `document_id` in-database.
- [ ] `distance`/`similarity` semantics are defined exactly as in §8 and used consistently in code, schemas, and docs.
- [ ] HNSW index on `document_chunks.embedding` (`vector_cosine_ops`) exists, created idempotently via the existing schema mechanism.
- [ ] Document status gating matches §11 for every real status the repository defines.
- [ ] `top_k` and query validation match §12–§13 exactly, including edge cases.
- [ ] No-result cases never raise; they return `[]`.
- [ ] Raw embeddings are never exposed in results or API responses.
- [ ] `POST /documents/{document_id}/search` (or repo-appropriate path) exists, is thin, and matches §17.
- [ ] No new dependencies added without explicit report.
- [ ] Full test suite in §23 passes, including all Phase 4–7 regressions.
- [ ] No LLM, agent, chat, or answer-generation code exists anywhere in the diff.

---

## 26. Phase 9 Integration Boundary

Phase 9 (or whichever phase introduces the LLM/agent) will:
- Call `retrieve_relevant_chunks(db, document_id, query, top_k)` as-is.
- Consume the returned `RetrievalResult` list to construct grounded context/prompts.
- Own all prompt construction, LLM/Ollama calls, and answer synthesis.

Phase 8 must not anticipate this by adding prompt-shaped fields, answer placeholders, or LLM configuration. The contract Phase 9 depends on is exactly: **a function that returns a list of `RetrievalResult` objects, scoped to one document, ranked by relevance.**

---

## 27. Risks / Edge Cases

- **Planner may skip the HNSW index for small per-document chunk counts.** Filtering by `document_id` first can leave few enough candidate rows that Postgres chooses a sequential scan over the index. This is correct planner behavior at MVP scale, not a bug — it should not cause test failures if tests check index *existence*, not that it is used on every query.
- **Cold-start latency:** the first retrieval call after backend startup pays the Phase 6 model's lazy-load cost. This is inherited from Phase 6, not new to Phase 8, but worth noting in manual verification (first call may be visibly slower).
- **Normalization mismatch:** if Phase 6 normalizes embeddings before storage but query embeddings aren't normalized identically (or vice versa), cosine distance becomes inconsistent. Must reuse the exact same embedding function end-to-end.
- **ID type mismatch:** `document_id` type (`int` vs `UUID`) must match exactly what Phase 3/7 actually used; a mismatch causes either silent empty results or type errors depending on driver behavior.
- **Large `content` fields in API responses:** if chunks are large, returning full `content` for every result could bloat responses; acceptable for MVP top_k defaults (≤20), but worth a one-line note in the docs rather than solving now.
- **Race between status check and query:** a document could theoretically change status between the status check and the similarity query in high-concurrency scenarios. Acceptable to ignore at MVP scale; not worth transaction-level locking here.
- **`chunk_id` may not exist as a clean field** on the ORM model as currently defined (Phase 5's `ChunkRecord` description doesn't list one) — verify before assuming it's available; omit from the result model if it genuinely isn't there rather than inventing a surrogate.

---

## 28. Exact OpenCode Implementation Instructions

Follow this order exactly:

1. **Inspect first.** Read every file listed in §0. Produce, as your first action, a short internal note reconciling each `[VERIFY]` item in this document against what you actually find (status enum values, ID types, settings naming, existing exception patterns, embedding service's exact callable, routes file layout, schema-init mechanism, pgvector package version).
2. **Do not touch Phase 4–7 code** unless you find a genuine defect blocking Phase 8; if so, stop and report it instead of fixing it silently.
3. Add the three settings from §19 to the existing Settings class, using its established naming convention.
4. Add the exceptions from §11/§13/§12, reusing an existing exception hierarchy if the repo has one.
5. Implement `backend/app/rag/retriever.py` per §16, with the private helpers as separate, independently testable functions.
6. Add the HNSW index DDL to the existing schema-init mechanism (or the small idempotent script fallback), per §9.
7. Add `SearchRequest`/`SearchResponse`/`RetrievalResultSchema` and the `POST /documents/{document_id}/search` route per §17, keeping the route thin.
8. Write the test suite per §23 against the real Postgres+pgvector test setup, including the catalog-inspection index test.
9. Run the full existing test suite (Phases 4–7) to confirm zero regressions.
10. Update `ARCHITECTURE.md` to add the retrieval stage to the pipeline diagram, without describing Phase 9+ functionality as implemented.
11. Do not write, mention, or scaffold anything related to Ollama, LLM calls, agents, or chat — even as commented-out placeholders.
12. If any dependency is genuinely missing per §20, stop and report it explicitly rather than adding it unilaterally.
