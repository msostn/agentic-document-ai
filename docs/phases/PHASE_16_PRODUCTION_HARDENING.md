# Phase 16 — Production Hardening, Reliability & Release Quality

---

## 1. Objective

Make the existing system harder to break, easier to reproduce, easier to test
and easier for another engineer to run — **without** changing the architecture,
the retrieval baseline, the API contracts or the dependency set.

Six areas, per `PHASE_16_SPEC.md` §6:

1. Configuration and environment hardening
2. API reliability and error consistency
3. Ingestion/re-ingestion reliability
4. Test and regression infrastructure hardening
5. Docker/release reproducibility
6. Operational documentation and failure-mode documentation

Engineering rule applied to every candidate change:

```text
measure -> identify failure -> smallest justified change -> test -> evaluate
-> verify Docker -> document
```

---

## 2. Starting baseline

| Item | Value |
|---|---|
| Commit | `7dbabc0` (`feat: improve retrieval quality with optimized chunking`) |
| Completed phases | 1–15 |
| Backend tests | 46 passing (`backend/tests`, 3 files) |
| Phase regression scripts | 10 drivers, 267 checks, 0 failed |
| Frontend tests | 41 passing (8 files) |
| Golden evaluation | 21/21, 100% retrieval / 100% citation / 100% rejection (`eval/reports/20260930_183114_sample_insurance_policy.*`) |
| Retention baseline | `CHUNK_SIZE=500`, `CHUNK_OVERLAP=100`, `RAG_MIN_SIMILARITY=0.30`, `RETRIEVAL_TOP_K_DEFAULT/MAX=5/20`, agent caps 3/3 |
| Working tree | 10 files showing line-ending-only diffs (no hunks); untracked: `docs/phases/PHASE_15_SPEC.md`, `docs/phases/PHASE_16_SPEC.md`, `opencode.json` |
| Environment | Windows, system Python 3.13.7 (pytest 9.1.1), `backend/.venv` (no pytest), Ollama local, Supabase pgvector |

Nothing in the baseline was treated as "probably true": every claim below was
re-measured in this session.

---

## 3. Repository audit

Read in full: `PHASE_16_SPEC.md`, `README.md`, `DEPLOYMENT.md`,
`ARCHITECTURE.md` (§7 API contracts, §15), `PHASE_15_RETRIEVAL_QUALITY.md`,
`backend/app/{main,config,database}.py`, `routes/documents.py`,
`services/{document,ingestion}_service.py`, `agent/{loop,tools}.py`,
`llm/ollama_client.py`, `rag/{retriever,context,parser}.py`, `timing.py`,
`models/*`, `schemas/*`, `scripts/init_db.py`, `pytest`/Docker configuration,
and the whole React app (`App.tsx`, `api.ts`, `components/*`).

Verified as already correct (left alone):

- FK constraints with `ON DELETE CASCADE`, HNSW cosine index, `vector(384) NOT NULL`.
- `scripts/init_db.py` is idempotent — repeated runs do not destroy data.
- Ingestion transactional behaviour: parse/embed failures persist a `failed`
  status with no partial chunk rows; a DB failure rolls back; `force=true`
  replaces the old chunk set; ready documents are not reprocessed.
- Readiness short-circuits to 503 when `DATABASE_URL` is unset; it performs no
  startup mutation of existing data.
- Request-ID middleware, stage timings (`retrieval`, `context_building`,
  `generation`, `total`), aggregate-only `/metrics/runtime`, privacy-safe logs.
- Upload size/type enforcement, `sanitize_filename`, ORM-only SQL.
- `.dockerignore` excludes `.env`; image runs as non-root `appuser`.
- Full Ollama failure taxonomy (connection / timeout / model-unavailable /
  malformed / empty / generic) mapped to stable HTTP codes — 19 checks in
  `test_phase10_ollama_answer.py`, 51 in `test_phase11_agent.py`.
- Agent boundaries: `MAX_AGENT_ITERATIONS`/`MAX_TOOL_CALLS` = 3, server-owned
  `document_id`, document-scoped retrieval, no chain-of-thought exposure.
