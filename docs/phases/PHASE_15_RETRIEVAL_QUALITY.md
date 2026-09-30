# Phase 15 — Retrieval Quality Improvement & Evaluation-Driven RAG Optimization

Status: **complete, uncommitted (awaiting review).**
Spec: `docs/phases/PHASE_15_SPEC.md`.

---

## 1. What Phase 15 changed

| Setting | Phase 14 | Phase 15 |
|---|---|---|
| `CHUNK_SIZE` | 800 | **500** |
| `CHUNK_OVERLAP` | 150 | **100** |
| `RAG_MIN_SIMILARITY` | 0.30 | 0.30 (**unchanged**) |
| `RETRIEVAL_TOP_K_DEFAULT` / `_MAX` | 5 / 20 | 5 / 20 (unchanged) |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | unchanged |

Nothing else in the retrieval path changed: no query rewriting, no second
embedding call, no reranker, no extra LLM call, no agent-limit change, no new
endpoint, no new dependency, no schema/index change.

Files touched:

| File | Change |
|---|---|
| `backend/app/config.py` | `CHUNK_SIZE` 800→500, `CHUNK_OVERLAP` 150→100 |
| `backend/.env.example` | same values; documents `RAG_MIN_SIMILARITY`, `RAG_CONTEXT_MAX_CHARS` |
| `backend/.env` (gitignored) | same values, so native and Docker runs agree |
| `eval/golden_sets/sample_insurance_policy.json` | 8 → 21 cases, `version: 2`, category notes |
| `backend/tests/test_phase15_units.py` | **new** — 19 unit tests |
| `backend/scripts/test_phase15_retrieval_quality.py` | **new** — 25 live checks |
| `ARCHITECTURE.md` | new §15 "Phase 15 — Retrieval Quality Improvement" |
| `README.md` | Retrieval Quality section, test commands, env-var table rows |
| `eval/reports/*` | baseline, after, and Docker evaluation reports |
| `docs/phases/PHASE_15_RETRIEVAL_QUALITY.md` | this document |

---

## 2. Baseline (recorded before any change)

`python evaluate.py` against the Phase 14 build, 8-case golden set
(report `eval/reports/20260929_171123_sample_insurance_policy.md`, identical
to the committed Phase 14 reports `20260929_015638` / `20260929_015944`):

```
Total cases: 8 (answerable=5, unanswerable=3, returned sources=4)
Passed: 7   Failed: 1
  - Retrieval hit-rate: 80.0%
  - Citation validity:  100.0%
  - Correct rejection:  100.0%
  - Keyword heuristic:  57.14%
```

The single failure was `case-005` — *"Is there a waiting period for
pre-existing conditions?"* → `context_status=below_similarity_threshold`,
`sources=[]`, `answer=null`.

Baseline retrieval tests: `scripts/test_phase8_retrieval.py` 32/32,
`scripts/test_phase9_rag_context.py` 29/29.

Baseline configuration record: embedding `all-MiniLM-L6-v2` (384-d, CPU),
`CHUNK_SIZE=800`, `CHUNK_OVERLAP=150`, `MIN_CHUNK_SIZE=100`,
`RETRIEVAL_TOP_K_DEFAULT=5`, `RETRIEVAL_TOP_K_MAX=20`,
`RAG_MIN_SIMILARITY=0.30`, pgvector cosine HNSW index
`ix_document_chunks_embedding_hnsw_cosine` (pgvector defaults m=16,
ef_construction=64), stored vector metric `vector_cosine_ops`.

---

## 3. Root cause — chunk-level embedding dilution

Evidence gathered against the **live model and live document** (offline
diagnostics kept outside the repository, in the session temp directory):

| Measurement | Value | Reading |
|---|---|---|
| Sample policy chunks under Phase 14 config | 2 (both page 1) | chunk 0 = sections 1–3, chunk 1 = sections 4–6 |
| Chunk 0 vs case-005 (the chunk that holds the answer) | **0.2338** | below 0.30 |
| Chunk 1 vs case-005 | 0.2679 | below 0.30 |
| Section 3 text alone vs case-005 | **0.6494** | wording matches well |
| Section 3 alone vs chunk 0 | 0.4597 | the section is diluted by the rest of the page |
| Chunk 0 vs chunk 1 | 0.8192 | near-duplicates caused by the 150-char overlap |
| Stored embeddings vs freshly computed embeddings | cosine **1.000000** | no embedding-model or index mismatch |
| case-004's best chunk (the only other marginal case) | 0.2991 | 0.0009 above the bar — margins were already razor-thin |

