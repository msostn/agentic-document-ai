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

- Python 3.11+
- Node.js 18+
- [Ollama](https://ollama.com/) installed and running
- Supabase PostgreSQL (free tier) or local PostgreSQL with pgvector

## Setup

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

## Testing

### Backend tests

```bash
cd backend
python scripts/test_phase12_frontend_integration.py
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

## Environment Variables

### Backend (`backend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | — | PostgreSQL connection URL (required) |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `qwen3:4b` | Ollama model name |
| `ALLOWED_ORIGINS` | `http://localhost:5173` | CORS allowed origins (comma-separated) |
| `ENVIRONMENT` | `development` | Environment name |

### Frontend (`frontend/.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `VITE_API_BASE_URL` | `http://localhost:8000` | Backend API URL |
| `VITE_UPLOAD_TIMEOUT_MS` | `120000` | Upload timeout in milliseconds |