- Re-ingestion after the Phase 15 chunking change is documented in
  `README.md` and `ARCHITECTURE.md§15.5`.

Contract note: the spec's endpoint list says `POST /documents`; the actual
route is `POST /documents/upload`. Routes were **not** changed — that would be
a contract change, not hardening.

---

## 4. Findings

### 4.1 Confirmed defects (fixed)

| # | Severity | Finding | Evidence |
|---|---|---|---|
| F1 | High | No global handler for exceptions no route catches. Starlette answered a raised `RuntimeError` with `500 text/plain "Internal Server Error"` — no JSON `{"detail": ...}` contract, and **no `X-Request-ID`**, because the exception propagates *through* the correlation middleware so its post-`finally` header assignment never runs. | probe against a temp route: `content-type: text/plain`, no header |
| F2 | High | The request ID a late handler *would* log is wrong. `clear_request_id()` runs in the correlation middleware's `finally`, and `get_request_id()` **mints a brand-new UUID when the contextvar is unset** — so any handler running after the middleware logs an ID that appears on no response. | instrumented run: handler logged a UUID that was not the response's |
| F3 | High | Upload failures were invisible in the UI. `App.tsx` rendered `ErrorBanner` only when `fetchError && uploadState !== 'error'` — exactly the case when an upload fails — and `UploadPanel` renders only a Dismiss button, never the message. | new test fails on the original code |
| F4 | High | An in-flight `/ask` response could be appended after the user switched documents, showing document A's answer under document B. | new test fails on the original code |
| F5 | Medium | Both ingestion routes had `except Exception: raise HTTPException(500, generic_message)` with **no server-side log**. Uvicorn logs access lines for `HTTPException`, not tracebacks, so an unexpected embedding/persistence failure left zero evidence. | code inspection (`routes/documents.py:97`, `:152`) |
| F6 | Medium | Two `OllamaResponseError` messages interpolated the whole Ollama response object, which can contain generated text and (for `qwen3:4b`) `message.reasoning`. That content would reach a client-facing 502 body. | `ollama_client.py:147`, `:230` |
| F7 | Medium | Bare `pytest` from `backend/` had no `testpaths`, so it also matched `backend/scripts/test_*.py`. Those are driver scripts whose `record()` helper returns `None`, so pytest "passes" them regardless of what they measured; `scripts/test_ingestion.py::test_01_ordering_detail(stored)` also needs a fixture that does not exist (collection error). | `pytest.ini` absent; 231 items collected from `scripts/` |
| F8 | Medium | `pytest` is required to run the suite but was not declared anywhere, and `backend/.venv` does not have it — the suite only ran because the system interpreter happened to have it. | `.venv\Scripts\python -m pytest` → `No module named pytest` |
| F9 | Low | `.dockerignore` copied `tests/`, `pytest.ini` and `scripts/test_*.py` into the runtime image, and its bare `__pycache__`/`*.pyc` patterns do not match nested paths, so stale bytecode shipped too. | `docker compose exec backend ls -a /app` |
| F10 | Low | `.env.example` omitted `OLLAMA_TIMEOUT_SECONDS`, `OLLAMA_TEMPERATURE`, `OLLAMA_NUM_PREDICT`, `AGENT_MAX_ITERATIONS`, `AGENT_MAX_TOOL_CALLS`; the README table omitted nine more. §7.2/§16 require these to be visible and non-contradictory. | diff of `.env.example` vs `Settings` |
| F11 | Low | `Settings` validated only the Phase 5 chunking relationships. `CHUNK_SIZE=0`, `MAX_UPLOAD_SIZE_MB=0`, `RETRIEVAL_TOP_K_MAX < DEFAULT`, `AGENT_MAX_* > 3`, `LOG_LEVEL=VERBOSE`, `ALLOWED_ORIGINS=localhost:5173` all loaded silently. | constructed `Settings(...)` before the change |

### 4.2 Verified, not defects

