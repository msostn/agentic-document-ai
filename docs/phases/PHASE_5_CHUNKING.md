# Phase 5 Implementation Specification — Document Text Chunking

Status: NEXT (to be implemented by a coding agent)
Parent spec: `ARCHITECTURE.md` (permanent source of truth — this phase must not contradict it)
Verified prior state: Phases 1–4 complete and committed. PDF upload, validation, PyMuPDF extraction, page-aware text extraction, and document CRUD (`ready`/`failed` lifecycle) all work — 21/21 Phase 4 tests passed. `document_chunks` table exists (Phase 3) with `embedding` fixed as `vector(384) NOT NULL`. No chunk has ever been written to that table. No embeddings, retrieval, or agent logic exists.

---

## Important note on scope resolution (read first)

The Phase 5 request asks this spec to "define exactly when `DocumentChunk` rows are created" and to "respect the existing foreign key and cascade-delete behavior." Read literally, that implies live persistence into `document_chunks` during this phase. That would conflict with `ARCHITECTURE.md` §9, which assigns *"full ingestion pipeline writing chunks + embeddings to pgvector"* to **Phase 7**, not Phase 5 — and it would collide with the schema itself, since `embedding` is `NOT NULL` and no embedding logic exists until Phase 6.

Per the standing instruction to follow `ARCHITECTURE.md` rather than redesign it, this spec resolves the tension as follows:

- **Phase 5 produces chunks as an in-memory data structure only.** It does not insert any row into `document_chunks`, and it is not wired into the upload flow.
- **"When are `DocumentChunk` rows created?"** → Not in Phase 5. They are created in Phase 7, once embeddings exist to satisfy the `NOT NULL` constraint. This spec instead guarantees *field-level compatibility* between the chunker's output and the `DocumentChunk` model, so Phase 7 can persist chunks with no rework.
- **"Respect FK/cascade behavior"** → Already verified at the schema level in Phase 3 (§11, TEST 4 of that spec used a placeholder embedding specifically to confirm FK + cascade delete work). Phase 5 does not need to re-run that live-database test; it only needs to confirm its output shape matches what that already-verified schema expects.
- **No schema changes are made in this phase** beyond what's already true — see §7.

This keeps Phase 5 fully independent and testable without a database, consistent with how Phase 4 deferred chunk persistence for the same underlying reason.

---

## 1. Phase Objective

Take the page-aware text that Phase 4's extraction already knows how to produce, and turn it into a sequence of well-sized, page-scoped, retrieval-friendly text chunks — as a pure, standalone module with no database or HTTP involvement. The output of this phase is the exact data shape Phase 7 will later attach embeddings to and persist.

---

## 2. Current Architecture Relevant to Phase 5

- `app/rag/parser.py` (Phase 4) exposes page-aware extraction: given PDF bytes, it returns an ordered sequence of `(page_number, text)` pairs, 1-indexed, in memory only. This is the exact input shape Phase 5 consumes.
- `app/models/document_chunk.py` (Phase 3) defines the eventual destination shape: `document_id`, `chunk_index`, `page_number`, `content`, `embedding (NOT NULL)`. Phase 5's output must map cleanly onto the first four of these fields.
- `app/config.py` already declares `chunk_size` (default 800) and `chunk_overlap` (default 150) from Phase 1 — these were reserved for this exact phase and are reused here, not redefined.
- No route, service, or script currently calls the (not-yet-existing) chunker. This phase does not change that — no wiring into `document_service.py` or any route happens here.

---

## 3. Chunking Design

**Unit of measurement:** characters, not tokens. A tokenizer belongs to the embedding model (Phase 6, `sentence-transformers`) and must not be introduced in this phase — that would pull in embedding-adjacent dependencies early, which is explicitly out of scope.

**Page-scoping rule (hard constraint):** a chunk never spans more than one page. This is forced by the existing schema — `document_chunks.page_number` is a single integer, not a range — so cross-page chunks have no valid way to be represented without a schema change, and a schema change is not "genuinely necessary" here. Each page's text is chunked independently.