**Diagnosis:** the question and the answer text match well in isolation
(0.649), but embedding an entire multi-section page into a single vector pulls
the answer-bearing chunk below the threshold (0.234). The failure was caused
by **oversized chunks**, not by the embedding model, not by the pgvector
metric/index, and not by threshold arithmetic (every other case cleared 0.30,
two of them by less than 0.001).

Grid search over chunk parameters (offline, real model), top-1 similarity for
case-005 / unanswerable controls:

| size/overlap | chunks | case-005 | case-006 | case-007 |
|---|---|---|---|---|
| 800/150 (Phase 14) | 2 | 0.268 **fail** | 0.059 | 0.124 |
| 700/150 | 3 | 0.424 | 0.059 | 0.124 |
| 600/150 | 3 | 0.409 | 0.059 | 0.124 |
| **500/100 (chosen)** | 4 | **0.477** | 0.059 | 0.124 |
| 450/100 | 4 | 0.489 | 0.059 | 0.124 |
| 300/75 | 6 | 0.552 | 0.059 | 0.124 |

500/100 was chosen because it clears the bar with real margin (0.477 vs
0.30) while keeping chunks large enough to hold a whole policy section, and
because a 100-char overlap at 500 chars restores the 20 % overlap ratio the
chunker was designed around (150/800 ≈ 19 %).

### Alternatives considered and rejected

| Alternative | Why rejected |
|---|---|
| Lower `RAG_MIN_SIMILARITY` to ~0.20 | Treats the symptom; weakens the global rejection bar for every query. case-004's primary chunk sat at 0.2991, so a threshold change would be tuned to near-miss numbers rather than to evidence. |
| Replace the embedding model | `all-MiniLM-L6-v2` matched the source text at 0.649 in isolation; spec §9 forbids swapping models without a compelling reason. |
| LLM query rewriting / HyDE | Adds an LLM call to the retrieval path (spec §18/§9). |
| Cross-encoder or external reranker | Explicitly out of scope (spec §9). |
| Case-005-specific exception | Explicitly forbidden — the fix must be general. |

---

## 4. Before / after evaluation

Three measurements, all from `backend/evaluate.py` reports:

| Metric | **Baseline** (Phase 14 code, 8 cases) | **After** (Phase 15 code, same 8 cases) | **After** (Phase 15 code, 21 cases) |
|---|---|---|---|
| Passed | 7 / 8 | **8 / 8** | **21 / 21** |
| Retrieval hit-rate | 80.0 % | **100.0 %** | **100.0 %** |
| Citation validity | 100.0 % | **100.0 %** | **100.0 %** |
| Correct rejection | 100.0 % | **100.0 %** | **100.0 %** |
| Keyword heuristic (weak) | 57.14 % | 71.43 % | 75.0 % |
| Report | `20260929_171123_sample_insurance_policy.*` | `20260930_165102_golden_baseline8.*` | `20260930_164953_sample_insurance_policy.*` |

The middle column is the apples-to-apples comparison: the *same* 8 cases,
re-run against the new build, so the improvement is not an artefact of the
dataset expansion. Column 3 is the expanded dataset that ships with Phase 15.
The report run is reproducible — it was executed twice (2026-09-29 and
2026-09-30) with identical numbers.

Measured top-1 similarity on the sample document:

| Query | Before | After |
|---|---|---|
| case-001 deductible | 0.5xxx | 0.5636 |
| case-002 cancellation notice | 0.5xxx | 0.5320 |
| case-003 coverage limit | 0.5xxx | 0.5661 |
| case-004 claim filing deadline | 0.2991 | 0.5424 |
| **case-005 pre-existing waiting period** | **0.2679** | **0.4770** |
| case-015 paraphrase of case-005 | n/a | 0.6490 |
| case-006 weather (must be rejected) | 0.0590 | 0.0590 |
| case-007 World Cup (must be rejected) | 0.1244 | 0.1244 |
| case-019 capital of France | n/a | 0.0275 |
| case-020 Pride and Prejudice | n/a | 0.0350 |
| case-021 chocolate cake | n/a | −0.0190 |

---

## 5. Golden dataset expansion (8 → 21 cases)

`eval/golden_sets/sample_insurance_policy.json`, `version: 2`, with a `notes`
block recording the category coverage and the rationale for what was *not*
added. Every new case was probed against the live API before being added; no
case was added or removed to chase a score.

