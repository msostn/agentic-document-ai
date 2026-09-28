# PHASE 14 — RAG Evaluation & Observability Layer

**Audience:** fresh OpenCode/MiMo implementation session
**Author role:** architecture/specification reviewer (no code written, no files modified)
**Status:** ready for implementation after mandatory repo inspection (Section 0)
**Do not commit anything until the phase is implemented, tested, and manually verified.**

---

## 0. MANDATORY PRE-IMPLEMENTATION INSPECTION

Before writing any code, the implementation agent MUST inspect the actual repository and confirm or correct every assumption in Section 3. Do not trust prior phase reports — they may be stale relative to the current repo.

Inspect at minimum:

- `ARCHITECTURE.md`, `README.md`, `DEPLOYMENT.md`
- `git status`, `git log --oneline -20`
- `backend/app/` (full tree, not just top level)
- `backend/scripts/`
- `frontend/src/`
- `docker-compose.yml`, `backend/Dockerfile`, `frontend/Dockerfile`
- `.env.example` / environment example files
- existing test suite (`backend/tests/` or equivalent) — count, framework, structure
- current API routes (router registration, e.g. `backend/app/main.py` or `backend/app/api/`)
- current database models (`backend/app/models/` or equivalent)
- current configuration (`backend/app/config.py` / Pydantic Settings module)
- current logging setup (is there one already? structured or plain `print`/`logging`?)
- current agent/tool-orchestration module (Phase 11) — exact file and function names
- current answer-generation module (Phase 10) — exact response schema, including how `sources` (chunk_id, chunk_index, page_number) are shaped
- whether any metrics/diagnostics/debug endpoint already exists
- whether `qwen3:4b` responses currently expose any reasoning/thinking tokens that flow into logs or responses

Where this spec says "assume" or "likely," treat it as **unverified** — confirm against the real repo before writing code, and if reality differs, adapt file paths/names accordingly without changing the intent of this phase.

---

## 1. Phase Objective

Give the system a deterministic, zero-cost way to (a) measure whether retrieval and generation are actually working correctly over time, and (b) observe what is happening at runtime (latency per stage, error rates), without adding chat persistence, authentication, external monitoring infrastructure, or non-deterministic LLM-graded evaluation.

## 2. Why This Phase Exists

Phases 1–13 built and shipped the full pipeline (parse → chunk → embed → retrieve → generate → agent → UI → Docker), and Phase 13 verified it works end-to-end once, manually, for one document. That is not the same as knowing:

- whether retrieval quality holds up across different documents and question types,
- whether the "no fabricated citations" guarantee (Phase 10/11) actually holds under more than the one manually-tested case,
- whether the "unrelated question → rejected" behavior generalizes,
- where time is spent on a real query (retrieval vs. context building vs. Ollama generation),
- whether a future change silently regresses retrieval or groundedness.

This is a well-known and commonly-missing piece in RAG portfolio projects, and it is also one of the most interview-relevant additions you can make ("how do you know your RAG system works, and how would you catch a regression?") without touching authentication, chat history, or multi-document scope — all of which you've explicitly flagged as out of bounds unless justified.

## 3. Current-State Assumptions (TO BE VERIFIED by implementation agent)

- Backend is FastAPI with routers mounted centrally; adding a new router is a known, low-risk pattern already used in Phases 4/7/8/9/10/11.
- Configuration is centralized via Pydantic Settings (Phase 1–3); new env vars should be added there, not scattered as `os.getenv` calls.
- The answer/query endpoint (Phase 10/11) returns a response containing an `answer` field and a `sources` array, where each source includes `chunk_id`, `chunk_index`, `page_number` (confirmed in Phase 13 E2E verification notes — but the exact field names/schema must be re-confirmed against the actual Pydantic response model).
- Document-scoped retrieval (Phase 8) enforces `document_id` filtering at the SQL/pgvector query level, not just in application logic — this must be re-confirmed, because it is the basis of the data-isolation regression test in this phase.
- 283 tests currently pass (per Phase 13 report) — treat this as the regression floor, not a hard number to preserve exactly (new tests will raise the count; no existing test should start failing).
- Logging today is likely minimal/print-based or basic `logging` module usage without structured (key=value or JSON) fields or request correlation IDs — to be confirmed.
- No evaluation harness, golden dataset, or metrics endpoint currently exists.
- `qwen3:4b` via Ollama may emit internal reasoning content depending on how the Ollama call is configured; Phase 10 already claims "no chain-of-thought exposure" — this phase must not weaken that guarantee anywhere (logs included).

