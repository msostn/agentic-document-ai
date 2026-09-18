# PHASE 9 — RAG CONTEXT ORCHESTRATION

> Status: SPECIFICATION ONLY — no code has been implemented. This document is
> written to be handed to an implementation agent (OpenCode) working directly
> **Revision note:** This revision preserves the original Phase 9 design while
> correcting two edge cases: the context budget now counts the exact rendered
> `context_text` (including metadata/separators), and `NO_CHUNK_FITS_BUDGET`
> explicitly represents relevant context that cannot fit without truncation.

> in the repository. Wherever this spec references a file, function, or
> convention it does not have direct sight of, it says so explicitly and
> instructs the implementer to confirm against the real repository before
> writing code, per the Repository Rule (Section 27).

---

## 1. Phase Objective

Phase 9 introduces a thin orchestration layer, **the RAG context layer**,
that sits between Phase 8 (semantic vector retrieval) and the future Phase 10
(LLM answer generation).

Given a `document_id` and a user `query`, Phase 9 must produce a **structured,
bounded, source-attributed context object** that Phase 10 can consume without
knowing anything about embeddings, pgvector, or chunk storage.

Phase 9 does **not** answer the question. It decides *what evidence, if any,
is worth handing to an LLM*, and represents that decision in a structured,
inspectable form.

---

## 2. Current Architecture Context

The pipeline as of Phase 8:

```
PDF → parse → chunk → embed → persist → semantic retrieval → ranked chunks
```

Phase 8 exposes:

- An internal retrieval function/service (exact name/location TBD — see
  Section 27) that performs query embedding, pgvector cosine search scoped to
  a single `document_id`, and returns ranked chunks with:
  - `chunk_id`
  - `document_id`
  - `chunk_index`
  - `page_number`
  - `content`
  - cosine distance
  - similarity
- A route: `POST /documents/{document_id}/search`, which is a thin HTTP
  wrapper around that internal function.

Phase 8 is read-only, document-isolated, and already tested (32/32 Phase 8
checks, plus full regression on Phases 4–7).

Phase 9 must **reuse** the internal Phase 8 function directly — never the
HTTP route, and never a re-implementation of the pgvector query.

---

## 3. Phase 9 Scope

Phase 9 is responsible for, and only for:

1. Validating the inputs to a context request (non-empty query, resolvable
   document).
2. Calling the existing Phase 8 retrieval function with the requested
   `document_id` and `query`.
3. Optionally filtering low-relevance results using a configurable
   similarity threshold.
4. Selecting which of the (already-ranked) chunks fit inside a configurable
   character budget, preserving complete chunks.
5. Assembling a structured result object that preserves all source metadata
   (document, page, chunk index, rank, distance/similarity) for every chunk
   it considered.
6. Optionally producing a deterministic plain-text rendering of the included
   chunks, for convenience in Phase 10.
7. Representing, in that same structured object, the difference between
   "usable context found" and every category of "no usable context" the
   project cares about (Section 12).

Phase 9 is a **pure orchestration/read layer**. It performs no database
writes, no re-embedding, and no new persistence.

---

## 4. Explicit Out-of-Scope Items

Phase 9 explicitly does **not** include:

- Reranking models
- BM25 / hybrid (lexical + vector) retrieval
- Query rewriting or expansion
- Multi-query retrieval
- Parent-document or recursive retrieval
- Contextual compression
- Semantic filtering models (e.g. an LLM-based relevance judge)
- Any external API calls
- Tokenization or token counting
- Any assumption about which Ollama model will be used, or its context
  window size
- Construction of the final LLM system prompt
- Answer generation of any kind
- Deduplication logic beyond what Section 13 defines (i.e., none, for now)
- New HTTP endpoints unless justified (Section 16)
- New heavy dependencies (Section 18)

If an implementer finds themselves reaching for any of the above while
building Phase 9, that is a signal they have drifted out of scope.

---

## 5. Why Phase 9 Exists Separately From Phase 8

It would be possible to let Phase 10 call the Phase 8 retrieval function
directly. Phase 9 exists anyway, for three concrete reasons:

1. **Different questions, different concerns.** Phase 8 answers "which
   chunks are most similar to this query?" — a pgvector/embedding concern.
   Phase 9 answers "is there enough *usable* evidence to let an LLM answer,
   and if so, in what bounded form?" — an LLM-context-assembly concern. These
   are separate responsibilities with separate reasons to change: Phase 8
   changes if the embedding model or index changes; Phase 9 changes if the
   context budget, threshold, or formatting rules change.

2. **Phase 8 must stay reusable and LLM-agnostic.** If budget/threshold/
   formatting logic were pushed into Phase 8, every future consumer of raw
   retrieval (e.g. a "preview matching passages" UI feature, or a different
   future consumption path) would inherit LLM-context assumptions it doesn't
   need. Keeping Phase 8 a plain retrieval primitive keeps it reusable.

3. **Phase 10 needs one deterministic decision point.** Without Phase 9,
   every future caller of retrieval would need to re-implement "what counts
   as no relevant context" and "how much do I include" independently. Phase
   9 makes that decision exactly once, in one place, and returns an
   unambiguous structured answer.