- **`/search` has no similarity floor.** It returns the top-k document-scoped
  chunks, so a query whose terms are absent can still return weakly related
  chunks *of the same document*. The floor belongs to `/ask` context building
  (`RAG_MIN_SIMILARITY`), which is where answers are produced. Observed during
  E2E and recorded as behaviour (ARCHITECTURE.md§16.6), not changed.
- **`AnswerSource` has no `document_id` field.** Binding to a document is done
  by the response envelope (`AnswerResponse.document_id`), which is
  backend-authoritative. Changing the schema would be a contract change.
- **`document_service.finalize_document` is dead code.** Unused since Phase 7;
  left alone (renaming/removing is not hardening).
- **Two `empty.pdf` documents were deleted from the shared database** during
  the API probe cleanup (they were leftovers from an earlier phase's probe,
  alongside the three documents the probe itself created). Nothing else was
  removed.

---

## 5. Changes made

| File | Change |
|---|---|
| `backend/pytest.ini` | **new** — `testpaths = tests` so bare `pytest` collects the intended suite only |
| `backend/app/config.py` | `Settings` now validates upload limit, query length, embedding batch size, retrieval `top_k` default/max, context budget, similarity range, Ollama timeout/predict, agent caps (1–3), non-blank `ENVIRONMENT`, real `LOG_LEVEL`, and `http(s)` origins (empty list still allowed) |
| `backend/app/main.py` | `@app.exception_handler(Exception)` → `500 {"detail": "Internal server error."}` + `X-Request-ID`; logs `unhandled_exception` with `exc_info`, request ID, method, path, error type. Correlation middleware now publishes `request.state.request_id` so late handlers see the *same* ID |
| `backend/app/llm/ollama_client.py` | `_describe_payload()` reports payload/message **keys only**; both payload-interpolating error messages use it |
| `backend/app/routes/documents.py` | both generic ingestion `except Exception` blocks log `ingestion_failed` (`exc_info`, request ID, document ID, stage) before raising the same unchanged generic 500 |
| `backend/tests/test_phase16_units.py` | **new** — 30 tests: 22 invalid-settings cases, empty-origins case, Phase 15 value preservation, 500 JSON contract, 404 JSON contract, log↔header correlation, payload redaction |
| `backend/requirements-dev.txt` | **new** — declares `pytest` (test-only; not in the runtime image) |
| `backend/scripts/run_all_regressions.py` | **new** — the single command for the complete backend suite (pytest + all 10 drivers, non-zero exit on any failure) |
| `backend/.dockerignore` | excludes `tests`, `pytest.ini`, `scripts/test_*.py`, and nested `__pycache__`/`*.pyc` |
| `backend/.env.example` | adds the five missing documented settings |
| `frontend/src/App.tsx` | `ErrorBanner` renders whenever `fetchError` is set (upload errors were suppressed) |
| `frontend/src/components/ChatPanel.tsx` | monotonic request token; stale `/ask` responses and errors are dropped after a document switch or a newer submit; the dead `setAskState('asking')` immediately overwritten by `'idle'` was removed |
| `frontend/src/__tests__/App.test.tsx` | **+1 test** — "surfaces the upload failure message to the user" |
| `frontend/src/__tests__/ChatPanel.test.tsx` | **+1 test** — "does not deliver an in-flight answer to another document" |
| `README.md` | Phase 16 section; complete test-command section (single runner command); 9 rows added to the environment table |
| `DEPLOYMENT.md` | environment table aligned with `Settings` + validation note; container contents documented |
| `ARCHITECTURE.md` | new **§16** (scope, reliability decisions, test-infra decisions, deployment decisions, out-of-scope, limits) |
| `docs/phases/PHASE_16_PRODUCTION_HARDENING.md` | this report |

Both frontend fixes were proven as regressions first: with the fix reverted,
the two new tests fail (2 failed, 13 passed); with the fix restored, all 43
pass.

---

## 6. Changes explicitly rejected