## 4. Problems / Gaps Being Addressed

1. No repeatable way to detect a retrieval-quality or groundedness regression after a code change.
2. No visibility into per-stage latency (retrieval, context assembly, LLM generation) for a real query.
3. No deterministic guard confirming that "unanswerable" questions keep being correctly rejected as the system evolves.
4. No deterministic guard, beyond one manual test, confirming that a query against Document A can never surface content or citations from Document B.
5. No lightweight, zero-cost way to demonstrate system health/behavior at runtime (useful in an interview and useful operationally).

## 5. Scope

In scope for Phase 14:

- A deterministic **offline evaluation harness** (script + golden dataset format) that measures, per document:
  - retrieval hit-rate for answerable questions
  - citation validity (every returned `chunk_id` genuinely belongs to the queried document)
  - correct rejection behavior for unanswerable/out-of-scope questions
  - a weak, clearly-labeled heuristic answer-correctness signal (keyword presence) — not treated as a strong quality metric
- A **cross-document data-isolation regression test** (automated, part of the normal test suite, not just the eval script).
- **Per-request stage timing** for the query/answer path (retrieval, context building, generation) surfaced via structured logs.
- A minimal **in-memory runtime metrics endpoint** (`GET /metrics/runtime` or similar — exact path confirmed against existing route conventions) exposing aggregate counters: uptime, total requests, total errors, average latency per stage. In-memory only, resets on restart, explicitly documented as a lightweight dev/ops aid, not a production monitoring system.
- Structured logging with a request correlation ID threaded through the agent's tool-calling loop.
- Documentation updates (`ARCHITECTURE.md` / `README.md`) describing the new capability and its limitations.

Out of scope: see Section 6.

## 6. Explicit Non-Goals

- **No** LLM-as-judge or any non-deterministic evaluation scoring. All eval metrics must be deterministic (string/keyword/ID matching), consistent with "deterministic where practical."
- **No** authentication, API keys, or user accounts.
- **No** chat/conversation persistence or multi-turn memory. This phase does not change the system from single-shot Q&A.
- **No** multi-document cross-referencing or corpus-wide search. Retrieval remains document-scoped, exactly as today.
- **No** external observability stack (Prometheus, Grafana, ELK, OpenTelemetry collectors, etc.). Metrics stay in-process and in-memory.
- **No** new database tables for storing evaluation history (see Section 11 for the reasoning). Eval output is a generated report file, not a persisted DB record.
- **No** UI dashboard. The metrics endpoint returns JSON; no frontend visualization is required in this phase.
- **No** CI/CD pipeline wiring (e.g., gating merges on eval scores). That is a reasonable Phase 15+ idea, not Phase 14.
- **No** removal or breaking change to any existing endpoint, response schema, or CLI script.

## 7. Architecture Changes

Additive only. No existing component is replaced or restructured.

```
React + Vite + TS
        ↓
FastAPI  ──────────────► [NEW] /metrics/runtime (read-only, in-memory counters)
        ↓
Agent  ──────[instrumented with stage timers + correlation ID]
        ↓
RAG search tool
        ↓
pgvector semantic retrieval
        ↓
relevant document chunks
        ↓
local Ollama LLM
        ↓
grounded answer + sources

[NEW, offline/out-of-band]
backend/scripts/evaluate.py
        ↓ (HTTP calls against the running API, black-box)
golden dataset (JSON, per document)
        ↓
eval report (Markdown/JSON, written to disk, git-trackable)
```