**Per-page algorithm:**
1. **Normalize whitespace** for the page's text: collapse runs of whitespace/newlines/blank lines into single spaces, strip leading/trailing whitespace.
2. **Skip near-empty pages:** if the normalized text's length is below `MIN_CHUNK_SIZE` (new config value, §7), the page produces zero chunks. This is not an error — it's expected for cover pages, section dividers, or pages with only a header.
3. **Split into words** on whitespace (never split inside a word — chunk boundaries always fall between words).
4. **Accumulate words into a chunk** until adding the next word would exceed `CHUNK_SIZE` characters, then close that chunk.
5. **Overlap:** the next chunk begins by re-including the trailing words of the previous chunk, up to roughly `CHUNK_OVERLAP` characters worth, before continuing with new words. Overlap is word-based (never mid-word), so the realized overlap length will vary slightly around the configured value — this is expected and acceptable.
6. **Trailing-chunk merge rule:** if the last chunk produced from a page would be smaller than `MIN_CHUNK_SIZE`, merge it into the previous chunk instead of emitting a tiny final fragment (avoids "unnecessarily tiny chunks"). If the page has only one chunk total, this rule doesn't apply (there's nothing to merge into).
7. **No overlap carries across a page boundary.** The first chunk of each page starts fresh; this is a deliberate, documented trade-off in exchange for guaranteed-accurate per-page citations (see the scope-resolution note above).

**Chunk ordering:** `chunk_index` is a single, globally increasing counter across the *entire document*, not reset per page. Skipped pages simply contribute no entries — there are no gaps to reason about, since the index only increments when a chunk is actually produced.

---

## 4. Chunk Metadata Design

Each produced chunk is an in-memory record (no ORM class needed yet — this is what Phase 7 will map onto `DocumentChunk` when it inserts rows) with exactly these fields:

| Field | Type | Description |
|---|---|---|
| `document_id` | UUID | The parent document, passed in by the caller — never invented by the chunker itself |
| `chunk_index` | integer | Global, 0-based, sequential across the whole document |
| `page_number` | integer | The single source page this chunk's text came from (1-indexed, matching `parser.py`'s page numbering) |
| `content` | string | The normalized chunk text, trimmed, no leading/trailing whitespace |

This is a direct, unmodified match to `DocumentChunk`'s non-embedding columns — deliberately, so Phase 7 requires no field renaming or transformation.

---

## 5. Processing Flow

```
Document (status = "ready", from Phase 4)
   │
   ▼
Extracted pages: [(page_number, text), ...]     ← already exists from Phase 4's parser
   │
   ▼
For each page:
   normalize whitespace
   → if below MIN_CHUNK_SIZE: skip page, produce 0 chunks
   → else: split into words, accumulate into chunks respecting CHUNK_SIZE/CHUNK_OVERLAP
   → merge undersized trailing chunk into previous, if applicable
   │
   ▼
Flat, ordered list of chunk records (document_id, chunk_index, page_number, content)
   │
   ▼
[END OF PHASE 5 — nothing is persisted; this list is the phase's deliverable]
```

Phase 7 will later extend this flow by feeding each chunk's `content` into the embedding service (Phase 6) and inserting the resulting rows into `document_chunks`.

---

## 6. Files to Create / Modify

**Created:**
- `backend/app/rag/chunker.py` — the chunking algorithm. Pure functions only: given a document id and a list of `(page_number, text)` pairs, return a list of chunk records as defined in §4. No imports of `SQLAlchemy`, `fastapi`, or any HTTP/DB module.
- `backend/scripts/test_chunker.py` — a standalone, deterministic verification script (see §10) that exercises the chunker against synthetic, hardcoded page text (not real PDFs, to keep results deterministic) and asserts the behaviors described in this spec, printing a clear pass/fail summary per case.

**Modified:**
- `backend/app/config.py` — add the new `min_chunk_size` setting (§7).
- `backend/.env` and `backend/.env.example` — add `MIN_CHUNK_SIZE`.

**Not modified:**
- `backend/app/models/document_chunk.py` — no column or type changes.
- `backend/app/services/document_service.py`, `backend/app/routes/documents.py` — chunking is not called from here yet; that wiring is Phase 7's job.
- `backend/app/main.py` — no new routes.
- `backend/requirements.txt` — no new dependencies required (plain Python string/list operations only).

---