Phase 9 is intentionally **thin**: it does not duplicate anything Phase 8
already computes (distance, similarity, ranking order all pass through
unchanged), and it does not introduce a new database round-trip.

---

## 6. End-to-End Data Flow

```
user query + document_id
        ↓
[0] validate query is non-empty / non-whitespace
        ↓  (invalid → INVALID_QUERY, stop — no Phase 8 call)
[1] call Phase 8 internal retrieval function
    (document_id, query, top_k = Phase 8's existing default unless overridden)
        ↓  (document not ready/found → DOCUMENT_NOT_READY, propagate Phase 8's
        ↓   existing error semantics — see Section 21)
[2] ranked chunks (chunk_id, document_id, chunk_index, page_number,
    content, distance, similarity), in Phase 8's existing rank order
        ↓  (zero chunks returned → NO_CHUNKS_RETRIEVED)
[3] apply similarity threshold (RAG_MIN_SIMILARITY)
    → drop chunks below threshold, keep the rest, preserve rank order
        ↓  (all chunks dropped → BELOW_SIMILARITY_THRESHOLD)
[4] apply character budget (RAG_CONTEXT_MAX_CHARS)
    → walk chunks in rank order, include whole chunks until the next
      whole chunk would exceed the remaining budget, then stop
        ↓
[5] assemble RAGContextResult:
    - full per-chunk metadata for every chunk considered in step 3
    - `included` flag per chunk (true for chunks kept in step 4)
    - optional deterministic context_text built from included chunks only
    - status = OK
        ↓
Phase 10 (not part of this phase)
```

Every one of the "stop early" branches above still returns a fully-formed
`RAGContextResult` — Phase 9 never raises a domain-level exception for a
"no relevant content" case. It only propagates genuine infrastructure errors
(DB connectivity, unexpected exceptions), and it propagates whatever
not-ready/not-found signal Phase 8 already uses (see Section 21).

---

## 7. Retrieval Integration

Phase 9 must call the **internal Python function** that Phase 8's route
`POST /documents/{document_id}/search` already calls — not the HTTP endpoint,
and not a new pgvector query.

Before writing any code, the implementer must locate this function inside
`backend/app/rag/` and/or `backend/app/services/` (exact file/name unknown
to this spec — inspect the repository) and confirm:

- Its exact signature (parameter names/types for `db`, `document_id`,
  `query`, `top_k`, and any others).