- direct facts: case-001, 003, 004, 005, 009, 010
- paraphrase: case-002, 012, **015 (independent rewording of case-005 — proves the fix is not wording-specific)**
- terminology mismatch: case-016 (*submitted* vs *filed*), 014 (*pro-rated refund*), 018 (*deductible* in §1 and §6)
- answer near a chunk boundary: case-013 (end of §2)
- repeated/common terms across sections: case-017, 018
- policy-specific terminology: case-009, 011, 014
- unanswerable / must stay rejected: case-006, 007, 019, 020, 021 (+ case-008 empty query)

**Deliberately not added as refusals:** document-adjacent distractors such as
*dental implant cover*, *maternity waiting period*, *coinsurance*,
*prescription drugs*. A cosine threshold is **topical, not semantic**: those
questions now score 0.37–0.50 and legitimately retrieve related policy text.
Rejecting them would require a semantic judgement, not a vector cut-off. They
are covered instead by `scripts/test_phase15_retrieval_quality.py`, where the
assertion is the invariant that actually matters — they may only ever return
chunks owned by the queried document.

**Not covered:** page-specific questions. The sample policy is a single page,
so no multi-page fixture exists. Recorded as a known limitation instead of
being silently skipped.

---

## 6. Test results

| Suite | Command | Result |
|---|---|---|
| Phase 15 unit tests | `python -m pytest tests/test_phase15_units.py` | **19 / 19** |
| Phase 15 live retrieval tests | `python scripts/test_phase15_retrieval_quality.py` | **25 / 25** |
| All `tests/` (Phase 14 + isolation + Phase 15) | `python -m pytest tests` | **46 / 46** |
| Phase 1–14 regression scripts | `scripts/test_*.py` | **242 / 242** (17 + 12 + 19 + 32 + 29 + 48 + 51 + 15 + 19) |
| Local E2E (health, upload, search, ask, rejection, metrics, isolation, delete) | HTTP probe | **16 / 16** |
| **Total** | | **345 checks, 0 failures** |

Phase 15 test coverage (spec §14):

1. case-005 regression — TEST 7 (0.4770 ≥ 0.30), TEST 8 (best chunk contains
   the answer), TEST 22 (context status `ok`), TEST 25 (live evaluation
   document).
2. general, not hard-coded — the same assertion is applied to *all six*
   answerable queries, plus case-015, a reworded version of case-005.
3. document scoping — TEST 19/20 (live two-document retrieval), plus the
   pre-existing `tests/test_cross_document_isolation.py` and Phase 8/9 suites.
4. unrelated questions rejected — TEST 14 × 5 queries, TEST 23, E2E 10,
   golden cases 006/007/019/020/021.
5. source metadata — TEST 21 (`RetrievalResult` field set, `similarity =
   1 − distance`, page/chunk indices), TEST 24, E2E 5/7/9.
6. chunking change — TEST 4 (4 chunks, all ≤ 500), TEST 5 (Section 3 answer
   sentence not split), TEST 6 (no dropped content), plus 5 unit tests
   including determinism and budget checks.
7. threshold behaviour unchanged — TEST 2 and the `test_similarity_threshold_was_not_lowered`
   unit test (0.30).
8. existing suites — Phase 4–14 scripts all pass unchanged.

---

## 7. Performance

Measured with the Phase 14 `/metrics/runtime` counters, same 7-question probe
(5 answerable + 2 out-of-scope), warm process:

| Metric | Baseline | Phase 15 |
|---|---|---|
| Retrieval per `/ask` (avg) | 193.96 ms | **181.76 ms** (earlier run 159 ms) |
| Context building per `/ask` (avg) | 0.20 ms | 0.07 ms |
| Total `/ask` (avg over 7) | 5979 ms | 6371 ms |
| Embedding calls per `/ask` | 1 | 1 (unchanged) |
| LLM calls in the probe | 4 of 7 | 5 of 7 |
| Agent follow-up tool calls | 0 | 0 (unchanged, limit still 3) |

- Retrieval did not regress; it is marginally faster despite the document now
  having 4 chunks instead of 2.
- Total `/ask` is ~6 % higher for one reason: `case-005` now reaches the model
  because it has evidence (≈5–10.7 s of generation) where it previously
  returned a rejection in 0.4 s. That is the fix working, **not** a new
  LLM call added to the pipeline — the code path is unchanged, the query just
  clears the threshold now.
