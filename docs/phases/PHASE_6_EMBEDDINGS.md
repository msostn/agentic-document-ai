# Phase 6 Implementation Specification — Local Text Embedding Service

Status: NEXT (to be implemented by a coding agent)
Parent spec: `ARCHITECTURE.md` (permanent source of truth — this phase must not contradict it)
Verified prior state: Phases 1–5 complete and committed. `document_chunks.embedding` is `vector(384) NOT NULL` (Phase 3, unchanged). PDF upload/extraction/CRUD works, 21/21 tests passed (Phase 4). Pure page-aware chunking produces `ChunkRecord` objects (`document_id`, `chunk_index`, `page_number`, `content`), configurable via `CHUNK_SIZE=800`, `CHUNK_OVERLAP=150`, `MIN_CHUNK_SIZE=100`; 17/17 tests passed; writes nothing to the database (Phase 5). No embedding, vector storage, retrieval, or agent logic exists yet.

---

## 1. Phase Objective

Provide a single, reusable, standalone module that converts chunk text into 384-dimensional embedding vectors using a local `sentence-transformers` model — loaded once and reused, batch-capable, order-preserving, and producing output in a shape Phase 7 can attach directly to each `ChunkRecord` and insert into `DocumentChunk.embedding` without transformation. This phase implements the embedding layer only; it is not wired into any route, service, or the chunker itself.

---

## 2. Current Architecture Relevant to Phase 6

