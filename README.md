# Agentic Document Intelligence

A general-purpose Agentic RAG application. Users upload PDFs, retrieve relevant sections with vector search, and ask an LLM questions grounded in that document only.

It is *agentic* rather than a plain RAG call because answering is a bounded
tool-calling loop: retrieval always happens first (mandatory first search), the
model may then call `search_document` up to 3 times (3 iterations max), and the
backend — not the model — decides which sources are returned and whether there
was enough evidence to answer at all.

**Stack:** React + Vite + TypeScript · FastAPI + SQLAlchemy + PostgreSQL/pgvector ·
`sentence-transformers all-MiniLM-L6-v2` (CPU embeddings) · Ollama `qwen3:4b`
(local LLM) · pytest + Vitest · Docker Compose · GitHub Actions CI.

## Architecture

```
React/Vite (browser)
  ↓ HTTP/JSON
FastAPI (backend)
  ↓
Phase 11 bounded agent (tool-calling loop, max 3 iterations)
  ↓
Phase 9 RAG context (similarity filtering + character budgeting)
  ↓
Phase 8 semantic retrieval (pgvector cosine search)
  ↓
Ollama qwen3:4b (local LLM)
  ↓
Grounded answer + backend-verified sources
```

Phase 12 adds the React frontend and browser integration. The core RAG/agent logic (Phases 4–11) is unchanged.

## Prerequisites

- Python 3.12+ for native development (pinned requirements need numpy ≥ 2.5 /
  scipy ≥ 1.18; verified on 3.13, which is also what CI and the Docker image use)
- Node.js 22+ for native development (the frontend toolchain requires
  `^20.19 || ^22.12 || >=24`; CI uses 22)
