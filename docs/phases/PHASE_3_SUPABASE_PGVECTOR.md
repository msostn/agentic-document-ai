# Phase 3 Implementation Specification — Supabase PostgreSQL + pgvector

Status: NEXT (to be implemented by Cursor)
Parent spec: `ARCHITECTURE.md`
Verified prior state: Phase 1 (project skeleton) and Phase 2 (`GET /health` only, no DB functionality) are complete.

---

## 1. Objective

Make the database layer real. At the end of this phase, the backend must be able to open a live connection to a Supabase PostgreSQL database with `pgvector` enabled, and two tables (`documents`, `document_chunks`) must exist with the exact schema required by the rest of the pipeline. No ingestion, chunking, embedding, or retrieval logic is implemented in this phase — only the schema and a verified connection.

---

## 2. Architecture / Data Flow

This phase does not introduce a request-time data flow. It establishes the storage layer that Phases 4–8 will write to and read from.

```
FastAPI backend (app/database.py: engine, SessionLocal, Base)
        │
        │  DATABASE_URL (from backend/.env)
        ▼
Supabase PostgreSQL
        │
        ▼
pgvector extension enabled
        │
        ▼
documents table  ◄──FK──  document_chunks table (embedding: vector(384))
```

`app/database.py` already defines `engine`, `SessionLocal`, `Base`, and `get_db()` per Phase 2 — these were declared but never exercised, since `DATABASE_URL` was a placeholder and no model/table existed. This phase supplies a real `DATABASE_URL`, the model definitions, and the table creation step, making that existing code path functional for the first time.

---

## 3. Required Database Schema

**`documents`**

| Column | Type | Constraints |
|---|---|---|
| id | UUID | primary key, server default random UUID |
| filename | text | not null |
| file_type | text | not null |
| status | text | not null; one of `processing`, `ready`, `failed` |
| created_at | timestamp (with time zone) | not null, default now |

**`document_chunks`**

| Column | Type | Constraints |
|---|---|---|
| id | UUID | primary key, server default random UUID |
| document_id | UUID | not null, foreign key → `documents.id`, `ON DELETE CASCADE`, indexed |
| chunk_index | integer | not null |
| page_number | integer | not null |
| content | text | not null |
| embedding | vector(384) | not null |

`ON DELETE CASCADE` on `document_id` is required so that `DELETE /documents/{id}` (a later phase) can remove a document's chunks without orphaned rows — specify this now so the FK doesn't need to be altered later.

**Indexes:**
- B-tree index on `document_chunks.document_id` (required now — this is the column every retrieval query filters on).
- Vector index on `document_chunks.embedding` (IVFFlat or HNSW) — may be deferred to Phase 7/8 if Supabase's pgvector version behaves better with a populated table, but this must be noted as deferred, not silently skipped.

`conversations` and `messages` tables (per `ARCHITECTURE.md` §6) are **not** part of this phase's scope — they are not required until the chat/agent phases.

---

## 4. Required SQLAlchemy Models

Two new model modules under `backend/app/models/`, both inheriting from the existing `Base` in `app/database.py`:

- `Document` — maps to `documents`, with a `chunks` relationship to `DocumentChunk` (one-to-many, cascade delete configured on the relationship to mirror the DB-level `ON DELETE CASCADE`).
- `DocumentChunk` — maps to `document_chunks`, with the `embedding` column typed using the `pgvector` SQLAlchemy `Vector` type, fixed at dimension 384, and a `document` back-reference to `Document`.

`backend/app/models/__init__.py` must import and re-export both models so that `Base.metadata` is aware of them when table creation runs (SQLAlchemy only registers models that have been imported somewhere in the process).

No other application module should define its own table metadata or bypass these models for reads/writes.

---

## 5. pgvector Configuration

- Extension must be enabled at the database level (`CREATE EXTENSION IF NOT EXISTS vector;`) — see §10, manual step.
- Embedding dimension is fixed at **384**, matching `all-MiniLM-L6-v2` (the model configured in `EMBEDDING_MODEL`). This value is hard-coded in the `DocumentChunk.embedding` column definition, not derived dynamically at runtime.
- Documented constraint (carried from `ARCHITECTURE.md` §4): if `EMBEDDING_MODEL` changes to a model with a different output dimension in the future, the column and all stored vectors must be recreated/re-embedded manually. No auto-migration logic should be built for this.
- Similarity metric to be used in later retrieval phases: cosine distance. No query logic is implemented in this phase, but the vector index (whenever created) should be created with cosine distance as the operator class, since Phase 8 will use cosine similarity search.

---

## 6. Required Dependencies

No new packages are required — `sqlalchemy`, `psycopg[binary]`, and `pgvector` were already installed in Phase 1's `requirements.txt`. Cursor should verify these are present and importable in the existing `.venv`, not reinstall or add anything new (no Alembic, no migration framework — table creation for this MVP is handled directly via `Base.metadata.create_all()`, invoked from a standalone script, not automatically on every app startup).

---

## 7. Environment Variables

Already declared in `backend/.env.example` since Phase 1 — no new variable names are introduced. This phase is what makes `DATABASE_URL` a real, functional value rather than a placeholder.

```
DATABASE_URL=postgresql://postgres:[YOUR-PASSWORD]@[YOUR-SUPABASE-HOST]:5432/postgres
```

