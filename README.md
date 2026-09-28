# Agentic Document Intelligence

A general-purpose Agentic RAG application. Users upload PDFs, retrieve relevant sections with vector search, and ask an LLM questions grounded in that document only.

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

- Python 3.11+ (for native development)
- Node.js 18+ (for native development)
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

## Testing

### Backend tests

```bash
cd backend

# Per-phase regression scripts (each prints its own pass/fail summary)
python scripts/test_phase12_frontend_integration.py

# Phase 14 test suite (unit tests + cross-document isolation)
python -m pytest tests
```

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
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS allowed origins (comma-separated) |
| `ENVIRONMENT` | `development` | Environment name |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ENABLE_METRICS_ENDPOINT` | `true` | Serve `GET /metrics/runtime` |
| `ENABLE_REQUEST_LOGGING` | `true` | Emit per-stage/request `query_event` log lines |
| `EVAL_GOLDEN_SET_DIR` | `../eval/golden_sets` | Golden datasets read by `evaluate.py` |
| `EVAL_REPORT_DIR` | `../eval/reports` | Reports written by `evaluate.py` |

### Frontend (`frontend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend API URL |
| `VITE_UPLOAD_TIMEOUT_MS` | `120000` | Upload timeout in milliseconds |