## 7. Database Changes

**None.** The schema from Phase 3 is left exactly as-is:
- `document_chunks.embedding` remains `vector(384) NOT NULL`.
- No new tables, columns, or indexes.
- No vector index work (explicitly out of scope per the Phase 5 request).

This satisfies the instruction not to introduce schema changes for the sake of a future phase: nothing about the chunker's design requires the database to change, so nothing changes.

---

## 8. Configuration Changes

Reused from existing config (no changes):
- `CHUNK_SIZE` — default `800` (target maximum characters per chunk).
- `CHUNK_OVERLAP` — default `150` (approximate character overlap between consecutive chunks on the same page).

New:
- `MIN_CHUNK_SIZE` — default `100` (characters). Two roles: (1) pages with less normalized text than this are skipped entirely; (2) a trailing chunk smaller than this is merged into the previous chunk rather than kept standalone.

**Validation rule:** at startup/config-load time, if `CHUNK_OVERLAP >= CHUNK_SIZE`, this is a configuration error (overlap must be strictly smaller than chunk size, or the algorithm never makes forward progress). Similarly, `MIN_CHUNK_SIZE` must be smaller than `CHUNK_SIZE`. Fail fast with a clear error message rather than allowing the chunker to loop or misbehave silently.

---

## 9. Error Handling

Since this phase has no database or HTTP surface, "error handling" means: what the chunker function itself returns or raises for each edge case, and what contract Phase 7 will later need to honor.