- [Ollama](https://ollama.com/) installed and running
- Supabase PostgreSQL (free tier) or local PostgreSQL with pgvector
- Docker Desktop (for Docker deployment)

## Quick start (Docker Compose)

```bash
# 1. Clone the repo
git clone <repository-url>
cd agentic-document-ai

# 2. Configure backend
cd backend
cp .env.example .env    # edit with your DATABASE_URL and set OLLAMA_BASE_URL=http://host.docker.internal:11434
cd ..

# 3. Ensure Ollama is running with the required model
ollama pull qwen3:4b
ollama serve

# 4. Start with Docker Compose
docker compose up --build
```

Frontend: http://localhost:5173
Backend: http://localhost:8000

For full deployment details, see [DEPLOYMENT.md](DEPLOYMENT.md).

## Native development setup

### 1. Clone and configure backend

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # then edit with your DATABASE_URL
```

Edit `.env` with your Supabase PostgreSQL connection URL.

### 2. Configure frontend

```bash
cd frontend
npm install
cp .env.example .env          # defaults are fine for local dev
```

### 3. Start Ollama

```bash
ollama pull qwen3:4b
ollama serve                  # if not already running
```

### 4. Initialize database

```bash
cd backend
python scripts/init_db.py
```

### 5. Start the backend

```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 6. Start the frontend

```bash
cd frontend
npm run dev
```

Open http://localhost:5173.

## Demo Workflow

1. Open the frontend at http://localhost:5173
2. Check backend health (automatic on page load)
3. Upload a PDF file
4. Wait for ingestion to complete (status changes to "Ready")
5. Select the document from the sidebar
6. Ask a question about the document
7. View the grounded answer with source page references

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness check |
| GET | `/health/ready` | Readiness check (DB + Ollama) |
| POST | `/documents/upload` | Upload and ingest a PDF |
| GET | `/documents` | List all documents |
| GET | `/documents/{id}` | Get document metadata |
| POST | `/documents/{id}/ingest` | Re-ingest a document |
| POST | `/documents/{id}/search` | Semantic vector search |
| POST | `/documents/{id}/ask` | Ask a question (agent loop) |
| DELETE | `/documents/{id}` | Delete a document |
| GET | `/metrics/runtime` | Aggregate runtime metrics (in-memory, dev/ops aid) |

## Observability & Evaluation (Phase 14)

**Request correlation ID** — every response carries an `X-Request-ID` header.
The same ID appears in that request's log lines, so a response can be traced
through retrieval, context building, and generation.

**Structured logs** — each `/ask` request logs one line per stage plus one
summary line at completion, as `key=value` pairs after the standard prefix:

```
2026-09-29 00:37:32 INFO app.routes.documents: query_event document_id=... request_id=... stage=retrieval duration_ms=... outcome=success
2026-09-29 00:37:32 INFO app.main: query_event request_id=... stage=total duration_ms=... outcome=success
```

Only safe metadata is logged — never the question, the answer, document or
chunk text, or model reasoning. Set `ENABLE_REQUEST_LOGGING=false` to silence
the informational lines (warnings/errors still log).

**Runtime metrics** — `GET /metrics/runtime` returns uptime, request/error
counters, and per-stage totals/averages as JSON. Counters are process-local
and reset on restart; there is no persistent monitoring. Disable the endpoint
with `ENABLE_METRICS_ENDPOINT=false` (returns HTTP 503).

**Offline evaluation** — `backend/evaluate.py` is a black-box HTTP client that
scores a golden dataset against the running backend:

```bash
cd backend
python evaluate.py                      # uses EVAL_GOLDEN_SET_DIR
python evaluate.py --base-url http://localhost:8000
```

It reports retrieval hit-rate, citation validity (every returned `chunk_id`
belongs to the queried document), correct-rejection rate, and an explicitly
weak keyword heuristic. Markdown and JSON reports are written to
`eval/reports/`. A sample dataset for the synthetic insurance-policy PDF is at
`eval/golden_sets/sample_insurance_policy.json` (create the document with
`python scripts/create_phase14_sample_document.py`).

**Cross-document isolation test** —
`backend/tests/test_cross_document_isolation.py` proves a question scoped to
one document can never return another document's chunks.

## Retrieval Quality (Phase 15)

Phase 14's evaluation exposed one failing case: the pre-existing-condition
waiting-period question was rejected by the similarity threshold. The cause
was chunk-level embedding dilution — at `CHUNK_SIZE=800` the sample policy
became two broad chunks whose vectors scored 0.23/0.27 against the question,
while the Section 3 text alone scores 0.65.

Phase 15 therefore reduced `CHUNK_SIZE` 800 → 500 and `CHUNK_OVERLAP`
150 → 100 (smaller, more topical chunks) and **left `RAG_MIN_SIMILARITY`
at 0.30**, so recall improved without weakening the global rejection bar.
Measurements, alternatives rejected, and the before/after tables are in
`ARCHITECTURE.md§15` and `docs/phases/PHASE_15_RETRIEVAL_QUALITY.md`.

Re-ingest existing documents after changing chunking — chunks are only
regenerated at ingestion time:

```bash
curl -X POST "http://localhost:8000/documents/<id>/ingest?force=true" -F "file=@policy.pdf"
```

Golden-set evaluation (Phase 15 expanded it from 8 to 21 cases):

```bash
cd backend
python evaluate.py       # writes eval/reports/<timestamp>_sample_insurance_policy.{md,json}
```

## Production Hardening (Phase 16)

Phase 16 changed no architecture, no retrieval quality and no API contract. It
hardened the edges around them:

- **Settings are validated at startup.** Chunking, upload size, retrieval
  `top_k`, the agent iteration/tool caps (1–3), `LOG_LEVEL` and each
  `ALLOWED_ORIGINS` entry now fail loudly at boot instead of at request time.
- **Unhandled exceptions keep the JSON error contract.** A failure no route
  handles returns `500 {"detail": "Internal server error."}` with an
  `X-Request-ID`; the stack trace goes to the server log under the same
  request ID and is never sent to the client. Unexpected ingestion failures are
  logged the same way instead of only surfacing as an anonymous 500.
- **Frontend failure states are usable.** An upload error is actually shown,
  and an `/ask` response that resolves after the user switched documents is
  discarded rather than rendered under the wrong document.
- **One command runs the backend suite:** `python scripts/run_all_regressions.py`.

Measurements, rejected alternatives and the before/after table are in
`ARCHITECTURE.md§16` and `docs/phases/PHASE_16_PRODUCTION_HARDENING.md`.

## Testing

### Complete backend suite

One command runs everything — the unit/integration tests plus every Phase 1–15
regression script, and exits non-zero if any of it fails:

```bash
cd backend
pip install -r requirements-dev.txt    # once; pytest is not a runtime dependency
python scripts/run_all_regressions.py
```

Each regression driver also prints its own summary, so the parts can be run
individually:

```bash
cd backend

# Unit + integration tests (pytest.ini restricts bare `pytest` to the test suite,
# from backend/ or from the repository root)
python -m pytest

# Phase 1–15 regression drivers (each prints "N checks, N passed, 0 failed")
python scripts/test_chunker.py
python scripts/test_ingestion.py
python scripts/test_phase8_retrieval.py
python scripts/test_phase15_retrieval_quality.py
```

Expected: `python -m pytest` reports **0 failures and 0 collection errors**, and
every regression driver ends with **0 failed**.

### Evaluation

```bash
cd backend
python evaluate.py
```

### Frontend tests

```bash
cd frontend
npm test
```

### Build

```bash
cd frontend
npm run build
```

## Release verification (Phase 17)

```bash
python scripts/verify_release.py              # full release gate
python scripts/verify_release.py --offline    # deterministic stages only (what CI runs)
```

Each stage is a separate process; the script stops at the first failure, names
it in the summary, and exits non-zero — nothing is retried or swallowed. The
full gate runs: syntax → imports → pytest → chunking driver → deployment
driver → `scripts/run_all_regressions.py` → frontend lint/test/build →
`backend/scripts/api_contract_probe.py` (27 contract and security checks) →
`backend/scripts/e2e_smoke.py` (24 end-to-end checks) → `backend/evaluate.py`
plus a gate on its JSON report (21/21 cases, 100% retrieval hit-rate, 100%
citation validity, 100% correct rejection). The Docker procedure
(build → up → the same probes against the container → image-content checks →
down) is in [DEPLOYMENT.md](DEPLOYMENT.md#release-verification-phase-17).

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `master` and every pull
request:

- **Backend** (Python 3.13): pinned `requirements.txt` install (CPU torch
  wheel first), `python -m compileall`, import validation,
  `pytest -m "not integration" --strict-markers`, `scripts/test_chunker.py`,
  `scripts/test_phase13_deployment.py`
- **Frontend** (Node 22): `npm ci`, `npm run lint`, `npm test`, `npm run build`

There is no `continue-on-error`, no `|| true` and no retry loop: a red gate
fails the job. The live-service gates (regression drivers, E2E probe,
evaluation) need the database and Ollama, so they run through
`scripts/verify_release.py` rather than in CI.

## Final results (Phase 17)

| Gate | Result |
|------|--------|
| Backend pytest | 76 passed, 0 failed, 0 collection errors |
| Regression suite (Phase 1–16) | 11 suites, 267 checks, 0 failed |
| Frontend lint / tests / build | 0 errors · 43 passed · pass |
| Golden evaluation | 21/21 · retrieval 100% · citation validity 100% · correct rejection 100% |
| Cross-document isolation | pass (pytest integration test + E2E isolation checks) |
| API contract & security probe | 27/27 (native **and** container) |
| End-to-end smoke probe | 24/24 (native **and** container) |
| Docker | build → up → `/health` + `/health/ready` 200 → frontend 200 → image checks (non-root, no `.env`/tests/scripts) → down |
| CI workflow | deterministic stages reproduced locally in a fresh Python 3.13 venv |
| Secret scan / `git diff --check` | no secrets in tracked files · clean |

Full evidence: `docs/phases/PHASE_17_RELEASE_READINESS.md`.

## Known Limitations

- **No authentication**: Anyone with a document ID can query it. Do not expose to the public internet with sensitive documents.
- **No PDF persistence**: The original uploaded PDF is not stored. If ingestion fails, the file must be re-uploaded.
- **Ingestion is synchronous**: Large PDFs may take time. The frontend uses a generous timeout (120s default).
- **Local-first only**: Ollama must be running locally. The backend connects to `localhost:11434`.
- **No streaming**: Answers appear after the full agent loop completes.
- **Chat history is in-memory**: Lost on page refresh or document switch.
- **Metrics are ephemeral**: `/metrics/runtime` counters live in one process
  and reset on restart; they are not a substitute for real monitoring.
- **Generation token budget**: `OLLAMA_NUM_PREDICT` defaults to 2048. It must
  stay high enough for reasoning-heavy models (such as `qwen3:4b`) to finish
  thinking *and* produce answer text — at 512 the budget was exhausted during
  reasoning (`done_reason=length`, empty `content`) and `/ask` fell back to
  "I couldn't find enough information...".

## Environment Variables

### Backend (`backend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — | PostgreSQL connection URL (required) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `qwen3:4b` | Ollama model name |
| `OLLAMA_TIMEOUT_SECONDS` | `120` | HTTP timeout for an Ollama call |
| `OLLAMA_TEMPERATURE` | `0.1` | Sampling temperature for generation |
| `OLLAMA_NUM_PREDICT` | `2048` | Max tokens the model may generate per response |
| `AGENT_MAX_ITERATIONS` | `3` | Hard cap on agent-loop iterations (validated 1–3) |
| `AGENT_MAX_TOOL_CALLS` | `3` | Hard cap on tool calls per `/ask` (validated 1–3) |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | sentence-transformers model |
| `EMBEDDING_BATCH_SIZE` | `32` | Texts embedded per batch |
| `EMBEDDING_DEVICE` | `cpu` | Device used for embedding |
| `MAX_UPLOAD_SIZE_MB` | `25` | Upload size limit; larger files get `413` |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS allowed origins (comma-separated; empty disables cross-origin access) |
| `ENVIRONMENT` | `development` | Environment name |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `CHUNK_SIZE` | `500` | Max characters per chunk (Phase 15: reduced from 800) |
| `CHUNK_OVERLAP` | `100` | Word-based overlap carried into the next chunk (Phase 15: reduced from 150) |
| `MIN_CHUNK_SIZE` | `100` | Chunks shorter than this are merged into their predecessor |
| `MAX_QUERY_LENGTH` | `8000` | Character cap on `/ask` and `/search` queries |
| `RETRIEVAL_TOP_K_DEFAULT` / `_MAX` | `5` / `20` | Chunks returned by `/search` (validated: max ≥ default) |
| `RAG_MIN_SIMILARITY` | `0.30` | Cosine floor; below it `/ask` returns `below_similarity_threshold` with no sources |
| `RAG_CONTEXT_MAX_CHARS` | `8000` | Character budget for the context handed to the model |
| `ENABLE_METRICS_ENDPOINT` | `true` | Serve `GET /metrics/runtime` |
| `ENABLE_REQUEST_LOGGING` | `true` | Emit per-stage/request `query_event` log lines |
| `EVAL_GOLDEN_SET_DIR` | `<repo>/eval/golden_sets` | Golden datasets read by `evaluate.py` (absolute default; override only to point elsewhere) |
| `EVAL_REPORT_DIR` | `<repo>/eval/reports` | Reports written by `evaluate.py` (absolute default) |

### Frontend (`frontend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend API URL |
| `VITE_UPLOAD_TIMEOUT_MS` | `120000` | Upload timeout in milliseconds |