- Logs confirm exactly one `POST /api/chat` per answered request and one
  embedding batch per retrieval; rejected requests do one embedding and zero
  LLM calls.
- Ingestion embeds 4 chunks instead of 2 for the sample policy — a one-time
  upload-time cost, not a query-time cost.

---

## 8. Security verification

- **Document isolation** — `tests/test_cross_document_isolation.py`,
  Phase 8 (32/32), Phase 9 (29/29), Phase 15 TEST 19/20, local E2E 13 and
  Docker E2E 13 all pass: no query can return another document's chunks.
- **Source validity** — citation validity 100 % on both datasets; every cited
  `chunk_id` was re-resolved through `/search` for the owning document
  (evaluate.py's black-box inventory check, and E2E 9/12).
- **No fabricated sources** — `AnswerSource` is still
  `{chunk_id, chunk_index, page_number}`, backend-derived only.
- **No sensitive logging** — Phase 14's `test_log_query_event_only_emits_safe_keys`
  still passes; no logging code was changed.
- **No secrets** — `backend/.env` remains gitignored; the tracked diff
  contains only the `postgresql+psycopg://…[YOUR-PASSWORD]@…` placeholder in
  `.env.example`. A scan of the full diff for credentials/tokens found nothing.
- **No raw embeddings exposed** — `/search` returns no embedding field (E2E 5).
- **No prohibited additions** — `requirements.txt` is byte-identical to HEAD;
  a repository-wide scan for LangChain/LangGraph/LlamaIndex/Redis/Celery/
  Elasticsearch/rerankers/paid-API clients/new vector stores finds only the
  pre-existing *guard tests* that assert their absence (Phase 10 TEST 42 and
  Phase 11 TEST 41, both passing).

---

## 9. Docker verification

```text
docker compose build      → agentic-document-ai-backend + -frontend built (exit 0)
docker compose up -d      → both containers Up, 8000 + 5173 published
GET /health               → {"status":"ok"}
GET /health/ready         → {"database":"ok","ollama":"ok"}   (host Ollama reachable)
GET /documents            → 200
GET http://localhost:5173 → 200 (frontend unaffected)
Local E2E probe           → 16/16, incl. "ingestion produced Phase 15 chunking
                            (4 chunks at 500 chars)" — the container picks the
                            values up from env_file, so native and Docker agree
python evaluate.py        → 21/21, 100.0% / 100.0% / 100.0%, heuristic 75.0%
docker compose down       → containers and network removed (exit 0)
```

Docker evaluation matches local evaluation exactly (same shared database, same
configuration values). No new service, container, volume, or network was added.

---

## 10. Known limitations / human review items

1. **Topical, not semantic, rejection.** Questions about topics the document
   does not cover but that sit next to covered topics (dental, maternity,
   coinsurance, prescription drugs) now clear the threshold and receive a
   grounded `ok` context plus an honest *"the document does not state this"*
   answer. Refusal scoring therefore uses genuinely out-of-scope questions.
   If product intent is to reject those too, it needs a semantic check — which
   is out of scope for Phase 15 and would require an architecture decision.
2. **Chunking change needs re-ingestion.** Existing documents keep their old
   chunks until re-ingested (`POST /documents/{id}/ingest?force=true`).
   Documents created before Phase 15 will not benefit until re-ingested —
   documented in README and ARCHITECTURE §15.5.
3. **Single-page fixture.** The golden set cannot exercise page-specific
   retrieval; a multi-page synthetic document would be the next dataset
   improvement.
4. **Bare `pytest` at `backend/` collects `scripts/*.py`.** The repo's
   documented command is `python -m pytest tests` (46/46). Running bare
   `pytest` collects the standalone regression scripts as if they were test
   modules, which leaks `timing.add_context_building_ms()` into a `ContextVar`
   and fails `tests/test_phase14_units.py::TestContextAccumulator`. This is
   **pre-existing** (reproduced with only Phase 9 + Phase 14 files, no Phase 15
   code) and unrelated to Phase 15. Suggested one-line follow-up for a human to
   approve: add `testpaths = tests` to `backend/pytest.ini`.
5. **Line-ending noise.** `git status` reports ~10 files as modified
   (`.gitignore`, `requirements.txt`, models, etc.) but `git diff` shows **zero
   content changes** in them — they are LF/CRLF normalisation artefacts under
   `core.autocrlf=true`. Only the three content changes listed in §1 plus the
   new files are real.
6. **No commit has been made.** Everything is left in the working tree for
   review.