| Rejected | Why |
|---|---|
| Redis / Celery / async ingestion / background workers | spec §28 non-goals; nothing measured requires them |
| Authentication, chat persistence, multi-document QA | spec §28 non-goals |
| Alembic | `init_db.py` is idempotent; no concrete migration requirement |
| Dependency upgrades | none demonstrated a defect; only a *declaration* of the already-used `pytest` was added, deliberately in `requirements-dev.txt` |
| LangChain / LangGraph / reranker / cloud fallback / new vector DB | spec §28 non-goals |
| CI/CD pipeline, external monitoring | spec §28 non-goals |
| Echoing a client-supplied `X-Request-ID` | the middleware deliberately generates a fresh ID per request; spec asks only for *deterministic* behaviour. Changing it would alter log semantics for no measured benefit |
| Rejecting an empty `ALLOWED_ORIGINS` | an empty allowlist is a valid, fully restrictive configuration and is asserted by `scripts/test_phase13_deployment.py`; validating the *format* of non-empty entries was kept |
| Adding a similarity floor to `/search` | would change retrieval behaviour and is not what the threshold is for (§4.2) |
| Renaming `test_01_ordering_detail` in `scripts/test_ingestion.py` | pytest is no longer pointed at `scripts/`; the driver path calls the helper correctly. Editing a Phase 7 regression script to silence a runner path we do not recommend would be churn |
| Retries around the cross-document isolation test | spec §12.2 forbids hiding real failures; the test passed on every run (4 tests inside the 76) |
| Making `UploadPanel` render the error message itself | would be a UI change; the existing `ErrorBanner` (with `role="alert"`) is reused instead |
| Moving `CHUNK_SIZE` / `CHUNK_OVERLAP` / `RAG_MIN_SIMILARITY` | Phase 15 baseline is frozen; not touched |

---

## 7. Tests

### 7.1 Results

| Suite | Before | After |
|---|---|---|
| `pytest` (backend) | 46 tests; bare `pytest` also collected `scripts/` (231 items, incl. 1 collection error, and `record()`-based checks that cannot fail) | **76 tests, 0 failures, 0 collection errors** — bare `pytest` collects `backend/tests` only |
| Phase 16 unit tests | — | **30** (settings validation ×23, 500/404 contracts, log↔header correlation, payload redaction) |
| Frontend | 41 tests / 8 files | **43 tests / 8 files**, 0 failed (2 new regression tests) |
| Phase regression drivers | 267 checks | **267 checks, 0 failed** (chunker 17, embeddings 12, ingestion 19, phase8 32, phase9 29, phase10 48, phase11 51, phase12 15, phase13 19, phase15 25) |
| Cross-document isolation | 4 tests passing | still passing (part of the 76) |
| API probe (error matrix) | 26 checks | **26/26** |
| Local E2E | — | **24/24** |

The one-command suite:

```bash
cd backend && python scripts/run_all_regressions.py
# REGRESSION SUMMARY
#   PASS  pytest (backend/tests)
#   PASS  scripts/test_chunker.py
#   ... (10 drivers)
# all 11 suites passed   (exit 0)
```

### 7.2 Before / after evidence (spec §26)

| Area | Before Phase 16 | After Phase 16 |
|---|---|---|
| Golden evaluation | 21/21 | 21/21 verified (native + Docker) |
| Retrieval hit-rate | 100% | 100% verified |
| Citation validity | 100% | 100% verified |
| Correct rejection | 100% | 100% verified |
| Pytest collection | no `pytest.ini`; default collection also matched `scripts/` | `pytest` → 76 tests, 0 failures, 0 collection errors |
| Isolation reliability | 4 tests passing | verified unchanged (in the 76) |
| Docker E2E | verified in Phase 15 | re-verified: 24/24 checks + frontend 200 from nginx |
| Runtime configuration | chunking relationships only | 23 invalid-value cases rejected at load; Phase 15 defaults unchanged |
| Security checks | upload size/type, sanitisation, isolation, secrets, error leakage — verified unchanged | + 500 JSON contract with correlation ID, + no upstream payload in error bodies, + upload errors visible, + image free of tests/`.env` |
| Dependency set | `requirements.txt` (11 entries) | unchanged; `requirements-dev.txt` declares the already-used `pytest` |

Nothing was weakened; no denominator changed.

---

## 8. Evaluation