The evaluation harness is a black-box HTTP client against the existing API — it does not reach into the database directly and does not require new internal coupling. This keeps it honest (it tests what a real user/client would see) and simple to explain in an interview.

## 8. Data-Flow Changes

- Query request path gains: a correlation/request ID (generated at request entry, e.g. via middleware or dependency), per-stage timing capture (retrieval start/end, context-build start/end, generation start/end), and a structured log line per stage plus one summary line per request.
- No change to the ingestion data flow is required for this phase. (Optional stretch, not required: the same timing utility could also wrap ingestion stages, but only if trivial given the existing code — do not force it.)
- Evaluation data flow is entirely separate and offline: golden dataset → HTTP requests to the already-running backend → scoring → report file. It never touches the frontend and never runs automatically as part of a normal request.

## 9. Backend Changes

1. **Timing/instrumentation utility** (new module, e.g. `backend/app/observability/timing.py` — exact location per existing package layout):
   - A context manager or decorator, e.g. `with stage_timer("retrieval", request_id=...):`, that records duration and emits a structured log line, and updates the in-memory metrics accumulator.
   - Must be lightweight (no external dependency required; Python's `time.perf_counter` and `logging` are sufficient).

2. **Request correlation ID**: generate a short ID (e.g. `uuid4().hex[:12]`) per incoming query request (middleware or route-level dependency), include it in every log line for that request, and optionally echo it back in the response (e.g. `X-Request-ID` header) for debuggability. Do not persist it anywhere.

3. **Instrument the existing query/answer route and agent loop** with `stage_timer` calls around: tool-driven retrieval call(s), context assembly, and the Ollama generation call. Do not change the response schema of the existing endpoint unless Section 12 explicitly calls for an additive field.

4. **In-memory metrics accumulator** (new module, e.g. `backend/app/observability/metrics.py`):
   - Thread/async-safe counters: `total_requests`, `total_errors`, per-stage running average latency (or simple count+sum, computing average on read), process start time (for uptime).
   - A single process-local singleton. Explicitly document the limitation that this does not aggregate across multiple worker processes/containers if the deployment ever scales beyond one backend instance — that is acceptable for this phase's zero-cost/local-first scope.

5. **New read-only endpoint**, e.g. `GET /metrics/runtime` (confirm naming doesn't collide with anything existing; align with the existing `/health` and `/health/ready` naming convention):
   - Returns JSON: `uptime_seconds`, `total_requests`, `total_errors`, `average_latency_ms` (overall), and a per-stage breakdown (`retrieval_ms`, `context_build_ms`, `generation_ms` — averages).
   - Must never include document content, chunk text, questions, or answers — aggregate numbers only.
   - Gate its exposure behind a configuration flag (see Section 13) so it can be disabled in a "production-like" environment without code changes.

6. **Golden dataset format and loader** (new module/location, e.g. `backend/eval/schema.py` or plain dataclasses/Pydantic models):
   - See Section 17 for the exact proposed JSON shape.
   - Loader validates the file and gives clear errors for malformed entries.

7. **Evaluation script** (`backend/scripts/evaluate.py`):
   - Takes a document ID (or document alias) and a golden dataset file path as input.
   - For each case, calls the existing query/answer API (black-box, over HTTP) and scores the response deterministically (see Section 17).
   - Handles Ollama/backend being unreachable gracefully: reports the failure clearly, does not crash uninformatively, and does not silently mark cases as passing.
   - Writes a report (Markdown and/or JSON) to a predictable output location (e.g. `eval/reports/<timestamp>_<document_alias>.md`), and also prints a concise summary to stdout.

8. **Logging must not expose chain-of-thought.** If the Ollama call configuration surfaces any "thinking"/reasoning content (common with `qwen3` family models), the instrumentation and logs must only capture stage timing, response length, and outcome — never the raw reasoning trace. This must be explicitly checked against how the existing Phase 10 generation call is implemented.

## 10. Frontend Changes

None required. This phase is backend/observability-focused by design, to keep scope coherent (see Section 6). If, after inspection, there is a trivial and clearly low-risk way to surface the request ID or a "debug timing" panel in the existing dev tooling, it may be added as a small optional addition — but it must not be treated as required scope, and must not block the phase's completion.

## 11. Database Changes

**None.** This is a deliberate architectural decision, not an oversight:

- Evaluation results are written as report files (Markdown/JSON) to disk, not persisted to Postgres. This avoids introducing a new table purely for observability/eval history, keeps the addition reversible and low-risk, and lets eval reports be tracked in git history if desired (a good interview talking point: "I track retrieval-quality regressions via versioned eval reports rather than a bespoke metrics database").
- Runtime metrics are in-memory only, explicitly not persisted, consistent with "no chat persistence unless there is a compelling architectural reason" — the same reasoning extends here: there is no compelling reason yet to persist operational metrics.
- If inspection reveals a strong existing pattern that makes a lightweight metrics table trivial and clearly beneficial, flag it as a discussion point rather than implementing it unilaterally — this spec's default is no DB changes.

## 12. API Changes

Additive only, no breaking changes to existing routes/schemas:

- **New:** `GET /metrics/runtime` — returns aggregate in-memory counters (see Section 9.5). No auth (consistent with current no-auth posture), but gated by a config flag to allow disabling exposure.
- **No changes** to the existing query/answer endpoint's response schema. If a request ID is echoed back, prefer an HTTP header (`X-Request-ID`) over modifying the JSON body, to avoid any risk of breaking existing frontend/type contracts (Phase 12's API client/types).
- **No changes** to upload, document list/get/delete, or retrieval endpoints.

## 13. Configuration / Environment Changes

Add to the existing Pydantic Settings module (exact file confirmed at inspection time):

- `ENABLE_METRICS_ENDPOINT` (bool, default `true` in local/dev, recommend documenting how to set `false` for a more locked-down deployment).
- `ENABLE_REQUEST_LOGGING` (bool, default `true`) — controls whether structured per-stage logs are emitted; useful to quiet logs in constrained environments.
- `EVAL_GOLDEN_SET_DIR` (path, default e.g. `eval/golden_sets/`).
- `EVAL_REPORT_DIR` (path, default e.g. `eval/reports/`).
- `LOG_LEVEL` — only add if not already present after inspection.

Update `.env.example` (or equivalent) with the new variables and short comments. Document all of them in `README.md`/`DEPLOYMENT.md`.

## 14. Security Considerations

- The `/metrics/runtime` endpoint must expose **only aggregate numeric counters** — never document content, chunk text, raw questions, raw answers, or per-document breakdowns that could leak what documents exist or what was asked. This preserves the "document content remains untrusted input" and general data-isolation posture.
- The evaluation harness runs as an offline script against the API — it must not become a new persistent network-exposed service or endpoint.
- Golden datasets must not embed real, sensitive uploaded-document content beyond what is already used for testing; use clearly synthetic or already-public sample documents.
- Logs must never contain full document text, full chunk text, or raw model chain-of-thought — only metadata (IDs, lengths, durations, outcomes). This must be explicitly checked, not assumed.
- The new **cross-document data-isolation test** (Section 17) directly targets the "secure against data-isolation issues" hard constraint and should be treated as a security regression test, not just a functional one.

## 15. Error Handling

- `evaluate.py` must handle: backend unreachable, Ollama unreachable/timeout (mid-run), malformed golden dataset entries, and a document ID that doesn't exist — each with a clear, distinct error message, and a non-zero exit code on hard failure. Partial results (cases that did complete) should still be reported rather than discarded.
- `/metrics/runtime` must return a valid response even if zero requests have been recorded yet (e.g., averages should not divide by zero — return `0` or `null` with a clear meaning, not an error).
- Timing instrumentation must not swallow or mask exceptions from the wrapped code — if a stage fails, the exception must still propagate normally after the timing/log side-effect completes.

## 16. Logging / Observability Considerations

- Use structured logging (key=value or JSON lines) rather than free-text prints, so log lines are greppable/parseable — this is itself a demonstrable improvement over ad hoc logging.
- Every query request should produce: one log line per stage (retrieval, context-build, generation) with `request_id`, `document_id`, `stage`, `duration_ms`, and one summary line at request completion with total duration and outcome (success/error/below-threshold/rejected).
- Do not log full questions or full answers at default log level if that risks logging sensitive document-derived content; log lengths/hashes if content-sensitivity is a concern, and document this choice.

## 17. Testing Strategy

**Golden dataset format** (proposed; adapt field names to match actual existing schemas after inspection):

```json
{
  "document_alias": "sample_insurance_policy",
  "document_id": "REPLACE_WITH_REAL_ID_AT_RUNTIME_OR_LOOKUP",
  "cases": [
    {
      "id": "case_001",
      "question": "What is the hospitalization deductible?",
      "should_be_answerable": true,
      "expected_page_range": [12, 14],
      "expected_answer_keywords": ["deductible"]
    },
    {
      "id": "case_002",
      "question": "What is the weather forecast for tomorrow?",
      "should_be_answerable": false
    }
  ]
}
```

**Deterministic scoring rules:**

- *Retrieval hit-rate:* for `should_be_answerable: true` cases, count a hit if any returned source's `page_number` falls in `expected_page_range` (when provided). Score = hits / answerable cases.
- *Citation validity:* every `chunk_id` in the response's `sources` must correspond to a chunk that actually belongs to the queried `document_id` (verified via an existing, already-available API — e.g. document/chunk lookup — not direct DB access from the eval script; confirm at inspection time which existing endpoint can support this, or flag if a small read-only lookup needs to be added).
- *Correct-rejection rate:* for `should_be_answerable: false` cases, verify the system's existing "below similarity threshold / no answer" behavior (Phase 9/10) triggers — i.e., it does **not** fabricate a confident answer with sources.
- *Answer-keyword heuristic (optional, weak signal):* case-insensitive substring match of `expected_answer_keywords` in the generated answer, explicitly reported as a non-authoritative signal only.

**New automated tests to add:**

- Unit tests for the timing utility (records duration correctly, does not swallow exceptions).
- Unit tests for the metrics accumulator (counters increment correctly, zero-state returns sane defaults, basic thread-safety if applicable).
- Unit tests for the eval-scoring functions (hit-rate, citation-validity, rejection-rate) using mocked/fixture API responses — these do not require a live Ollama or live DB.
- Integration test: call the real (test-client) query endpoint a few times, then call `/metrics/runtime` and assert counters reflect the calls.
- **Data-isolation regression test:** ingest (or use existing fixtures for) two distinct documents, ask a question scoped to Document A whose semantic content overlaps with Document B, and assert the response's sources only ever reference Document A's chunks. This must pass deterministically and is a security-relevant regression test.
- Negative/error-path tests: `evaluate.py` behavior when the API is unreachable; `/metrics/runtime` behavior at zero-traffic state.

**Regression requirements:**

- All previously-passing tests must continue to pass unchanged.
- No existing response schema changes; if the implementation agent finds this impossible without a schema change, it must stop and flag the conflict rather than silently altering a Phase 10–13 contract.

## 18. Regression Requirements (Summary)

- Existing 283 (or current actual count) tests: must still pass.
- No existing endpoint removed, renamed, or given a breaking schema change.
- Existing Docker Compose stack must still build and run exactly as before, with the new endpoint simply available as an addition.
- Existing frontend behavior must be completely unaffected (no frontend code changes are in scope).

## 19. Manual Verification

- Ingest at least one real sample PDF locally; author a small golden dataset (5–10 cases, mixing answerable and unanswerable questions) for it.
- Run `evaluate.py` against the running local stack; inspect the generated report for plausibility (hit-rate, citation validity, rejection rate all present and sensible).
- Issue a handful of manual queries via the existing UI or API, then call `/metrics/runtime` and confirm the counters and average latencies move as expected.
- Tail backend logs during a query and confirm structured, per-stage log lines appear with a consistent request ID and no leaked chain-of-thought, full document text, or full answer text (per the logging policy chosen).

## 20. Docker Verification

- `docker compose up` (or equivalent) and confirm `/metrics/runtime` is reachable through the same path/port as `/health` and `/health/ready`.
- Confirm `evaluate.py` can be pointed at the dockerized backend's URL and complete successfully (it does not need to run inside the container itself, but must work against it).
- Confirm no new required volumes/services were introduced (there should be none — this phase adds no new infrastructure).
- `docker compose down` cleanly, as in Phase 13.

## 21. Files Expected to Be Created/Modified (approximate — confirm against real structure)

- `backend/app/observability/timing.py` (new)
- `backend/app/observability/metrics.py` (new)
- `backend/app/api/routes/metrics.py` or equivalent (new route file, or addition to an existing router module)
- Modifications to the existing query/answer route and/or agent orchestration module (Phase 11) to add instrumentation and correlation IDs — file name to be confirmed at inspection
- `backend/app/config.py` (or equivalent Settings module) — add new env vars
- `backend/eval/schema.py` (or similar) — golden dataset model/loader
- `backend/scripts/evaluate.py` (new)
- `eval/golden_sets/<alias>.json` — at least one sample golden dataset
- `eval/reports/` — output directory (may be gitignored except for one committed sample report for demonstration)
- New test files, e.g. `backend/tests/test_metrics.py`, `backend/tests/test_eval_scoring.py`, `backend/tests/test_data_isolation.py` (if an isolation test doesn't already exist — confirm)
- `.env.example` — new variables documented
- `README.md` / `ARCHITECTURE.md` / `DEPLOYMENT.md` — describe the new capability, its limitations, and how to run the eval harness

## 22. Acceptance Criteria

- [ ] `GET /metrics/runtime` exists, returns valid JSON at zero-traffic and after traffic, and never exposes document/question/answer content.
- [ ] Structured, per-stage logs with a request correlation ID appear for every query request.
- [ ] Logs never contain full document text, full chunk text, full answers, or raw model chain-of-thought.
- [ ] `backend/scripts/evaluate.py` runs against a live local (and dockerized) backend, producing a report with retrieval hit-rate, citation validity, and correct-rejection metrics for a real sample document.
- [ ] A new automated data-isolation regression test exists and passes, proving cross-document leakage cannot occur.
- [ ] All previously-passing tests still pass; no existing endpoint schema changed.
- [ ] `.env.example` and documentation updated with all new configuration options.
- [ ] Docker Compose stack builds and runs with the new endpoint reachable; no new services/infrastructure introduced.
- [ ] No authentication, chat persistence, multi-document synthesis, or LLM-graded evaluation was introduced.

## 23. Risks and Edge Cases

- **In-memory metrics are process-local.** If the deployment ever runs multiple backend workers/containers, counters won't aggregate. Acceptable now; must be documented as a known limitation, not silently hidden.
- **Golden dataset quality determines eval usefulness.** A too-small or too-easy dataset will show misleadingly perfect scores. Recommend at least a handful of genuinely hard/ambiguous cases per document.
- **Keyword-based answer-correctness is weak** and can both false-positive (keyword present but answer wrong) and false-negative (correct answer phrased differently). Must be labeled clearly as a heuristic, not a quality guarantee.
- **Reasoning-model leakage risk:** `qwen3:4b` reasoning/thinking content must be confirmed to not leak into logs or the metrics/answer path — this needs explicit verification against the actual Ollama call configuration, not assumption.
- **Scope creep risk:** it will be tempting to add a dashboard, persist eval history, or wire this into CI. All three are explicitly out of scope for Phase 14 (see Section 6) and should be captured as future-phase candidates instead of implemented now.
- **Endpoint exposure risk:** even aggregate metrics can leak operational information (e.g., traffic volume) if the deployment is ever made public; the config flag to disable the endpoint exists specifically to mitigate this, and its default should be reconsidered if/when the project moves toward any public-facing deployment.

## 24. Future-Phase Boundary

Explicitly deferred, not part of Phase 14, and not to be designed in detail here:

- Multi-turn conversational memory (in-session, non-persisted) — a plausible Phase 15 candidate if a compelling UX case is made.
- Multi-document / cross-document question answering.
- Authentication and per-user document scoping.
- Persisted evaluation history / trend tracking across runs.
- CI-integrated automatic regression gating using the eval harness.
- Any external observability stack (Prometheus/Grafana/OpenTelemetry) — only worth revisiting if the project ever needs multi-instance/production-grade monitoring.

---

# FINAL RECOMMENDATION SUMMARY

### A. Final Phase 14 Recommendation
**RAG Evaluation & Observability Layer** — a deterministic offline evaluation harness plus lightweight in-process runtime observability for the existing query/answer path.

### B. Why It Is the Next Logical Milestone
Phase 13 proved the system works once, manually, end-to-end. Phase 14 gives you a repeatable, deterministic way to know whether it keeps working as the system evolves — the single highest-leverage gap left, and one that requires no new infrastructure, no compromise on the "no auth / no chat persistence unless compelling reason" constraints, and no conflict with any of the preserved architectural principles (RAG grounding, bounded agent, document-scoped retrieval, backend-controlled identity, no chain-of-thought exposure).

### C. Exact Scope
- Deterministic golden-dataset evaluation harness (`evaluate.py`) measuring retrieval hit-rate, citation validity, and correct-rejection behavior, black-box over HTTP.
- Structured per-stage logging with request correlation IDs on the query/answer path.
- In-memory `/metrics/runtime` endpoint (aggregate counters only), config-gated.
- A new automated cross-document data-isolation regression test.
- Documentation and `.env.example` updates.

### D. Explicit Non-Goals
No auth. No chat persistence/multi-turn memory. No multi-document synthesis. No LLM-as-judge evaluation. No new DB tables. No external monitoring stack. No frontend changes. No CI/CD wiring.

### E. Implementation Order
1. Inspect the real repository (Section 0); correct any assumptions.
2. Add config flags and settings entries.
3. Build the timing utility and in-memory metrics accumulator; write their unit tests.
4. Instrument the query/answer route and agent loop with stage timers and a request correlation ID; verify no chain-of-thought or content leaks into logs.
5. Add the `/metrics/runtime` endpoint; write its tests.
6. Define the golden dataset schema/loader and author one real sample dataset against an ingested test document.
7. Build `evaluate.py` and its deterministic scoring functions; write unit tests for the scoring logic with mocked responses.
8. Add the cross-document data-isolation regression test.
9. Run the full existing test suite to confirm zero regressions; add the new tests.
10. Manual verification (Section 19), then Docker verification (Section 20).
11. Update documentation and `.env.example`.
12. Report results back for review — do not commit/push without review, consistent with your stated workflow.

### F. Acceptance Checklist
See Section 22 in full; summarized: metrics endpoint safe and functional, structured logging with no content/chain-of-thought leakage, working eval harness with a real report, new isolation regression test passing, zero breakage to existing tests/endpoints, documentation updated, Docker still works.

### G. Risks / Unresolved Decisions
- Exact route path/naming for the metrics endpoint (align with existing `/health` conventions — confirm at inspection).
- Whether an existing endpoint can already support citation-validity checks without any new read-only lookup, or whether one small addition is needed — confirm at inspection.
- Default posture for `ENABLE_METRICS_ENDPOINT` if/when the project is ever exposed beyond local/dev use.
- How rigorously to verify absence of chain-of-thought leakage from `qwen3:4b` given the exact current Ollama call configuration — flagged as a required verification step, not an assumption.