| Scenario | Chunker behavior |
|---|---|
| Empty or all-whitespace page text | Page contributes zero chunks; not an error |
| Page below `MIN_CHUNK_SIZE` | Page contributes zero chunks; not an error |
| Very short overall document (single short page just above threshold) | Produces exactly one chunk; not an error |
| Unusual whitespace (tabs, repeated blank lines, mixed line endings) | Normalized before chunking; does not affect chunk count or cause errors |
| Every page in the document is skipped (zero chunks produced overall) | The function returns an empty list. This is a distinguishable, valid return value — **not** an exception — but it is a meaningful signal: **Phase 7, when it eventually calls this chunker as part of the live ingestion pipeline, must treat an empty result as a reason to mark the document `status = "failed"`** rather than leaving it `ready` with nothing retrievable. Phase 5 defines this signal; Phase 7 is responsible for acting on it against a real `Document` row. |
| Malformed input (e.g., a page tuple with a negative or missing page number) | Function raises a `ValueError` with a clear message — this indicates a bug in the caller (Phase 4's parser should never produce this), not a document-quality issue |

No document rows exist in Phase 5's own code paths, so "avoid leaving documents stuck in processing" is honored by contract (documented above) rather than by any runtime state transition performed in this phase.

---

## 10. Testing Plan

All tests use synthetic, hardcoded input page lists — not real PDFs — so results are fully deterministic and don't depend on PyMuPDF's extraction behavior. Implement these as plain Python functions with `assert` statements inside `backend/scripts/test_chunker.py`, run via `python scripts/test_chunker.py`; introducing `pytest` is not required for this phase (existing verification scripts, e.g. Phase 3's `init_db.py`, follow this same standalone-script pattern).

**TEST 1 — Normal multi-page document**
Input: 3 synthetic pages, each ~1,500 characters of realistic prose.
Expected: multiple chunks per page; `chunk_index` strictly increasing across the whole result; every chunk's `page_number` matches its source page.

**TEST 2 — Short document**
Input: 1 page, ~300 characters (above `MIN_CHUNK_SIZE`, below `CHUNK_SIZE`).
Expected: exactly one chunk, content equal to the full normalized page text.

**TEST 3 — Long single page**
Input: 1 page, ~5,000 characters.
Expected: multiple chunks; each chunk's length is at or below `CHUNK_SIZE`, no chunk falls below `MIN_CHUNK_SIZE` except possibly none (trailing-merge rule applied).

**TEST 4 — Page boundaries**
Input: 2 pages with distinctly different, easily-identifiable text (e.g. page 1 all lowercase, page 2 all uppercase).
Expected: no chunk contains a mix of the two pages' text; every chunk's `page_number` correctly identifies its single source page.

**TEST 5 — Overlap behavior**
Input: 1 page long enough to produce at least two chunks.
Expected: the trailing words of chunk N reappear at the start of chunk N+1; the overlapping word span's character length is reasonably close to `CHUNK_OVERLAP` (exact match not required, since overlap is word-bounded).

**TEST 6 — Whitespace normalization**
Input: a page with tabs, multiple consecutive blank lines, and irregular spacing.
Expected: resulting chunk content contains single-spaced text with no blank lines or tab characters.

**TEST 7 — Tiny/empty pages**
Input: one page that is empty/whitespace-only, one page with a few words below `MIN_CHUNK_SIZE`.
Expected: both pages produce zero chunks; no exceptions raised.

**TEST 8 — Zero-chunk document**
Input: a document where every page is below `MIN_CHUNK_SIZE`.
Expected: the function returns an empty list; the test asserts this explicitly (distinguishing "valid empty result" from a crash).

**TEST 9 — Field compatibility with `DocumentChunk`**
Check (static/manual, not a live DB write): confirm the chunk record's field names and types (`document_id`, `chunk_index`, `page_number`, `content`) match `DocumentChunk`'s corresponding columns exactly, so Phase 7 can construct ORM instances directly from chunker output with no field mapping/renaming step.

**TEST 10 — Cascade/FK compatibility (reference only, no re-test)**
Note in the script's output that FK and cascade-delete behavior for `document_chunks` was already verified in Phase 3 (§11, TEST 4) using a placeholder embedding, and does not need to be re-verified here since Phase 5 doesn't touch the database. If extra confidence is desired, an optional one-off insert using a zero-vector placeholder embedding (same pattern as Phase 3) may be run manually — but this is not part of the required Phase 5 test suite.

---

## 11. Acceptance Criteria

- [ ] `app/rag/chunker.py` exists, contains no database or HTTP imports, and exposes a function that takes a `document_id` and an ordered list of `(page_number, text)` pairs and returns a list of chunk records per §4.
- [ ] Chunks never span more than one page.
- [ ] `chunk_index` is globally sequential across the whole document, not reset per page.
- [ ] Whitespace (tabs, repeated blank lines, irregular spacing) is normalized before chunking.
- [ ] Pages below `MIN_CHUNK_SIZE` produce zero chunks without raising an error.
- [ ] Trailing chunks smaller than `MIN_CHUNK_SIZE` are merged into the preceding chunk rather than kept standalone.
- [ ] Consecutive chunks on the same page exhibit overlap approximately matching `CHUNK_OVERLAP`.
- [ ] A document where every page is skipped returns an empty list, not an exception.
- [ ] `CHUNK_SIZE`, `CHUNK_OVERLAP`, and the new `MIN_CHUNK_SIZE` are all read from configuration, not hard-coded in the chunker.
- [ ] Config validation rejects `CHUNK_OVERLAP >= CHUNK_SIZE` and `MIN_CHUNK_SIZE >= CHUNK_SIZE` at load time with a clear error.
- [ ] No changes were made to `document_chunks`, `documents`, or any other table.
- [ ] No code path in this phase creates a `DocumentChunk` row or calls the database.
- [ ] No embedding, vector, LLM, Ollama, agent, chat, or frontend code was introduced.
- [ ] `backend/scripts/test_chunker.py` runs standalone (`python scripts/test_chunker.py`) and all 10 tests in §10 pass deterministically, with no dependency on a live database or a real PDF file.

---

## 12. Explicit Out-of-Scope List

- Embeddings, `sentence-transformers`, or any vector generation.
- Vector indexes, vector similarity search, or any `pgvector`-specific query.
- Writing, updating, or reading any row in `document_chunks`.
- Any change to `document_chunks.embedding` or its `NOT NULL` constraint.
- Wiring the chunker into `document_service.py`, any route, or the upload flow (`POST /documents/upload`).
- Any new API endpoint.
- RAG retrieval, LLM calls, Ollama, agent/tool-calling logic, or chat functionality.
- Frontend changes of any kind.
- Authentication.
- Introducing `pytest`, `LangChain`, `LangGraph`, or any new third-party dependency.
- Cross-page chunk merging or any schema change to support multi-page chunk ranges.
