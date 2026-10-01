# Deployment Guide

This guide explains how to reproduce and run the Agentic Document Intelligence system on a local machine.

## Important: This is NOT a public SaaS deployment

This is a **reproducible local/demo deployment**. It is not intended to be exposed to the public internet because there is:

- **No authentication** — anyone with a document ID can query it
- **No user ownership** — no multi-tenant isolation
- **No rate limiting** — no protection against abuse
- **No persistent chat history** — in-memory only
- **No PDF persistence** — original files are discarded after ingestion

Do not expose this system to the internet with sensitive documents.

---

## Architecture

```
Browser
  ↓
Frontend container (nginx, static build)
  ↓
FastAPI backend container
  ↓
Agent/RAG pipeline (embedded in backend)
  ↓
Supabase PostgreSQL + pgvector (remote)
  ↓
Ollama running natively on host
```

Docker Compose manages:
- **frontend** — static production build served by nginx
- **backend** — FastAPI application

Docker Compose does **NOT** manage:
- **Supabase PostgreSQL** — remote hosted service (free tier)
- **Ollama** — must run natively on the host machine

---

## Prerequisites

### Required

1. **Docker Desktop** (with Docker Compose v2+)
   - Windows: [Docker Desktop for Windows](https://docs.docker.com/desktop/install/windows-install/)
   - Requires WSL 2 backend on Windows

2. **Ollama** (running natively on host)
   - Download: [https://ollama.com/](https://ollama.com/)
   - Must be running before starting Docker Compose

3. **Supabase account** (free tier)
   - Create a project at [https://supabase.com/](https://supabase.com/)
   - You need the PostgreSQL connection URL from Settings → Database

### Hardware expectations

- Minimum 8 GB RAM (embedding model + LLM)
- The `qwen3:4b` model requires ~2.5 GB after download
- The embedding model (`all-MiniLM-L6-v2`) requires ~80 MB download on first run
- SSD recommended for model loading performance

---

## Setup

### 1. Clone the repository

```bash
git clone <repository-url>
cd agentic-document-ai
```

### 2. Configure environment variables

**Backend:**

```bash
cd backend
cp .env.example .env
```

Edit `backend/.env` with your Supabase connection URL:

```
DATABASE_URL=postgresql+psycopg://postgres:[YOUR-PASSWORD]@[YOUR-SUPABASE-HOST]:5432/postgres?sslmode=require
```

For Docker deployment, also set:

```
OLLAMA_BASE_URL=http://host.docker.internal:11434
ALLOWED_ORIGINS=http://localhost:5173
LOG_LEVEL=INFO
```

**Frontend (optional):**

The default `VITE_API_BASE_URL` is `http://localhost:8000`, which is correct for the Docker setup. No changes needed unless you want a custom port.

### 3. Start Ollama

```bash
ollama pull qwen3:4b
ollama serve          # if not already running as a service
```

Verify Ollama is running:

```bash
curl http://localhost:11434/api/tags
```

### 4. Start with Docker Compose

```bash
# From the project root:
docker compose up --build
```

This will:
1. Build the backend Docker image (Python + dependencies)
2. Build the frontend Docker image (Node.js build + nginx)
3. Start both containers

### 5. Verify the deployment

```bash
# Backend health
curl http://localhost:8000/health

# Backend readiness
curl http://localhost:8000/health/ready

# Frontend
# Open http://localhost:5173 in your browser
```

---

## Native development (fallback)

If you prefer to run without Docker:

### Backend

Python **3.12+** (the pinned requirements need numpy ≥ 2.5 / scipy ≥ 1.18;
the project is verified on 3.13, which is also what CI and the Docker image
use).

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt   # once, for the test suite
cp .env.example .env          # edit with your DATABASE_URL

# Ensure OLLAMA_BASE_URL=http://localhost:11434 in .env
python scripts/init_db.py
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

Node.js **22+** (the toolchain requires `^20.19 || ^22.12 || >=24`; CI uses 22).

```bash
cd frontend
npm install
cp .env.example .env
npm run dev
```

Open http://localhost:5173.

---

## Health checks

| Endpoint | Purpose | Success | Failure |
|----------|---------|---------|---------|
| `GET /health` | Liveness | 200 `{"status": "ok"}` | — |
| `GET /health/ready` | Readiness | 200 `{"status": "ok"}` | 503 `{"status": "degraded"}` |

The readiness endpoint checks:
- **database** — can reach Supabase PostgreSQL
- **ollama** — can reach Ollama API

---

## Environment variables

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
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `EMBEDDING_BATCH_SIZE` | `32` | Texts embedded per batch |
| `EMBEDDING_DEVICE` | `cpu` | Device used for embedding |
| `MAX_UPLOAD_SIZE_MB` | `25` | Maximum upload size in MB |
| `CHUNK_SIZE` | `500` | Max characters per chunk (Phase 15 baseline) |
| `CHUNK_OVERLAP` | `100` | Word-based overlap carried into the next chunk |
| `MIN_CHUNK_SIZE` | `100` | Chunks shorter than this are merged into their predecessor |
| `MAX_QUERY_LENGTH` | `8000` | Character cap on `/ask` and `/search` queries |
| `RETRIEVAL_TOP_K_DEFAULT` / `_MAX` | `5` / `20` | Chunks returned by `/search` |
| `RAG_MIN_SIMILARITY` | `0.30` | Cosine floor for `/ask` context |
| `RAG_CONTEXT_MAX_CHARS` | `8000` | Character budget for the context handed to the model |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS allowed origins (comma-separated) |
| `ENVIRONMENT` | `development` | Environment name |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `ENABLE_METRICS_ENDPOINT` | `true` | Serve `GET /metrics/runtime` |
| `ENABLE_REQUEST_LOGGING` | `true` | Emit per-stage/request `query_event` log lines |
| `EVAL_GOLDEN_SET_DIR` | `<repo>/eval/golden_sets` | Golden datasets read by `evaluate.py` (absolute default; override only to point elsewhere) |
| `EVAL_REPORT_DIR` | `<repo>/eval/reports` | Reports written by `evaluate.py` (absolute default) |

Every value above is validated when the settings object is built: a bad
chunking relationship, a `top_k` maximum below its default, an agent bound
outside 1–3, an unparseable `LOG_LEVEL` or a non-`http(s)` origin stops the
process at startup instead of failing later per request. Copy
`backend/.env.example` to `backend/.env` and edit the placeholders.

### Frontend (`frontend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend API URL (build-time) |
| `VITE_UPLOAD_TIMEOUT_MS` | `120000` | Upload timeout in milliseconds |

---

## Docker configuration details

### Backend container

- Base image: `python:3.13-slim` (Phase 17: matches the interpreter the
  project is pinned and verified on; `requirements.txt` is fully pinned, and
  torch is installed from the CPU wheel index first so the image never pulls a
  CUDA build)
- Runs as non-root user (`appuser`, uid/gid 1000)
- Exposes port 8000
- Binds uvicorn to `0.0.0.0`
- Reads configuration from environment variables
- Reaches host Ollama via `http://host.docker.internal:11434`
- Reaches Supabase via the configured `DATABASE_URL`
- Image contents are exactly `app/`, `requirements.txt` and the `Dockerfile`:
  `.dockerignore` keeps `.env`, virtualenvs, bytecode caches, the test suite
  (`tests/`, `pytest.ini`), the phase regression scripts (`scripts/`),
  `requirements-dev.txt` and `evaluate.py` out of the image — no secrets, no
  test tooling, no evaluation harness ships in the release artifact
- No model cache is baked in (by design, unchanged since Phase 13): the
  embedding model is fetched on first use into the container's home cache

### Frontend container

- Multi-stage build:
  1. `node:22-alpine` — builds the Vite production bundle
  2. `nginx:alpine` — serves static files
- `VITE_API_BASE_URL` is injected as a build argument
- Serves the SPA with `try_files $uri $uri/ /index.html`
- Exposes port 5173 externally, maps to nginx port 80 internally

### Linux compatibility

For Linux Docker hosts (not Docker Desktop), add to `docker-compose.yml`:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

This is already included in the provided `docker-compose.yml`.

---

## Release verification (Phase 17)

Two entry points cover the release gate. Both exit non-zero on the first
failure, name the stage that failed, and never retry to mask flakiness.

### Against a running backend (native)

```bash
python scripts/verify_release.py              # full gate
python scripts/verify_release.py --offline    # deterministic stages only (CI subset)
```

The full gate runs: syntax check → import validation → pytest → chunking
driver → deployment-contract driver → `scripts/run_all_regressions.py` →
frontend lint/tests/build → `scripts/api_contract_probe.py` (27 contract and
security checks) → `scripts/e2e_smoke.py` (24 end-to-end checks) →
`backend/evaluate.py`, then verifies the evaluation report itself
(21/21 cases, 100% retrieval hit-rate, 100% citation validity, 100% correct
rejection). `--offline` stops before the live stages and prints them as
NOT RUN rather than as passed.

### Against the Docker deployment

```bash
# 1. build and start — ports 8000/5173 must be free (stop a native uvicorn first)
docker compose build
docker compose up -d

# 2. health, readiness, frontend
curl http://localhost:8000/health             # {"status":"ok"}
curl http://localhost:8000/health/ready       # {"status":"ok","checks":{"database":"ok","ollama":"ok"}}
curl -I http://localhost:5173                 # 200, nginx serves the built SPA

# 3. the live gates, pointed at the container
python backend/scripts/api_contract_probe.py --base-url http://localhost:8000
python backend/scripts/e2e_smoke.py          --base-url http://localhost:8000
python backend/evaluate.py                   --base-url http://localhost:8000

# 4. the release image must be non-root and must not ship secrets or test tooling
docker run --rm --entrypoint id agentic-document-ai-backend:latest   # uid=1000(appuser)
docker run --rm --entrypoint ls agentic-document-ai-backend:latest /app
#   -> Dockerfile  app  requirements.txt      (no .env, tests/, scripts/, evaluate.py)

# 5. tear down
docker compose down
```

**Cold start:** the image bakes no model cache (unchanged from Phase 13), so
the *first* ingestion in a fresh container downloads the embedding model
(~80 MB) into the container layer before chunks can be embedded. That download
can take minutes on a slow connection and exceed the E2E probe's client
timeout — the download continues server-side, so warm the container once
(upload any PDF, wait for `status: ready`) and then run step 3. Later uploads
are unaffected because the model stays in the container layer.

---

## Troubleshooting

### "Cannot reach backend server"

- Ensure the backend container is running: `docker compose ps`
- Check backend logs: `docker compose logs backend`
- Verify `OLLAMA_BASE_URL` is set to `http://host.docker.internal:11434` in `backend/.env`

### "Ollama service unavailable"

- Ensure Ollama is running natively on the host: `curl http://localhost:11434/api/tags`
- Pull the model: `ollama pull qwen3:4b`
- Check that Docker can reach the host: from inside the backend container, `host.docker.internal` must resolve

### "Database unreachable"

- Verify your Supabase project is active (free tier may pause after inactivity)
- Check the `DATABASE_URL` in `backend/.env`
- Supabase free tier requires `sslmode=require`

### Frontend shows blank page

- Check that the frontend container is running: `docker compose ps`
- Verify nginx is serving files: `docker compose logs frontend`
- Ensure `VITE_API_BASE_URL` was set correctly at build time

### Upload fails

- Check `MAX_UPLOAD_SIZE_MB` in `backend/.env`
- Large PDFs may take time; the frontend timeout is 120 seconds
- Ensure the embedding model is available (first run downloads ~80 MB)

---

## Known limitations

- **No authentication**: Anyone with a document ID can query it
- **No PDF persistence**: Uploaded files are not stored; re-upload after ingestion failure
- **Synchronous ingestion**: Large PDFs may take time
- **In-memory chat history**: Lost on page refresh or document switch
- **Local-first**: Ollama must run natively on the host
- **No streaming**: Answers appear after the full agent loop completes
- **Supabase free tier**: May pause after inactivity; wake the project before use
