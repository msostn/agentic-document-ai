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

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # edit with your DATABASE_URL

# Ensure OLLAMA_BASE_URL=http://localhost:11434 in .env
python scripts/init_db.py
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend

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
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS allowed origins (comma-separated) |
| `ENVIRONMENT` | `development` | Environment name |
| `LOG_LEVEL` | `INFO` | Python logging level |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformers model |
| `MAX_UPLOAD_SIZE_MB` | `25` | Maximum upload size in MB |

### Frontend (`frontend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend API URL (build-time) |
| `VITE_UPLOAD_TIMEOUT_MS` | `120000` | Upload timeout in milliseconds |

---

## Docker configuration details

### Backend container

- Base image: `python:3.11-slim`
- Runs as non-root user (`appuser`)
- Exposes port 8000
- Binds uvicorn to `0.0.0.0`
- Reads configuration from environment variables
- Reaches host Ollama via `http://host.docker.internal:11434`
- Reaches Supabase via the configured `DATABASE_URL`

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