- Its exact return type (a list of Pydantic models? ORM rows? dicts?) and
  the exact field names it uses for distance/similarity (this spec assumes
  `distance` and `similarity` but the real names may differ — match the
  repository, don't rename it silently).
- What it does when `document_id` does not exist or the document is not yet
  ingested (exception type, or a sentinel return value).
- Its default `top_k`.

Phase 9 must **reuse this function as-is**. It must not duplicate the
pgvector query, must not re-embed the query itself (Phase 8 already does
this via the Phase 6 embedding service), and must not add a second
database session pattern — it should accept the same `db` session dependency
style already used elsewhere in the codebase.

---

## 8. Context / Result Data Model

Two new schema objects, placed wherever the repository already keeps
Pydantic schemas (see Section 15) — this spec assumes `backend/app/schemas/`
exists per the Repository Rule file list.

### 8.1 `RAGContextStatus` (enum)

| Value | Meaning |
|---|---|
| `OK` | One or more chunks were retrieved, passed the similarity threshold, and at least one fit inside the character budget. `chunks` and `context_text` are usable. |
| `NO_CHUNKS_RETRIEVED` | Phase 8 retrieval returned zero chunks for this document/query (e.g. document has no chunks at all — an edge case, not the normal "irrelevant query" case). |
| `BELOW_SIMILARITY_THRESHOLD` | Phase 8 returned chunks, but none met `RAG_MIN_SIMILARITY`. This is the normal "query is unrelated to this document" case. |
| `DOCUMENT_NOT_READY` | The document exists but is not in a searchable/ingested state, mirroring whatever state Phase 7/8 already use to represent this. |
| `INVALID_QUERY` | The query was empty, whitespace-only, or otherwise fails basic input validation. No retrieval call was made. |
| `NO_CHUNK_FITS_BUDGET` | One or more chunks passed the similarity threshold, but none could be included without exceeding the configured context budget. |

Only `OK` carries usable context. Every other status is a legitimate,
expected outcome, not an error — Phase 10 is expected to branch on this enum
directly rather than on exceptions.

### 8.2 `RAGContextChunk`

Field-for-field, this preserves everything Phase 8 already returns, plus
Phase 9's own bookkeeping:

| Field | Type | Source |
|---|---|---|
| `chunk_id` | existing chunk ID type | passthrough from Phase 8 |
| `document_id` | existing document ID type | passthrough from Phase 8 (also re-validated, Section 14) |
| `chunk_index` | int | passthrough from Phase 8 |
| `page_number` | int | passthrough from Phase 8 |
| `content` | str | passthrough from Phase 8, unmodified/untruncated |
| `rank` | int | position in Phase 8's ranked result, 1-based, unmodified |
| `distance` | float | passthrough from Phase 8 |
| `similarity` | float | passthrough from Phase 8 |
| `included` | bool | Phase 9: true if this chunk survived both the threshold filter and the budget selection and is part of `context_text` |

`RAGContextChunk` is returned for **every** chunk Phase 8 retrieved that
passed the similarity threshold (Section 12 explains why chunks that fail the
budget are still listed, with `included=False`, rather than silently
dropped) — this keeps the full picture inspectable for debugging and for
Phase 10 to make its own downstream decisions if it ever needs to (e.g.
logging "3 more relevant chunks existed but didn't fit").

### 8.3 `RAGContextResult`

| Field | Type | Notes |
|---|---|---|
| `document_id` | existing document ID type | echoed input |
| `query` | str | echoed input, unmodified |
| `status` | `RAGContextStatus` | Section 8.1 |
| `message` | str | short, human-readable explanation of the status, for logs/debugging — not intended as end-user copy |
| `chunks` | `list[RAGContextChunk]` | rank-ordered; empty for `NO_CHUNKS_RETRIEVED`, `BELOW_SIMILARITY_THRESHOLD`, `DOCUMENT_NOT_READY`, `INVALID_QUERY`; threshold-passed chunks are retained for `NO_CHUNK_FITS_BUDGET` with `included=False` |
| `context_text` | `str \| None` | deterministic formatted string (Section 9.3), built only from `included=True` chunks; `None` unless `status == OK` |
| `total_retrieved` | int | count of chunks returned by Phase 8 before any filtering |
| `total_included` | int | count of chunks with `included=True` |
| `total_characters` | int | exact `len(context_text)` for the included rendered context; `0` when no context is included |
| `similarity_threshold_used` | float | the effective `RAG_MIN_SIMILARITY` value used for this call |
| `max_chars_used` | int | the effective `RAG_CONTEXT_MAX_CHARS` value used for this call |
| `top_similarity` | `float \| None` | best similarity score seen among retrieved chunks, even if it was below threshold — useful diagnostic for `BELOW_SIMILARITY_THRESHOLD`; `None` only for `NO_CHUNKS_RETRIEVED`/`DOCUMENT_NOT_READY`/`INVALID_QUERY` |

This is the single object Phase 9 returns. Everything Phase 10 needs —
including citation metadata (`page_number`, `chunk_index`, `document_id` per
chunk) — is present without Phase 10 needing to go back to Phase 8 or the
database.

---

## 9. Context Selection Strategy

### 9.1 Ordering

Chunks are processed strictly in the rank order Phase 8 already produced
(most similar first). Phase 9 never re-sorts, re-scores, or re-ranks.

### 9.2 Selection Algorithm (character budget)

The character budget applies to the **actual rendered `context_text`**, including
its page/chunk metadata and separators. It is not merely a budget for raw
`content` characters.

1. Start with an empty list of included chunks and `current_length = 0`.
2. Walk the threshold-filtered chunks strictly in Phase 8 rank order.
3. For each chunk, construct its exact rendered block:

```text
[Page {page_number} | Chunk {chunk_index}]
{content}
```

4. If this is not the first included chunk, account for the fixed separator
   (`\n\n`) that will appear before the block.
5. Include the whole chunk only if adding its complete rendered block and
   required separator keeps the final rendered `context_text` length
   `<= RAG_CONTEXT_MAX_CHARS`.
6. If the next complete block does not fit, stop inclusion. Mark that chunk and
   all subsequent lower-ranked threshold-passed chunks as `included=False`.
7. Never truncate a chunk.
8. Never skip an oversized higher-ranked chunk to include a later lower-ranked
   chunk.
9. After selection, build `context_text` from the included chunks and assert
   the invariant:

```text
len(context_text) <= max_chars_used
```

If at least one threshold-passed chunk exists but the first one cannot fit,
return `NO_CHUNK_FITS_BUDGET`. More generally, this status means zero
threshold-passed chunks were included because of the budget.

This remains a deterministic relevance-ordered prefix selection strategy.

### 9.3 Deterministic Text Formatting

When `status == OK`, Phase 9 also builds `context_text` by joining the
`included=True` chunks, in rank order, using a fixed, whitespace-normalized
template:

```
[Page {page_number} | Chunk {chunk_index}]
{content}
```

with a single blank line between consecutive chunk blocks, and no leading or
trailing prompt language of any kind (no "You are an AI...", no
instructions to the model — that belongs entirely to Phase 10). Chunk
`content` is inserted exactly as stored; Phase 9 does not strip, normalize,
or otherwise transform it.

The structured `chunks` list remains the source of truth. `context_text` is
a convenience derived from it and must always be reproducible from `chunks`
alone (this is directly testable — see Section 22, test #13).

---

## 10. Context Budget Strategy

### 10.1 Why character-based, not token-based

The project's own instructions rule out tokenization: Phase 9 must not
assume a specific Ollama model, tokenizer, or context window size, and must
not add a tokenization dependency. A character count is:

- Deterministic and dependency-free (no new library).
- A reasonable, conservative proxy for token count across the kinds of
  documents this project targets (English/Latin-script prose in insurance
  policies, legal text, manuals) — roughly 4 characters per token is a
  standard rough heuristic, but Phase 9 does not encode that ratio anywhere;
  it only uses raw character counts.
- Trivial to unit test without mocking a tokenizer.

### 10.2 Setting

`RAG_CONTEXT_MAX_CHARS` (int, default **8000**).

Rationale for the default: 8000 characters is roughly 2,000 tokens under the
common ~4-chars-per-token heuristic, which comfortably fits inside the
context window of small local Ollama models (e.g. 4K–8K token models) while
leaving headroom in Phase 10 for the query, any system instructions, and the
generated answer. This is a starting default, not a hardcoded assumption
about any specific model — it is fully configurable via Pydantic Settings,
and Phase 10 (or an operator) can override it once a specific Ollama model is
chosen.

### 10.3 Chunk count

Phase 9 does **not** introduce a new "max chunk count" setting
(e.g. no `RAG_MAX_CONTEXT_CHUNKS`). It reuses whichever `top_k` Phase 8's
retrieval function already defaults to (confirm the actual value during
implementation — Section 27), optionally overridable via the same parameter
name Phase 8 already exposes. Introducing a second, independent chunk-count
cap alongside the character budget would be redundant configuration for no
behavioral benefit — the character budget already bounds total content size,
which is the actual constraint that matters for Phase 10.

### 10.4 Truncation

Phase 9 never truncates a chunk's `content` to make it fit. A chunk either
fits whole or is excluded whole (Section 9.2). This avoids ever handing
Phase 10 a sentence fragment that could be mis-cited or misread, and avoids
Phase 9 needing any text-splitting logic.

---

## 11. Similarity Threshold Decision

**Decision: introduce a configurable similarity threshold.**

Justification: pgvector top-k search always returns `k` results regardless
of whether the query has anything to do with the document. Without a
threshold, Phase 9 (and therefore Phase 10) has no principled way to
distinguish "the document genuinely answers this" from "these are simply the
least-dissimilar chunks in an otherwise unrelated document." The project's
own no-answer requirements (Section 12) explicitly require this distinction
to exist, so deferring it to Phase 10 would just mean re-deriving the same
retrieved-chunk metadata a second time outside this layer — the opposite of
"thin orchestration."

### 11.1 Metric

**Similarity** (not raw distance), because it is the more intuitive,
consistently-oriented "higher is better" scale, and Phase 8 already computes
and returns it. Phase 9 does not recompute similarity from distance; it uses
whatever value Phase 8 already labels as similarity, whatever the exact
formula (confirm during implementation whether it is `1 - cosine_distance`
or something else — Phase 9 must not assume, only consume the field
Phase 8 provides).

### 11.2 Setting

`RAG_MIN_SIMILARITY` (float, default **0.3**).

This default is an explicit, honestly-labeled heuristic starting point, not
a derived or empirically-validated constant — cosine similarity thresholds
for sentence-transformer embeddings vary by domain and query phrasing. 0.3
is chosen as a conservative floor: low enough that a legitimately relevant
but loosely-worded query is unlikely to be wrongly rejected, high enough to
reject clearly unrelated queries (e.g. a question about an unrelated topic
against a policy document). This value should be revisited empirically once
real usage data exists; the spec's job is to make it configurable and
testable, not to get the number perfect on day one.

### 11.3 Configuration & Validation

- Exposed via the same Pydantic Settings mechanism used for existing config
  (confirm exact pattern in `backend/app/config.py`).
- Validated to fall within `[-1.0, 1.0]` (the mathematical range of cosine
  similarity), even though in practice values are expected to be within
  `[0.0, 1.0]` for this embedding model.
- Callable-overridable per request (an optional parameter on
  `build_rag_context`, defaulting to the configured value) so tests can
  exercise different thresholds without changing global config.
- Setting it to `-1.0` effectively disables filtering (everything passes),
  which is useful for tests that want pure pass-through behavior — this is
  not a "special disable flag," just the natural effect of the bound.

### 11.4 Behavior when all chunks fall below threshold

Status is `BELOW_SIMILARITY_THRESHOLD`. `chunks` is an empty list,
`context_text` is `None`, `total_included` is `0`, but `top_similarity` is
still populated with the best score seen, so Phase 10/logging can see *how
close* the nearest chunk was, without treating any of it as usable context.

---

## 12. No-Context / Insufficient-Context Behavior

All six cases the project requires are represented purely through
`RAGContextResult.status` (Section 8.1) — never through Phase 9 generating
any natural-language text about *why* there's no answer. That decision is
Phase 10's, once it sees the status.

| # | Case | Status | Phase 8 called? |
|---|---|---|---|
| 1 | Relevant context found | `OK` | yes |
| 2 | No chunks retrieved at all | `NO_CHUNKS_RETRIEVED` | yes |
| 3 | Chunks retrieved, none relevant enough | `BELOW_SIMILARITY_THRESHOLD` | yes |
| 4 | Relevant chunks exist, but none fit the context budget | `NO_CHUNK_FITS_BUDGET` | yes |
| 5 | Document not searchable/ingested | `DOCUMENT_NOT_READY` | attempted; Phase 8's existing not-ready signal is what produces this |
| 6 | Invalid query | `INVALID_QUERY` | no — short-circuited before any retrieval call |

This table is the contract Phase 10 is expected to branch on.

---

## 13. Duplicate / Overlap Handling

**Decision: do nothing special. Preserve Phase 8's retrieval results
unchanged; no deduplication logic in Phase 9.**

Reasoning:

- The Phase 5 chunker's overlap is intentional — it exists to avoid losing
  context at chunk boundaries. Two overlapping chunks both scoring highly is
  a sign the answer likely sits near that boundary, which is useful signal,
  not noise.
- Any deduplication (e.g. "drop a chunk if it shares N% of content with a
  higher-ranked chunk already included") would require a similarity-of-text
  computation, more configuration (a dedup threshold), and more test surface
  — for a problem that has not been observed to actually cause harm yet.
- The simplest approach that satisfies every stated requirement is to defer
  this. If, after real usage, overlapping/duplicate context is shown to
  waste budget or confuse Phase 10, a later phase can add a narrowly-scoped
  dedup step without touching anything else in this design (the budget
  selection step in Section 9.2 is the single place such logic would slot
  in later).

This is a conscious, documented deferral, not an oversight.

---

## 14. Document Isolation

Phase 9 must preserve Phase 8's document isolation guarantee end-to-end:

- Phase 9 calls Phase 8's retrieval function with exactly the requested
  `document_id` — never a list, never "all documents."
- After receiving Phase 8's results, Phase 9 asserts that **every** returned
  chunk's `document_id` equals the requested `document_id`. If this
  assertion ever fails, that indicates a bug in Phase 8 or the underlying
  query, and Phase 9 must treat it as an internal error (log loudly, raise —
  do not silently filter or silently continue), never as a normal "no
  context" status. This is a defensive check, not expected to ever trigger
  given Phase 8's existing guarantees, but it makes the isolation contract
  explicit and testable at this layer too (see Section 22, test #6).
- Phase 9 never accepts a list of `document_id`s and never merges context
  across documents, even internally.

---

## 15. Service / Module Design

**Decision: `backend/app/rag/context.py`**, alongside wherever Phase 8's
retrieval function already lives (this spec assumes it is also under
`backend/app/rag/`, per the Repository Rule's file list — confirm during
implementation; if Phase 8's retrieval actually lives under
`backend/app/services/` instead, mirror that and use
`backend/app/services/rag_context_service.py` instead, so Phase 9 sits next
to Phase 8 rather than introducing a second architectural pattern).

Schemas (`RAGContextStatus`, `RAGContextChunk`, `RAGContextResult`) go
wherever the repository already keeps Pydantic schemas — this spec assumes
`backend/app/schemas/`, e.g. a new `backend/app/schemas/rag.py` — confirm and
follow the existing per-phase schema file naming convention.

### Primary entry point (conceptual signature — not implementation)

```
def build_rag_context(
    db: Session,
    *,
    document_id: <existing document ID type>,
    query: str,
    top_k: int | None = None,          # falls back to Phase 8's existing default
    min_similarity: float | None = None,  # falls back to RAG_MIN_SIMILARITY
    max_context_chars: int | None = None, # falls back to RAG_CONTEXT_MAX_CHARS
) -> RAGContextResult
```

This function must remain independent of:

- FastAPI (`Depends`, `Request`, route decorators) — it takes a plain `db`
  session and plain arguments, so it is callable identically from a route,
  from Phase 10, or from a standalone test script.
- Ollama / any LLM client.
- The frontend.
- Any agent/orchestration framework.

Internal helper functions this module will likely need (names indicative,
not prescriptive — the implementer should follow existing naming
conventions in the repo):

- A query-validation helper (empty/whitespace check).
- A threshold-filtering helper (Section 11).
- A budget-selection helper (Section 9.2).
- A text-formatting helper (Section 9.3).

Each should be small, independently testable, and free of side effects.

---

## 16. API Decision

**Decision: no new HTTP endpoint.**

Phase 9 is consumed internally by Phase 10, which will call
`build_rag_context(...)` directly in-process. The frontend's eventual need is
the grounded answer produced by Phase 10, not intermediate RAG context.

Do not add `/documents/{document_id}/rag-context` merely for parity with Phase 8.
Manual verification can use the standalone Phase 9 test script or a direct
Python invocation against the running database, following existing repository
conventions.

If implementation reveals a strong existing repository convention that makes a
debug route genuinely useful, flag that deviation in the implementation report
instead of silently expanding the Phase 9 API surface.

## 17. Configuration Changes

Only two new settings, added to wherever `backend/app/config.py` defines
existing settings (matching its existing style — env var name, type,
default, and validation pattern):

| Setting | Type | Default | Validation |
|---|---|---|---|
| `RAG_CONTEXT_MAX_CHARS` | int | `8000` | `> 0` |
| `RAG_MIN_SIMILARITY` | float | `0.3` | `-1.0 <= value <= 1.0` |

No other configuration is added. In particular, no new chunk-count setting
(Section 10.3) and no formatting toggle (`context_text` is always produced
when `status == OK`; there is no setting to disable it, since it is cheap to
compute and keeping it optional would just be another branch to test for no
real benefit).

---

## 18. Dependency Changes

**None.** Phase 9 requires zero new third-party dependencies. It uses only:

- Existing Pydantic (for the new schemas).
- Existing SQLAlchemy session usage (passed through to Phase 8's function
  unchanged).
- Standard library string operations for formatting (Section 9.3).

No tokenizer, no LLM SDK, no Ollama client, no vector database client, no
Redis, no Celery, no LangChain/LangGraph.

---

## 19. File-by-File Implementation Plan

Exact paths depend on repo inspection (Section 27); the plan below states
intent and is written so it can be mapped onto whichever concrete paths the
implementer confirms.

1. **New:** `backend/app/schemas/rag.py` (or extend an existing RAG-related
   schema file if one already exists from Phase 8)
   - `RAGContextStatus` enum
   - `RAGContextChunk` model
   - `RAGContextResult` model

2. **New:** `backend/app/rag/context.py` (or
   `backend/app/services/rag_context_service.py` — see Section 15)
   - `build_rag_context(...)` entry point
   - Private helpers for validation, threshold filtering, budget selection,
     text formatting

3. **Modified:** `backend/app/config.py`
   - Add `RAG_CONTEXT_MAX_CHARS` and `RAG_MIN_SIMILARITY` settings

4. **No new route by default.** Phase 9 remains an internal Python layer.
   Manual verification uses the standalone Phase 9 test convention.

5. **New:** a Phase 9 test script, following the exact convention already
   used for Phase 8's tests (standalone script vs. pytest — match whatever
   is already there; do not introduce pytest if the project currently does
   not use it). Name it to match the existing per-phase test file pattern
   (e.g. mirroring however Phase 8's test file is named).

6. **New:** `docs/phases/PHASE_9_RAG_CONTEXT.md` — this document, saved
   verbatim (with any corrections made during implementation to match actual
   file/function names) into the repository's phase docs.

7. **Modified:** `ARCHITECTURE.md` — add Phase 9 to whatever running
   architecture summary/diagram already documents Phases 1–8, following its
   existing format.

No existing Phase 4–8 file should require modification beyond what's listed
in item 3 and (optionally) item 4. If implementation reveals that Phase 8's
retrieval function needs a change to be reusable by Phase 9 (for example, if
it is currently only reachable as route-coupled code with no separable
function), that is worth flagging back rather than silently duplicating the
query — call this out explicitly rather than working around it.

---

## 20. Function / Class Responsibilities

| Component | Responsibility | Must NOT do |
|---|---|---|
| `build_rag_context` | Orchestrate the full flow in Section 6; return a `RAGContextResult` in all cases | Call Ollama; format an LLM prompt; write to the DB; catch/hide infra errors |
| Query validation helper | Reject empty/whitespace queries before any retrieval call | Attempt any semantic judgment of query quality |
| Threshold filter helper | Given ranked chunks + a similarity floor, return the subset that passes, preserving rank order | Re-sort or re-score chunks |
| Budget selection helper | Given ranked (filtered) chunks + a char budget, mark `included` per Section 9.2 | Truncate any chunk's content |
| Text formatting helper | Given `included=True` chunks, deterministically build `context_text` | Add any prompt/instruction language |
| `RAGContextResult` / `RAGContextChunk` / `RAGContextStatus` | Pure data containers | Contain any logic beyond field validation |

---

## 21. Error Handling

Phase 9 distinguishes **domain outcomes** (Section 12 — always returned as a
normal `RAGContextResult`, never raised) from **infrastructure errors**
(always propagated, never swallowed):

- **Invalid query** → return `RAGContextResult(status=INVALID_QUERY, ...)`.
  No exception. No Phase 8 call.
- **Document not ready / not found** → mirror whatever Phase 8 already does.
  Concretely, during implementation, confirm: does Phase 8's retrieval
  function raise a specific exception type for this case, or return a
  sentinel? Phase 9 must catch that *same* exception/sentinel and translate
  it into `RAGContextResult(status=DOCUMENT_NOT_READY, ...)` — it must not
  invent a new way of detecting this state.
- **Zero chunks / below threshold** → handled entirely within Phase 9's own
  logic as normal outcomes (Sections 8.1, 11.4).
- **Unexpected/infrastructure errors** (DB connection failure, an assertion
  failure from Section 14's isolation check, any exception Phase 8 raises
  that isn't the "not ready" case) → **propagate**, do not catch-and-convert
  to a status. Phase 9 is not responsible for turning genuine bugs or outages
  into a graceful status; only for representing the legitimate "no usable
  context" states.

---

## 22. Test Plan

Match the existing repository test convention exactly (standalone script vs.
pytest — confirm and mirror; do not introduce a new framework solely for this
phase).

Required cases:

1. A query with genuinely relevant chunks produces `status=OK` with non-empty
   `chunks` and `context_text`.
2. `document_id` on the result and on every chunk matches the requested document.
3. `page_number` is preserved and correct for every chunk.
4. `chunk_id` and `chunk_index` are preserved and correct for every chunk.
5. `rank` in the result matches Phase 8's original ranking order exactly.
6. Cross-document isolation: querying document A never returns a chunk whose
   `document_id` is document B.
7. A zero-chunk retrieval produces `NO_CHUNKS_RETRIEVED`.
8. A non-ready/missing document produces `DOCUMENT_NOT_READY` using Phase 8's
   existing semantics.
9. Empty-string and whitespace-only queries produce `INVALID_QUERY`, and Phase 8
   is never called.
10. Weak chunks all below `RAG_MIN_SIMILARITY` produce
    `BELOW_SIMILARITY_THRESHOLD` with `top_similarity` populated.
11. A low budget includes only the highest-ranked complete chunks that fit.
12. Budget accounting includes `[Page X | Chunk Y]` formatting and `\n\n`
    separators; `len(context_text) <= max_context_chars` always holds.
13. A chunk that does not fit is excluded whole and is never partially
    truncated.
14. If the first threshold-passed chunk cannot fit, the result is
    `NO_CHUNK_FITS_BUDGET`, with no included chunks and `context_text=None`.
15. With deliberately overlapping/adjacent chunks, both are preserved unchanged;
    no deduplication occurs.
16. `context_text` is deterministic and reproducible solely from included
    chunks and the fixed rendering rule.
17. Phase 9 calls the existing Phase 8 retrieval function exactly once per
    `build_rag_context` call.
18. Phase 9 performs no direct embedding or pgvector retrieval.
19. No rows are inserted, updated, or deleted during Phase 9.
20. No Ollama/LLM import, client, or call is touched.
21. `RAG_MIN_SIMILARITY` and `RAG_CONTEXT_MAX_CHARS` can be overridden
    independently through function arguments.
22. Full Phase 4, 5, 6, 7, and 8 regression suites pass unmodified.

Also test configuration validation:
- `RAG_CONTEXT_MAX_CHARS > 0`
- `-1.0 <= RAG_MIN_SIMILARITY <= 1.0`

## 23. Manual Verification Steps

1. Start the existing backend as usual.
2. Ingest a real multi-page PDF using the existing Phase 4–7 workflow.
3. Invoke `build_rag_context` through the existing standalone Phase 9 test
   mechanism or a direct Python invocation against the running database.
4. Use a question clearly answerable from the document. Confirm:
   - `status=OK`
   - sensible page numbers and ranks
   - deterministic `[Page X | Chunk Y]` formatting
   - `len(context_text) <= max_chars_used`
5. Use an obviously unrelated question. Confirm:
   - `status=BELOW_SIMILARITY_THRESHOLD`
   - `context_text is None`
6. Use an empty/whitespace query. Confirm `status=INVALID_QUERY`.
7. Temporarily set `RAG_CONTEXT_MAX_CHARS` very low (for example `200`).
   Confirm:
   - no chunk is truncated
   - if no threshold-passed chunk fits, status is `NO_CHUNK_FITS_BUDGET`
   - otherwise `len(context_text) <= 200`
8. Confirm the full existing regression scripts for Phases 4–8 still pass
   unmodified.

## 24. Acceptance Criteria

Phase 9 is complete when:

- [ ] `build_rag_context` exists, is callable independent of FastAPI/Ollama,
      and matches the signature style in Section 15.
- [ ] All fields in `RAGContextResult`/`RAGContextChunk` are populated
      correctly for the `OK` case, verified against real ingested data.
- [ ] All five statuses in Section 12 are reachable and correctly
      distinguished by tests.
- [ ] The character budget is enforced against the exact rendered `context_text`,
      with whole-chunk selection and no truncation (Sections 9–10).
- [ ] The similarity threshold is configurable, validated, and correctly
      gates the `BELOW_SIMILARITY_THRESHOLD` case (Section 11).
- [ ] No deduplication logic was added (Section 13 deferral respected).
- [ ] Document isolation is enforced and defensively checked (Section 14).
- [ ] Zero new dependencies were added.
- [ ] Exactly two new config settings were added, with the exact defaults
      and validation in Section 17.
- [ ] No database writes occur anywhere in the Phase 9 code path.
- [ ] No Ollama/LLM call, import, or client reference exists anywhere in the
      Phase 9 code path.
- [ ] All 21 required test cases (Section 22) exist and pass, using the
      repository's existing test convention.
- [ ] Full Phase 4–8 regression suites pass unmodified.
- [ ] `docs/phases/PHASE_9_RAG_CONTEXT.md` and `ARCHITECTURE.md` are updated.

---

## 25. Phase 10 Integration Boundary

Phase 10 will call `build_rag_context(...)` directly (in-process, not over
HTTP) and branch on `RAGContextResult.status`:

- `OK` → Phase 10 constructs its own system/user prompt using
  `context_text` (or `chunks` directly, if it prefers building its own
  format) and calls Ollama.
- Any other status → Phase 10 decides how to respond (e.g. a canned
  "I couldn't find relevant information in this document" style response)
  **without calling Ollama at all**, or by calling Ollama with an explicit
  instruction that no grounding context was found — that prompt design is
  entirely Phase 10's concern.

Phase 9 guarantees to Phase 10, and Phase 10 may rely on:

- `context_text` (when present) never exceeds `max_chars_used` characters, including all rendering overhead.
- Every chunk in `chunks` belongs to the requested `document_id`.
- `page_number` and `chunk_index` on every chunk are accurate and sufficient
  for Phase 10 to build citations (e.g. "see page 12").
- No side effects occurred — Phase 10 can call `build_rag_context` freely,
  including multiple times per user turn if it ever needs to, without
  worrying about persistence or state.

Phase 9 will never need to change to accommodate a specific Ollama model —
if a future model has a larger/smaller context window, only the
`RAG_CONTEXT_MAX_CHARS` value needs to change (as config, not code).

---

## 26. Risks and Edge Cases

- **Similarity scale mismatch.** If Phase 8's `similarity` field is not
  actually a clean 0–1 cosine similarity (e.g. it's unnormalized, or it's
  actually a distance mislabeled as similarity), `RAG_MIN_SIMILARITY`'s
  default of `0.3` may behave unexpectedly. Mitigation: confirm the exact
  computation in Phase 8's code before wiring the threshold, and adjust the
  default if needed, documenting the change.
- **Very large single chunks.** If Phase 5's chunker ever produces a chunk
  longer than `RAG_CONTEXT_MAX_CHARS` itself, that chunk can never be
  included under the "no truncation" rule, even if it's the top-ranked
  result — it would always be excluded whole. This is an accepted
  consequence of Section 10.4's no-truncation guarantee; if it proves to be
  a real problem in practice, the fix belongs in Phase 5 (chunk sizing), not
  in Phase 9 (which should not start truncating to compensate).
- **Threshold tuned for this document, not others.** Since the project must
  handle very different document types (insurance vs. research papers vs.
  manuals), a single global `RAG_MIN_SIMILARITY` will not be equally right
  for all of them. This is accepted as an MVP simplification per the
  project's explicit "avoid configuration explosion" instruction; a future
  phase could make it per-document if evidence shows it's needed.
- **Debug route scope creep.** If the optional route from Section 16 is
  added, care must be taken that nothing in Phase 10 or the frontend ever
  comes to depend on it — it must remain provably a debug-only convenience.
- **Empty document edge case.** A document that ingested with zero chunks
  (e.g. a corrupted or non-text PDF that produced no extractable content) is
  a distinct case from "no relevant chunks for this query" — Section 8.1
  treats it as `NO_CHUNKS_RETRIEVED`, but the implementer should confirm this
  scenario is actually reachable given Phase 4/7's ingestion guarantees, and
  adjust the test fixture accordingly if it isn't naturally reachable.

---

## 27. Exact OpenCode Implementation Instructions

Follow these steps in order. Do not skip the inspection steps — this
specification deliberately avoids assuming exact file/function/field names
because it was written without direct sight of the repository.

1. **Inspect before writing anything:**
   - `ARCHITECTURE.md`
   - `docs/phases/PHASE_5_CHUNKING.md`
   - `docs/phases/PHASE_6_EMBEDDINGS.md`
   - `docs/phases/PHASE_7_INGESTION_PERSISTENCE.md`
   - `docs/phases/PHASE_8_SEMANTIC_RETRIEVAL.md`
   - `backend/app/config.py`
   - `backend/app/rag/` (all files)
   - `backend/app/services/` (all files)
   - `backend/app/routes/` (specifically wherever `/documents/{document_id}/search` is defined)
   - `backend/app/schemas/`
   - `backend/app/models/`
   - `backend/scripts/`
   - `requirements.txt`
   - Existing Phase 8 test file(s), to learn both the test framework
     convention and how Phase 8's function is called/mocked in tests.

2. **Confirm and record** (as comments or a short note in the PR/commit, not
   in this doc): the real name, location, and signature of Phase 8's
   internal retrieval function; the real field names it returns for
   distance/similarity; its default `top_k`; and how it signals
   "document not ready"/"document not found."

3. **Implement schemas** exactly as specified in Section 8, adjusting field
   types to match the repository's existing ID types (UUID, int, etc.) and
   any existing Pydantic base-model conventions.

4. **Implement `build_rag_context`** and its private helpers per Sections
   6, 9, 11, 15, and 21 — pure orchestration, reusing Phase 8's function
   without modification.

5. **Add the two config settings** from Section 17 to `config.py`, following
   its existing style precisely (naming, env var prefix if any, validator
   pattern).

6. **Decide on the optional debug route** (Section 16) based on actual
   Phase 8 route conventions; implement only if it is a genuinely thin
   wrapper.

7. **Write the test suite** covering all cases in Section 22, using the
   exact test framework/convention already used for Phases 4–8 (inspect
   first — do not introduce pytest if the project uses standalone scripts,
   and vice versa).

8. **Run the full existing regression suite** for Phases 4 through 8
   unmodified and confirm no breakage.

9. **Update `ARCHITECTURE.md`** to include Phase 9 in whatever running
   summary/diagram already exists for Phases 1–8.

10. **Save this document** into `docs/phases/PHASE_9_RAG_CONTEXT.md`,
    correcting any file/function names in Sections 7, 15, 19, and 27 to
    match what was actually confirmed in step 2, so the saved doc reflects
    reality rather than this spec's placeholders.

11. **Do not** implement anything related to Ollama, prompt construction, or
    answer generation — if any such need is discovered during
    implementation, stop and flag it for Phase 10 rather than building it
    here.