`cd backend && python evaluate.py` against the golden set
(`eval/golden_sets/sample_insurance_policy.json`, 21 cases):

| Run | Context | Result |
|---|---|---|
| `eval/reports/20260930_183114_sample_insurance_policy.*` | native, session start (baseline) | 21/21 — 100% / 100% / 100% |
| `eval/reports/20260930_193617_sample_insurance_policy.*` | native, after all backend changes | 21/21 — 100% / 100% / 100% |
| `eval/reports/20260930_195208_sample_insurance_policy.*` | Docker backend (pre-final image) | 21/21 — 100% / 100% / 100% |
| `eval/reports/20260930_200056_sample_insurance_policy.*` | Docker backend (final image) | 21/21 — 100% / 100% / 100% |

The denominator stayed at 21 — the golden set was not modified.

---

## 9. Docker verification

Executed against the final image, in order:

```text
docker compose build        -> backend + frontend built, exit 0
docker compose up -d        -> both containers Up
GET  :8000/health           -> {"status":"ok"}
GET  :8000/health/ready     -> 200 {"status":"ok","checks":{"database":"ok","ollama":"ok"}}
GET  :5173/                 -> 200, server: nginx/1.31.6
full E2E (24 checks)        -> 24/24 passed against the container
python evaluate.py          -> 21/21, 100/100/100 against the container
docker compose down         -> containers and network removed
```

Image inspection:

| Check | Result |
|---|---|
| Non-root execution | `id -un` → `appuser`; `docker inspect .Config.User` → `appuser` |
| `.env` in image | absent (`/app/.env` does not exist; `.env.example` with placeholders is present) |
| Test artifacts | `tests/`, `pytest.ini`, `scripts/test_*.py` **absent** after the `.dockerignore` fix |
| Bytecode caches | none shipped (`**/__pycache__`, `**/*.pyc`) |
| Model cache | not baked in; the embedding model is fetched into the container's home cache at first use |
| Runtime dependencies | `requirements.txt` only — `requirements-dev.txt` is not installed |
| Frontend | multi-stage `node:22-alpine` → `nginx:alpine`, `VITE_API_BASE_URL` build arg, no dev server |

---

## 10. Security review

Protected (verified this session):

- **Upload size/type**: 26 MB → `413`; `.txt` → `415`; corrupt/empty PDF →
  `422` (document row recorded, chunks never written); oversized requests
  never reach the parser.
- **Path traversal**: filenames are sanitised (`document_service.sanitize_filename`);
  the original file is not persisted, so there is no upload directory to
  traverse.
- **SQL injection**: all access goes through SQLAlchemy ORM/query builders;
  the only raw SQL is `SELECT 1` in readiness and the pgvector query
  constructed with bound parameters.
- **Document isolation**: retrieval always filters on a server-supplied
  `document_id`; E2E asserts doc B never returns doc A's facts and that search
  results are scoped (4 isolation tests also pass).
- **Agent safety**: `document_id` is not model-controlled, tool calls cannot
  leave the application, iteration/tool caps are 1–3 and now enforced by
  validation, `document content` is treated as untrusted text in prompts.
- **Error leakage**: 500/404/422/413/415 bodies contain no traceback or
  `site-packages` reference (probe + E2E); unexpected failures are logged
  server-side only, with the correlation ID.
- **Log leakage**: logs carry document IDs, chunk counts, stage timings and
  durations — not document text, questions, answers, embeddings or secrets.
  Upstream Ollama payload content is no longer copied into error messages.
- **Secrets**: `backend/.env` is gitignored and absent from the image;
  `.env.example` contains placeholders only; no credentials in source; no API
  keys are required (Ollama is local).
- **CORS**: explicit allowlist read from `ALLOWED_ORIGINS` (default
  `http://localhost:5173`), `allow_credentials=False`, `*` only for
  methods/headers.
- **Docker permissions**: non-root user, read-only app tree ownership for
  `appuser`, no privileged flags in compose.

Out of scope and **not** claimed: authentication/authorization (anyone with a
document ID can query it), transport security in front of the app, rate
limiting, dependency CVE scanning, container runtime hardening, and
host/network isolation. The application should not be exposed to the public
internet with sensitive documents.