- The real value goes only in `backend/.env` (gitignored), never in `.env.example`.
- `.env.example` keeps the placeholder form shown above so the repository documents the required shape without leaking credentials.

---

## 8. Files to Be Created / Modified

**Created:**
- `backend/app/models/document.py` — `Document` model
- `backend/app/models/document_chunk.py` — `DocumentChunk` model
- `backend/scripts/init_db.py` — standalone script that imports `Base`, `engine`, and both models, then calls `Base.metadata.create_all(bind=engine)`. Run manually, once, not on app startup — this keeps schema creation an explicit, auditable action rather than something that silently runs on every server boot.

**Modified:**
- `backend/app/models/__init__.py` — import/export `Document` and `DocumentChunk` so their metadata is registered.
- `backend/.env` — real `DATABASE_URL` filled in (manual, by user — see §10).

**Not modified:**
- `backend/app/database.py` — its `engine` / `SessionLocal` / `Base` / `get_db()` definitions from Phase 2 are already correct and sufficient; do not restructure this file unless a genuine defect is found.
- `backend/app/main.py` — no new routes are added in this phase. A DB-connectivity check is performed via the script in §11, not via a new API endpoint.

---

## 9. Security Constraints

- `DATABASE_URL` lives only in `backend/.env`; it must never be committed, logged, or returned in any API response.
- No raw/interpolated SQL strings — all schema and (later) query access goes through SQLAlchemy's ORM/Core, which parameterizes values automatically.
- The database credentials are backend-only; the frontend has no knowledge of `DATABASE_URL` or any DB connection detail, now or later.
- Foreign key cascade behavior is defined at the schema level so that deletions are handled by the database itself rather than by ad hoc application-level cleanup code.

---

## 10. Manual Supabase Setup Steps

**ACTION REQUIRED FROM USER (Cursor cannot perform these):**

1. Create a free Supabase project at supabase.com (if one doesn't already exist).
2. In the Supabase dashboard, go to **Database → Extensions**, search for `vector`, and enable it. (Equivalent SQL, runnable from the Supabase SQL Editor: `CREATE EXTENSION IF NOT EXISTS vector;`)
3. Go to **Project Settings → Database → Connection string**. Use the **Session pooler** or **direct connection** string (port 5432) rather than the **Transaction pooler** (port 6543) — the transaction pooler is designed for short-lived serverless connections and is not the right fit for a persistent FastAPI backend process.
4. Copy that connection string into `backend/.env` as `DATABASE_URL`, substituting in the actual password and host Supabase provides.
5. Confirm the project is not paused (Supabase free-tier projects pause after a period of inactivity — if a connection attempt fails, check the dashboard for a "paused" state and resume it).

---

## 11. Testing Requirements

**TEST 1 — Extension enabled**
Command: run `SELECT * FROM pg_extension WHERE extname = 'vector';` in the Supabase SQL Editor.
Expected: one row returned.
Proves: `pgvector` is active on the database.

**TEST 2 — Table creation**
Command: run `python scripts/init_db.py` from `backend/` (with `.venv` active).
Expected: script completes with no errors.
Proves: SQLAlchemy can connect using `DATABASE_URL` and successfully issues `CREATE TABLE` statements.

**TEST 3 — Schema verification**
Command: in the Supabase SQL Editor, run `\d documents` and `\d document_chunks` (or use the Table Editor UI).
Expected: columns, types, and constraints match §3 exactly, including `embedding` shown as `vector(384)`.
Proves: the schema matches specification, not just "some tables exist."

**TEST 4 — Foreign key + cascade behavior**
Command: manually insert one row into `documents`, then one row into `document_chunks` referencing that `document_id` (a zero-vector or random 384-length array is acceptable for this manual test only — no embedding logic exists yet). Then delete the `documents` row.
Expected: the insert succeeds; after deleting the parent row, the corresponding `document_chunks` row is also gone.
Proves: the FK relationship and cascade delete are correctly configured.

**TEST 5 — Backend connectivity**
Command: from a Python shell inside `.venv`, import `SessionLocal` from `app.database`, open a session, and run a trivial query such as `db.query(Document).all()`.
Expected: returns an empty list (or the test row if not yet cleaned up), no connection errors.
Proves: the FastAPI backend process itself (not just the SQL editor) can reach the database using the configured `DATABASE_URL`.

---

## 12. Acceptance Criteria

- [ ] Supabase project is created and not paused.
- [ ] `pgvector` extension is enabled (Test 1 passes).
- [ ] `backend/.env` contains a real, working `DATABASE_URL`; `.env` remains gitignored.
- [ ] `documents` and `document_chunks` tables exist exactly as specified in §3 (Test 3 passes).
- [ ] `document_chunks.embedding` is typed `vector(384)`.
- [ ] Foreign key from `document_chunks.document_id` to `documents.id` exists with `ON DELETE CASCADE` (Test 4 passes).
- [ ] B-tree index exists on `document_chunks.document_id`.
- [ ] `backend/app/models/document.py` and `backend/app/models/document_chunk.py` exist and are correctly registered via `models/__init__.py`.
- [ ] `backend/scripts/init_db.py` exists and successfully creates the schema when run (Test 2 passes).
- [ ] The FastAPI backend process can open a session and query the database with no errors (Test 5 passes).
- [ ] No new API routes were added; `GET /health` remains the only endpoint, unchanged from Phase 2.
- [ ] No ingestion, chunking, embedding, or search logic exists yet — this phase is schema and connectivity only.
