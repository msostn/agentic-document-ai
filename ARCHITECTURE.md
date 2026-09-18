# Agentic Document Intelligence — Architecture Specification

Status: Source of truth for implementation. General-purpose document Q&A system (not insurance-specific). This document defines *what* to build and *why*. It is not an implementation guide.

---

## 1. Project Goal

Build a system where a user uploads any large text-based PDF (insurance policy, credit card terms, legal contract, research paper, university handbook, product manual, government document, etc.) and asks natural-language questions about it. An AI agent retrieves only the relevant parts of that specific document and answers using only that retrieved content. If the answer isn't in the document, the system says so rather than guessing.

Non-goals: this is not domain-specific to insurance or finance, not a general chatbot, not a multi-document synthesis tool, and not a system that supplements answers with outside/world knowledge.

Success is defined by one flow: **Upload → RAG → Agent → Tool Call → Retrieval → LLM → Grounded Answer → Sources.**

---

## 2. System Architecture

Three-tier application, single backend service, no microservices, no message queues.

```
┌────────────┐        HTTPS/JSON        ┌────────────────┐
│  Frontend  │ ───────────────────────> │  FastAPI        │
│  React +   │ <─────────────────────── │  Backend        │
│  Vite + TS │                          │  (single app)   │
└────────────┘                          └────────┬────────┘
                                                  │
                       ┌──────────────────────────┼──────────────────────────┐
                       ▼                          ▼                          ▼
              ┌─────────────────┐        ┌─────────────────┐        ┌──────────────────┐
              │ sentence-        │        │ Ollama            │        │ PostgreSQL         │
              │ transformers     │        │ (local LLM,        │        │ + pgvector         │
              │ (local embedding)│        │  agent + chat)     │        │ (Supabase free tier)│
              └─────────────────┘        └─────────────────┘        └──────────────────┘
```

**Design principles:**
- One backend service owns all business logic. Frontend never talks to the DB, Ollama, or the embedding model directly.
- Ollama and the embedding model run locally (or on whatever machine hosts the backend). No paid API calls anywhere in the core flow.
- The agent is a single agent with one primary tool. No multi-agent orchestration, no LangChain/LangGraph in the initial build.
- Every retrieval operation is scoped to one `document_id`. There is no cross-document retrieval path in the codebase.

---

## 3. Data Flow

### 3.1 Ingestion flow (upload-time, synchronous or background)
```
User uploads PDF
 → File validated (type, size, filename sanitized)
 → Document record created (status = "processing")
 → PyMuPDF extracts text page-by-page (page_number preserved)
 → If no extractable text found → status = "failed", user notified (no OCR fallback in MVP)
 → Page-aware chunker splits text into overlapping chunks
 → sentence-transformers generates one embedding vector per chunk
 → Chunks + embeddings + page_number + document_id written to document_chunks
 → Document status = "ready"
```

### 3.2 Retrieval flow (search-time)
```
User selects a document and sends a query
 → Backend embeds the query via sentence-transformers (one embedding call)
 → pgvector cosine similarity search in document_chunks
     WHERE document_id = <selected document> (hard filter, always applied)
     ORDER BY embedding <=> query_embedding
     LIMIT top_k
 → Returns top-K chunks with chunk_id, page_number, content, distance, similarity
 → No answer generation — retrieved chunks only (LLM integration is a future phase)
```

### 3.3 Isolation guarantee
`document_id` is a mandatory filter on every retrieval query. There is no code path where `search_document()` can execute without a `document_id` bound to it. This is enforced at the query-construction level, not just by convention.

---

## 4. RAG Pipeline

| Stage | Responsibility | Notes |
|---|---|---|
| Parsing | Extract raw text per page from PDF | PyMuPDF; scanned/image-only PDFs are explicitly rejected in MVP (no OCR) |
| Chunking | Split page text into overlapping chunks | Chunk size and overlap are configurable via env vars (`CHUNK_SIZE`, `CHUNK_OVERLAP`); each chunk retains its source `page_number` |
| Embedding | Convert chunk text into a fixed-length vector | Local `sentence-transformers` model (default `all-MiniLM-L6-v2`); model is loaded once and reused, not per-request |
| Storage | Persist chunk text + vector + metadata | PostgreSQL with `pgvector`; embedding column dimension must match the embedding model's actual output dimension |
| Retrieval | Similarity search scoped to one document | Cosine similarity via pgvector; always filtered by `document_id`; returns top-K chunks (K configurable, default 5, max 20) |
| Grounding | Constrain generation to retrieved content | Enforced via system prompt + response validation, not just prompt suggestion |