- `app/config.py` already declares `embedding_model` (default `all-MiniLM-L6-v2`) from Phase 1 — reused here unchanged.
- `app/rag/chunker.py` (Phase 5) produces `ChunkRecord` objects with a `content: str` field. Phase 6 has no import dependency on `chunker.py` — it only needs to accept plain strings — but its output is designed to slot onto `ChunkRecord.content` values by position once Phase 7 connects the two.
- `document_chunks.embedding` (Phase 3) is `vector(384) NOT NULL` — the fixed target dimension this service must guarantee and validate against, not merely assume.
- `sentence-transformers` and `numpy` are already installed (Phase 1's `requirements.txt`).
- No route, service, or startup hook currently references embeddings in any way; this phase does not change that.

---

## 3. Embedding Model

- **Exact model:** `sentence-transformers/all-MiniLM-L6-v2`, loaded via the `sentence-transformers` library's `SentenceTransformer` class.
- **Expected embedding dimension:** 384. This matches the model's published architecture and must match `document_chunks.embedding`'s fixed `vector(384)` column exactly.
- **Where/how loaded:** loaded from within `app/rag/embeddings.py`, using the model name from `settings.embedding_model` (configurable — see §7) — not hard-coded as a literal string in multiple places.
- **Loaded once and reused:** yes — via a lazy-loaded, process-wide singleton (see §5). The model is never re-instantiated per call, per text, or per request.
- **CPU/GPU behavior:** defaults to CPU (`EMBEDDING_DEVICE=cpu`, new config value, §7). This is a small, fast model — CPU inference is entirely adequate for MVP scale, avoids GPU-memory contention with Ollama (which will also run on the same machine from Phase 9 onward), and matches the project's general preference for CPU-compatible implementations on the development machine. GPU use remains possible by changing the config value, but is not required and not the default.
- **Normalization behavior:** embeddings are returned **unnormalized** (raw model output). `ARCHITECTURE.md` §4 specifies cosine similarity for retrieval, and pgvector's cosine-distance operator computes correct results regardless of vector magnitude — normalizing at embedding time is an optional performance micro-optimization that belongs to Phase 8's retrieval tuning, if ever, not a correctness requirement here. Keeping the service's output un-normalized avoids coupling it to a similarity-metric decision it doesn't own.
- **Deterministic/reproducible expectations:** `sentence-transformers` inference is deterministic — the model runs in evaluation mode (no dropout), so the same input text encoded twice on the same machine, same library versions, same device, produces bit-identical output. Cross-hardware or cross-device (CPU vs. GPU) runs may show negligible floating-point differences; this is expected and not a defect, and is avoided in practice by defaulting to CPU.
- **Cost:** entirely local and free. The only network dependency is a one-time model download (~80 MB) from Hugging Face Hub the first time this model is ever loaded on a given machine; after that, it's cached locally (`~/.cache/huggingface` by default) and runs fully offline. This does not violate the zero-cost/local requirement — it's a one-time asset fetch, not a per-request API call, and no payment or API key is involved.

---

## 4. Embedding Service Design

**Module:** `backend/app/rag/embeddings.py`. Plain functions, no database or HTTP imports — same style as `parser.py` and `chunker.py`.

**Public interface (described, not implemented in code here):**

- A function that accepts **either** a single string **or** a list of strings, and returns embeddings in the corresponding shape (single vector, or list of vectors) — one function, not two parallel single/batch functions, to avoid unnecessary API surface. Internally, a single string is treated as a batch of one.
- Behavior for **empty input**:
  - A single empty or whitespace-only string → raises `ValueError` (see §9). This is a defensive guard; in normal operation Phase 5's `MIN_CHUNK_SIZE` filtering already prevents near-empty content from reaching this point.
  - An empty list (`[]`) passed for batch encoding → returns an empty list. This is not an error — "embed nothing" is a valid, harmless no-op.
- **Configurable batch size:** when encoding a list of texts, the underlying `sentence-transformers` batching (`batch_size` parameter) is used, sized from `settings.embedding_batch_size` (new config, §7) rather than hard-coded, so it can be tuned for different hardware without a code change.
- No unnecessary abstractions: no embedding "provider" plugin system, no class hierarchy, no async wrapper. A small set of plain functions is sufficient for this phase and for what Phase 7 needs to call.

---

## 5. Model Loading Strategy

**Decision: lazy loading with a module-level singleton, guarded for thread safety.**

- The model is **not** loaded at import time and **not** loaded eagerly at FastAPI startup in this phase — there is no running application context to attach a startup hook to yet, since nothing calls this module until Phase 7. A lazy singleton works identically whether called from a standalone test script (this phase) or, later, from inside the live app (Phase 7), requiring no rework either way.
- The first call that needs the model triggers loading; the loaded `SentenceTransformer` instance is cached at module level for the lifetime of the process. All later calls reuse that same instance — never re-instantiated per call, per text, or per request.
- **Thread safety:** although Phase 6 itself is exercised only by a single-threaded test script, the eventual FastAPI/Uvicorn process (from Phase 7 onward) can receive concurrent requests. The lazy-load path must guard against two concurrent first-callers both triggering a model load simultaneously — a simple `threading.Lock` around the "load if not already loaded" check is sufficient. This is a small, well-understood safety measure, not over-engineering.
- **Model-loading failure handling:** any failure (invalid model name, no internet on a genuinely first-ever run with an empty cache, corrupted local cache, insufficient memory) raises a clear, custom `EmbeddingModelLoadError` with an actionable message — never a raw, unhandled library traceback.
- This strategy is intentionally simple: no dependency-injection framework, no external model-serving process, no lifecycle manager beyond the guarded singleton described above.

---

## 6. Input/Output Contract

```
Input:  "some chunk text"                         (str)
        OR ["text one", "text two", ...]          (list[str])
   ↓
Output: [0.0123, -0.045, ..., 0.021]              (list[float], length 384)
        OR [[...], [...], ...]                    (list[list[float]], each length 384)
```

- **Output type:** plain Python `float` values inside plain Python `list`s — never `numpy.ndarray` or `numpy.float32` returned to the caller. The service performs the numpy→list conversion internally (e.g. `.tolist()` on the model's output array), so callers — including Phase 7's future insert code — receive a JSON-serializable, `pgvector`/SQLAlchemy-ready shape with no numpy awareness required.
- **Shape:** exactly 384 floats per input string, always. This is verified at runtime (see below), not merely documented.
- **Order preservation:** for batch input, output list index `i` corresponds to input list index `i`, guaranteed — this is what lets Phase 7 zip embeddings back onto their originating `ChunkRecord` list purely by position.
- **Dimension validation:** immediately after the model loads (once, at first use), the service reads the model's own reported embedding dimension (`get_sentence_embedding_dimension()`) and compares it against a fixed internal constant of `384`. If they don't match, the service raises `EmbeddingDimensionMismatchError` before any embedding is attempted — this guards against someone changing `EMBEDDING_MODEL` to an incompatible model and only discovering the mismatch as a confusing database error later in Phase 7.
- **Compatibility with `DocumentChunk.embedding = Vector(384)`:** the output shape is exactly what that column expects — a length-384 sequence of floats — with no modification to that column required or made in this phase.
- **Floating-point handling:** no rounding or truncation is applied; full float precision from the model is preserved through the numpy→list conversion.

---

## 7. Configuration

Reused, unchanged:
- `EMBEDDING_MODEL` — default `all-MiniLM-L6-v2`. Configurable, but changing it to a model with a different native output dimension will trip the dimension-validation guard in §6 rather than silently corrupting data.

New:
- `EMBEDDING_BATCH_SIZE` — default `32`. Passed to `sentence-transformers`' internal batching when encoding a list of texts. A legitimate hardware-tuning knob, not a correctness-sensitive value.
- `EMBEDDING_DEVICE` — default `"cpu"`. Passed to the model at load time. Not required to be `"cuda"`; CPU is the safe, sufficient default for this model size.

**Deliberately not configurable:** the expected embedding dimension (`384`) is a fixed constant in code, not an environment variable. This directly follows the instruction to avoid configuration values that can create dangerous mismatches with the database: if dimension were an env var, someone could set it to a value that doesn't match the schema and doesn't match the actual model output either, and the system would have no way to catch that until an insert failed downstream. Instead, the *model name* is configurable (as required), and the dimension is *derived from the model itself at load time* and checked against the fixed schema expectation — the environment can influence which model is used, but never what dimension the system believes is correct.

---

## 8. Dependencies

No new packages. `sentence-transformers` and `numpy` were already added to `backend/requirements.txt` in Phase 1, along with their necessary transitive runtime dependencies (`torch`, `transformers`, `huggingface-hub`, `tokenizers`, `tqdm`, `scikit-learn`, `scipy`, `Pillow`, `regex`, `safetensors`, all pulled in automatically by `sentence-transformers`'s own dependency chain — no need to list these individually in `requirements.txt`). Phase 6's only action here is to **verify** these are importable in the existing `.venv`, not to add or change anything in `requirements.txt`.

Explicitly not added: `LangChain`, `LangGraph`, any OpenAI/Anthropic/Google/paid embedding client library, or any additional vector database client. None of these are needed for a local `sentence-transformers` embedding layer.

---

## 9. Error Handling

| Scenario | Behavior |
|---|---|
| Empty string (`""`) | Raise `ValueError` with a clear message before attempting to encode |
| Whitespace-only string (`"   "`) | Same as empty — raise `ValueError` |
| Empty list (`[]`) for batch input | Return `[]` — not an error |
| Invalid input type (e.g. `None`, a number, a nested list) | Raise `TypeError` with a clear message |
| Model fails to load (bad model name, no internet on a true first run, corrupted cache, insufficient memory) | Raise `EmbeddingModelLoadError` with an actionable message; never a raw library traceback |
| Loaded model's actual dimension ≠ 384 | Raise `EmbeddingDimensionMismatchError`, explaining the mismatch between the configured model and the fixed database schema expectation |
| Unexpected exception during encoding (e.g. a corrupted/mid-load model object) | Caught and re-raised as a clear `EmbeddingGenerationError`, not left as an opaque low-level exception |
| Model already loaded, called again | No reload; cached singleton instance is reused (see §5) |

All custom exceptions above should be simple, dedicated exception classes (not generic `Exception`), so callers — including Phase 7 later — can catch them specifically if needed.

---

## 10. Testing Plan

All tests are deterministic and run via a standalone script — no live database, no HTTP server, consistent with the pattern already used in Phases 3 and 5.

**Important distinction — first-run model download vs. normal test execution:** the very first time any test in this suite runs on a machine, `sentence-transformers` must download the model (~80 MB, one-time, requires internet). This is expected and should be clearly logged/printed by the test script (e.g. "Downloading model — first run only…") so it isn't mistaken for a hang or a bug. Every subsequent run on that machine uses the local cache and requires no network access. Tests must not fake or mock this download away — they should exercise the real model, since the entire point of this phase is verifying that real, local, deterministic embeddings work correctly.

**TEST 1 — Model loads successfully**
Trigger the first encode call. Assert it completes without raising, and that the singleton is populated afterward.

**TEST 2 — Single text embedding**
Encode one sample sentence. Assert the result is a `list` of `float`, length 384.

**TEST 3 — Batch embedding**
Encode a list of 5 distinct sentences in one call. Assert the result is a list of 5 vectors, each length 384.

**TEST 4 — Correct dimension**
For both single and batch results, assert every returned vector's length is exactly 384 — this is the test that would catch a misconfigured model.

**TEST 5 — Output format**
Assert returned values are plain Python `float` (not `numpy.float32`) and the outer container is a plain `list` (not `numpy.ndarray`) — this is what makes the output directly usable by SQLAlchemy/pgvector later without extra conversion.

**TEST 6 — Empty input handling**
Assert `""` and `"   "` each raise `ValueError`. Assert `[]` returns `[]` without error.

**TEST 7 — Ordering preserved across multiple inputs**
Encode a batch of texts with clearly distinguishable content (e.g. numbered sentences). Encode each individually afterward. Assert each individual result matches (within tight floating-point tolerance) the corresponding position in the batch result — proves both ordering and batching correctness together.

**TEST 8 — Model reuse / no repeated initialization**
Call the encode function multiple times across the script. Using a load counter or an object-identity check on the cached model, assert the underlying model is constructed exactly once for the whole test run, regardless of call count.

**TEST 9 — Dimension validation triggers correctly**
If feasible without adding a new dependency (e.g. by directly checking the validation function against a fabricated "wrong dimension" value, not necessarily loading an actual different model), assert `EmbeddingDimensionMismatchError` is raised when the check fails. This may be tested as a unit-level check on the validation logic in isolation, rather than requiring a second real model download, to keep the test suite fast and dependency-light.

**TEST 10 — Configuration behavior**
Assert that `EMBEDDING_BATCH_SIZE` and `EMBEDDING_DEVICE` are read from `settings` (not hard-coded) — e.g. by confirming the encode function's behavior/signature references these config values, or by testing with a non-default batch size and confirming it doesn't error and still produces correctly-shaped output.

**Determinism check (folded into Test 2/3):** encoding the same sentence twice in the same run must produce identical vectors — assert exact equality, not just "close enough," since this should be bit-reproducible on the same machine/process.

---

## 11. Testing/Verification Commands

Run from the `backend` directory, with the existing Windows virtual environment:

```powershell
cd backend
.venv\Scripts\Activate.ps1
python scripts\test_embeddings.py
```

Expected first-run output includes a one-time model download notice, followed by all test results. Expected output on every subsequent run (same machine): no download step, all tests pass immediately from the local model cache. If `sentence-transformers` or a dependency appears missing, run:

```powershell
pip install -r requirements.txt
```

before retrying — no new packages should actually be needed per §8, so this is a verification step, not an expected fix.

---

## 12. Performance Considerations

- **Model loading cost:** loading `all-MiniLM-L6-v2` typically takes a few seconds on a laptop CPU (after the one-time download). This cost is paid once per process (thanks to the singleton in §5), not once per chunk or per request — this is the whole reason the singleton pattern matters.
- **Batch processing:** encoding many chunks in a single batched call is meaningfully faster than looping single-text calls, because the model can process multiple inputs together on the same forward pass rather than paying fixed overhead per call. This matters directly for Phase 7, which will need to embed potentially dozens or hundreds of chunks per uploaded document — batching there, using this service's batch path, is strongly preferable to looping.
- **CPU vs. GPU:** this model is small enough (~80 MB, 6 transformer layers) that CPU inference is fast enough for MVP scale (a batch of a few dozen short chunks typically encodes in well under a second on a modern laptop CPU). GPU would help more at much larger volumes, but isn't necessary here, and defaulting to CPU avoids contending with Ollama for the same GPU once Phase 9 introduces it.
- **Memory considerations:** the loaded model occupies a modest, roughly fixed amount of memory once loaded — this is a one-time cost per process, not something that grows with the number of documents or chunks processed, since the model itself doesn't accumulate any per-document state.
- **No premature optimization:** no caching of embeddings for repeated identical text, no async/parallel batching infrastructure, no GPU auto-detection logic, and no dynamic batch-size tuning are implemented in this phase — these would be optimizations to consider only if real usage shows they're needed, not defaults for an MVP.

---

## 13. Phase 7 Integration Boundary

Phase 7 will, for each `ChunkRecord` produced by Phase 5's chunker:
1. Collect all `ChunkRecord.content` values for a document into a single list, preserving order.
2. Call this phase's batch-encode function once with that list (not once per chunk) — this is precisely why order preservation (§6) and batch support (§4) are required now.
3. Zip the returned list of embedding vectors back onto the original `ChunkRecord` list by position (index `i` of the embeddings corresponds to index `i` of the chunk records).
4. Construct `DocumentChunk` ORM instances using each `ChunkRecord`'s `document_id`, `chunk_index`, `page_number`, `content`, plus the matching embedding vector, and insert them into `document_chunks`.

Phase 6 is responsible for producing correct, correctly-ordered, schema-compatible vectors. It is explicitly **not** responsible for — and this phase does not implement — reading `ChunkRecord`s, constructing `DocumentChunk` instances, opening a database session, or performing any insert. That entire connective step belongs to Phase 7.

---

## 14. Files to Create / Modify

**Created:**
- `backend/app/rag/embeddings.py` — the embedding service described in §3–§6.
- `backend/scripts/test_embeddings.py` — the standalone test script described in §10–§11.

**Modified:**
- `backend/app/config.py` — add `embedding_batch_size` and `embedding_device`; `embedding_model` is reused unchanged.
- `backend/.env` and `backend/.env.example` — add `EMBEDDING_BATCH_SIZE` and `EMBEDDING_DEVICE`.

**Not modified:**
- `backend/app/rag/chunker.py`, `backend/app/rag/parser.py` — no changes; no dependency in either direction.
- `backend/app/models/document_chunk.py` — no schema change; `Vector(384)` untouched.
- `backend/app/services/document_service.py`, `backend/app/routes/documents.py`, `backend/app/main.py` — not touched; embeddings are not wired into any live flow in this phase.
- `backend/requirements.txt` — no new packages (§8).

---

## 15. Acceptance Criteria

- [ ] `app/rag/embeddings.py` exists, contains no database or HTTP imports, and exposes one function that accepts either a single string or a list of strings.
- [ ] The `sentence-transformers` model is loaded exactly once per process via a thread-safe lazy singleton, and reused for every subsequent call (Test 8).
- [ ] Output is always plain Python `list`/`float`, never `numpy.ndarray`/`numpy.float32` (Test 5).
- [ ] Every returned vector has exactly 384 elements, verified at runtime against the model's own reported dimension, not merely assumed (Test 4).
- [ ] Output order exactly matches input order for batch calls (Test 7).
- [ ] Empty/whitespace strings raise `ValueError`; an empty list returns `[]`; invalid types raise `TypeError` (Test 6).
- [ ] `EMBEDDING_MODEL`, `EMBEDDING_BATCH_SIZE`, and `EMBEDDING_DEVICE` are all read from configuration; the embedding dimension itself is a fixed internal constant, not an environment variable (§7).
- [ ] Model-loading failure raises `EmbeddingModelLoadError`; a dimension mismatch raises `EmbeddingDimensionMismatchError`; unexpected encoding failures raise `EmbeddingGenerationError` — none surface as raw/unhandled library exceptions (§9).
- [ ] No changes were made to `document_chunks`, `documents`, or any other table; `Vector(384)` is untouched.
- [ ] No code path in this phase writes to the database, calls a route, or is invoked from `document_service.py`, `routes/documents.py`, or `main.py`.
- [ ] No vector storage, similarity search, RAG, LLM, Ollama, agent, chat, frontend, or authentication code was introduced.
- [ ] `backend/scripts/test_embeddings.py` runs via the exact commands in §11 and Tests 1–10 in §10 pass deterministically, with the first-run model download clearly distinguished from normal execution in the script's output.

---

## 16. Explicit Out-of-Scope List

- Writing embeddings to `document_chunks` or any database table.
- Connecting this service to `chunker.py`'s `ChunkRecord` output as part of a live pipeline (Phase 7).
- Vector similarity search, semantic retrieval, or any `pgvector` query logic (Phase 8).
- Any new API endpoint, or any change to `main.py` routing.
- Eager model loading at FastAPI application startup (left as an optional decision for whoever wires this into the live app in Phase 7).
- RAG pipeline assembly, LLM calls, Ollama integration, agent/tool-calling logic, or chat functionality.
- Frontend changes of any kind.
- Authentication.
- `LangChain`, `LangGraph`, or any paid embedding API (OpenAI, Anthropic, Google, or otherwise).
- Any additional vector database beyond the existing Supabase/pgvector setup.
- Changing `DocumentChunk.embedding`'s type, dimension, or nullability.
- GPU auto-detection, dynamic batch-size tuning, or embedding caching for repeated text.