---

## 11. Performance observations

Measured from `GET /metrics/runtime` on the native backend during Phase 16
verification (49 requests, 2 "errors" — both the deliberate `422`/`404` probes
of the E2E, uptime 1692 s):

| Metric | Value |
|---|---|
| Average request latency | 1923.5 ms |
| Average generation time | 788.7 ms |
| Average retrieval time | 11.2 ms |
| Average context-building time | 0.02 ms |
| Total errors | 2 (intentional 4xx from the E2E) |

Consistent with Phase 15's observation that generation dominates `/ask` latency
and that retrieval/context construction are negligible. Upload/ingestion for
the 9-chunk E2E document completed well inside the 120 s frontend timeout.

No performance change was made: nothing measurable was broken, and the phase
forbids caching/queues for theoretical gains. The one behavioural addition on
the hot path is a single `logger.error` call **only when ingestion raises**, so
successful requests pay nothing.

---

## 12. Limitations

- **`AnswerSource` does not carry `document_id`.** Scoping is expressed by the
  response envelope. Callers that need per-source document identity must use
  `AnswerResponse.document_id`.
- **`/search` has no similarity floor** (§4.2) — it returns document-scoped
  nearest neighbours, not "relevant" chunks.
- **Configuration errors fail the process at import time.** That is the
  intent, but a typo in `.env` is only discovered on the next start.
- **No automated external monitoring**: `/metrics/runtime` is in-process and
  resets on restart.
- **The 500 handler logs a full traceback per failure** — server-side only,
  bounded by the failure rate.
- **`pytest` must be installed explicitly** (`requirements-dev.txt`); it is
  intentionally not a runtime dependency, so a bare runtime image cannot run
  the suite.
- **`scripts/test_ingestion.py::test_01_ordering_detail` cannot be collected
  by pytest** (needs a `stored` fixture). It is a helper invoked by the driver;
  running that file with pytest is not a supported path and is not part of the
  documented command.
- **Pre-existing line-ending-only diffs** in 10 files and untracked
  `docs/phases/PHASE_15_SPEC.md`, `docs/phases/PHASE_16_SPEC.md`,
  `opencode.json` were left untouched — they are not Phase 16 work.
- **Verification is single-machine**: Windows host, local Ollama, Supabase
  Postgres. No Linux-native or CI run was performed (CI is a non-goal).

---

## 13. Final status

**Complete — all Definition-of-Done items met.**

```text
Configuration validated and consistent          yes (23 invalid cases rejected; .env.example / README / DEPLOYMENT aligned)
API failure handling deterministic              yes (500/404 JSON + X-Request-ID; 26/26 error-matrix probe)
Ingestion lifecycle reliable                    yes (19/19 driver checks; unexpected failures now logged)
Database transactions safe                      yes (rollback/cascade/idempotent init verified unchanged)
Agent boundaries enforced                       yes (caps validated 1-3; 51/51 agent checks)
LLM failure modes handled                       yes (48/48; payload redaction added)
Observability privacy-safe                      yes (aggregate metrics; no content in logs or error bodies)
Frontend failure states usable                  yes (upload errors visible; no stale answers; 43 tests)
No unnecessary infrastructure                   yes (no new services, no dependency upgrades)

Pytest                                          76 passed / 0 failed / 0 collection errors
Phase 1-15 regression                           267 checks / 0 failed (11 suites via one command)
Golden evaluation                               21/21
Retrieval / Citation / Rejection                100% / 100% / 100%
Isolation                                       4/4
Local E2E                                       24/24 (+ frontend build)
Docker E2E                                      24/24 (+ nginx 200)
Docker evaluation                               21/21, 100/100/100
Docker build / startup / readiness              ok
Secrets / cache junk in image                   none
Documentation                                   README, DEPLOYMENT, ARCHITECTURE §16, this report
```

Nothing was committed or pushed: the working tree is left for human review of
the diff, per spec §32.

Known limitations: see §12. Unresolved issues: none identified in this
phase's scope.