**Chunk record contract:** every stored chunk must carry `document_id`, `chunk_index`, `page_number`, `content`, and `embedding`. No chunk exists without a page number and a parent document.

**Embedding model swap rule:** changing `EMBEDDING_MODEL` requires re-embedding all existing chunks (dimension mismatch otherwise breaks the vector column). This is a known operational constraint, not something the system auto-migrates.

---

## 5. Agent & Tool Architecture

**Agent model:** one agent, implemented as a tool-calling loop against the local Ollama model (function/tool-calling API, not a hard-coded `if/else` pipeline). The LLM itself decides whether a question needs document retrieval.

**Agent loop (conceptual):**
1. Receive user question + conversation context.
2. Send to LLM with tool schema and system prompt.
3. If the LLM requests a tool call → execute it → feed result back to the LLM.
4. If the LLM returns a direct text answer → validate it against grounding rules before returning.
5. Return final answer + collected sources to the caller.

The loop supports at most one round of tool calling per question in the MVP (call `search_document`, get results, answer). Multi-hop tool chains are out of scope initially.

**Primary tool: `search_document`**

| Aspect | Definition |
|---|---|
| Purpose | Retrieve semantically relevant chunks from the currently selected document |
| Input | `query` (natural-language string derived by the LLM from the user's question) |
| Implicit binding | `document_id` — always injected by the backend, never supplied by the LLM |
| Behavior | Embeds `query`, runs vector similarity search filtered by `document_id`, returns top-K chunks |
| Output | List of `{ chunk_id, page_number, content, similarity_score }` |
| Failure mode | If no chunks clear a minimum relevance threshold, returns an empty result set, which the agent must treat as "not found in document" |

**System prompt responsibilities (grounding):**
- Instruct the model to answer using only tool-retrieved content.
- Instruct the model to respond with a fixed fallback phrase when retrieved content is insufficient (e.g., "I couldn't find enough information to answer this question in the uploaded document.").
- Explicitly forbid filling gaps with general world knowledge, inference beyond the text, or invented figures/dates/terms.

**Future extension path (not built now):** the same tool-calling contract can be ported to LangGraph as a single-node ReAct-style graph without changing the tool interface or database layer.

---

## 6. Database Schema

Engine: PostgreSQL with the `pgvector` extension (Supabase free tier). ORM: SQLAlchemy.

**`documents`**
| Column | Type | Notes |
|---|---|---|
| id | UUID / PK | |
| filename | text | sanitized original filename |
| file_type | text | e.g. `pdf` |
| status | text | `processing` \| `ready` \| `failed` \| `empty` |
| error_message | text | set when ingestion fails (nullable) |
| chunk_count | integer | persisted chunk rows after ingestion (nullable) |
| processed_at | timestamp | last successful ingestion (nullable) |
| created_at | timestamp | |

**`document_chunks`**
| Column | Type | Notes |
|---|---|---|
| id | UUID / PK | |
| document_id | UUID / FK → documents.id | indexed; mandatory filter on all retrieval |
| chunk_index | integer | order within document |
| page_number | integer | source page |
| content | text | chunk text |
| embedding | vector(N) | N = actual output dimension of the configured embedding model |

**`conversations`**
| Column | Type | Notes |
|---|---|---|
| id | UUID / PK | |
| document_id | UUID / FK → documents.id | one conversation is scoped to one document |
| created_at | timestamp | |

**`messages`**
| Column | Type | Notes |
|---|---|---|
| id | UUID / PK | |
| conversation_id | UUID / FK → conversations.id | |
| role | text | `user` \| `assistant` |
| content | text | |
| created_at | timestamp | |

**Indexes:** vector index (e.g. IVFFlat or HNSW, whichever pgvector/Supabase version supports) on `document_chunks.embedding`; standard B-tree index on `document_chunks.document_id`.

---

## 7. API Contracts

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check → `{ "status": "ok" }` |
| POST | `/documents/upload` | Multipart PDF upload → triggers ingestion pipeline (persists chunks + embeddings) |
| GET | `/documents` | List all uploaded documents (id, filename, status) |
| GET | `/documents/{document_id}` | Fetch metadata for one document |
| POST | `/documents/{document_id}/ingest` | (Re-)ingest a document with optional PDF bytes and `force` flag |
| DELETE | `/documents/{document_id}` | Remove document + its chunks |
| POST | `/documents/{document_id}/search` | Semantic vector retrieval (top-K chunks scoped to document) |

**Chat request:**
```
{ "message": "What is the annual fee?" }
```

**Chat response:**
```
{
  "answer": "The annual fee is ...",
  "sources": [
    { "page": 12, "chunk_id": "...", "text": "..." }
  ]
}
```

Empty or low-confidence retrieval must still return HTTP 200 with the fallback "not found in document" answer and an empty or minimal `sources` array — this is a normal, expected response, not an error.

All endpoints use Pydantic request/response schemas and return proper HTTP status codes (400 for bad input, 404 for missing document, 415 for unsupported file type, 500 for unexpected failures).

---

## 8. Constraints

**Zero-cost:** No paid LLM APIs, no paid embedding APIs, no paid vector DB, no paid hosting required for the core system to function locally. Free-tier limitations (Supabase pausing, Render cold starts) must be documented, not hidden.

**Document isolation:** `document_id` filtering is mandatory and non-optional on every chunk query. No endpoint may search across all documents.

**Grounding:** No fallback to world knowledge. Refusal is the correct behavior when retrieval is insufficient.

**Security:** environment variables for all secrets; `.env` never committed; PDF type/size validation; filename sanitization; CORS restricted to known frontend origin(s); no AI credentials or DB URLs exposed to the frontend; parameterized queries only (no raw SQL string interpolation).

**Simplicity boundaries:** single agent, single primary tool, no LangChain/LangGraph in the initial build, no Redis/Kafka/Kubernetes, no multi-agent orchestration, no authentication system in the MVP.

---

## 9. Phase Roadmap

| Phase | Deliverable |
|---|---|
| 0 | Environment verified (Python, Node, Git, Ollama, GPU check) |
| 1 | Project skeleton (frontend + backend folders, venv, base deps) |
| 2 | `GET /health` working |
| 3 | Supabase Postgres + pgvector schema created and reachable |
| 4 | PDF upload + page-aware text extraction |
| 5 | Page-aware chunking with configurable size/overlap |
| 6 | Local embedding service (load-once, reusable) |
| 7 | Full ingestion pipeline writing chunks + embeddings to pgvector |
| 8 | Semantic vector retrieval (top-K cosine search scoped to document) |
| 9 | Ollama connected; basic prompt/response verified |
| 10 | Tool-calling agent loop implemented (LLM decides to call `search_document`) |
| 11 | Strict grounding system prompt + refusal behavior verified |
| 12 | Agent wired into `POST /documents/{id}/chat` |
| 13 | Frontend upload UI |
| 14 | Frontend chat UI |
| 15 | Frontend source/citation display |
| 16 | Full test pass (see §10) |
| 17 | Dockerfile + docker-compose for backend |
| 18 | Deployment (frontend, backend, DB; explicit LLM hosting tradeoff documented) |

---

## 10. Acceptance Criteria

The system is considered correct when, for a given uploaded document, all of the following hold:

- A valid text-based PDF uploads successfully and reaches `status = ready`.
- An invalid file (wrong type, too large, scanned/no text) is rejected with a clear error, not a silent failure.
- Chunks in the database always have a non-null `document_id`, `page_number`, and `embedding`.
- A question answerable from the document returns a correct answer with correct page-number sources.
- A question not answerable from the document returns the fixed "not found in document" response, not a hallucinated one.
- A question with no connection to the document (general trivia) is also refused, not answered from world knowledge.
- With two documents uploaded, a question against Document A never returns chunks or content from Document B.
- The agent visibly goes through a tool-call step for document-dependent questions (verifiable in logs/traces), rather than the answer being hard-coded from a direct search-then-generate shortcut.
- The full flow (upload → chat → sourced answer) works end-to-end through the frontend, not just via API testing tools.

---

## 11. Things Explicitly NOT to Build (MVP)

- No LangChain, LangGraph, CrewAI, AutoGen, or any multi-agent framework.
- No multi-agent systems — one agent only.
- No OCR / scanned-document support.
- No authentication or user accounts.
- No Redis, Kafka, Kubernetes, or microservices split.
- No multi-hop / recursive tool-calling chains.
- No cross-document search or multi-document synthesis.
- No paid LLM, embedding, database, or hosting services.
- No admin dashboards, analytics, or settings panels.
- No automatic re-embedding pipeline on model swap (manual/documented process only).
