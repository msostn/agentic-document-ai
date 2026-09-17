# Phase 4 Implementation Specification — PDF Upload, Extraction & Document Management

Status: NEXT (to be implemented by Cursor)
Parent spec: `ARCHITECTURE.md` (permanent source of truth — this phase must not contradict it)
Verified prior state: Phases 1–3 complete and committed. FastAPI backend runs. Supabase + pgvector connection works. `Document` and `DocumentChunk` SQLAlchemy models exist with `embedding` fixed at `vector(384)`. Frontend is still the unmodified Vite scaffold. No upload, extraction, chunking, embedding, retrieval, or agent logic exists yet.

---

## 1. Objective

Allow a user to upload a PDF, have its text extracted page-by-page in memory, and have the system record whether that document is usable (`ready`) or not (`failed`) — with no chunking, no embeddings, no vector storage, and no persistent storage of the PDF file itself. This phase also delivers full CRUD-style document management (`list`, `get`, `delete`) needed by every later phase and by the frontend.

---

## 2. Scope

**In scope:** file upload endpoint, validation, filename sanitization, page-aware PDF text extraction (PyMuPDF), document status lifecycle (`processing` → `ready`/`failed`), list/get/delete endpoints, Pydantic schemas, service layer, error handling.

**Explicitly out of scope for this phase:** chunking (Phase 5), embeddings (Phase 6), writing to `document_chunks` (Phase 7), vector/semantic search (Phase 8), Ollama/agent/chat (Phases 9–12), any frontend UI work (Phase 13+). The `document_chunks` table is not touched by any code written in this phase.

---

## 3. Files to Create / Modify

**Created:**
- `backend/app/rag/parser.py` — PDF text extraction logic (PyMuPDF).
- `backend/app/services/document_service.py` — validation, sanitization, orchestration, DB operations for documents.
- `backend/app/schemas/document.py` — Pydantic request/response models.
- `backend/app/routes/documents.py` — the five document endpoints.

**Modified:**
- `backend/app/schemas/__init__.py` — export the new schemas.
- `backend/app/routes/__init__.py` — export the new router.
- `backend/app/main.py` — register the documents router with the FastAPI app.

**Not modified:**
- `backend/app/database.py` — existing `engine`/`SessionLocal`/`Base`/`get_db()` are sufficient as-is.
- `backend/app/models/document.py`, `backend/app/models/document_chunk.py` — schema from Phase 3 is unchanged; this phase does not add columns.
- `backend/requirements.txt` — `pymupdf` was already installed in Phase 1; verify it's importable, do not reinstall or add new packages.
- `backend/app/config.py` — `max_upload_size_mb` already exists from Phase 1; no new environment variables are introduced by this phase.

---

## 4. Responsibilities of Each File

**`app/rag/parser.py`**
Pure extraction logic, no database or HTTP awareness. Responsible for:
- Opening PDF bytes in memory (never writing to disk).
- Extracting text per page, 1-indexed page numbers.
- Distinguishing "file could not be opened/parsed" (corrupted/invalid PDF) from "file opened but has no extractable text" (likely scanned/image-only) — these are different failure modes and must be raised/returned distinguishably.
- Applying a minimum-extractable-content threshold to decide whether the document counts as having usable text.
- This module will be reused unmodified by the Phase 7 ingestion pipeline — do not embed any upload- or database-specific logic here.

**`app/services/document_service.py`**
Orchestration and business rules, no direct HTTP concerns (no `Request`/`UploadFile` handling beyond receiving already-read bytes and a filename). Responsible for:
- Filename sanitization.
- File type and size validation logic (the actual checks; routes just call into this).
- Creating the `Document` row with `status = processing`.
- Invoking `rag/parser.py` and interpreting its result.
- Updating the `Document` row to `ready` or `failed`.
- List/get/delete operations against the `documents` table.

**`app/schemas/document.py`**
- `DocumentResponse`: `id`, `filename`, `file_type`, `status`, `created_at` — mirrors the `documents` table exactly, no extra computed fields.
- No request schema is needed for upload (it's a multipart file field, not JSON body).

**`app/routes/documents.py`**
- Declares the five endpoints, handles `UploadFile` reading, translates service-layer outcomes into HTTP responses and status codes, raises `HTTPException` for error cases. Contains no extraction or sanitization logic itself — delegates entirely to the service layer.

**`app/main.py`**
- Adds `app.include_router(documents.router)` (or equivalent), alongside the existing `/health` route. No other changes.

---

## 5. API Contracts

### `POST /documents/upload`
- Request: `multipart/form-data`, single field `file` (PDF).
- Success response: `201 Created`
  ```
  {
    "id": "uuid",
    "filename": "sanitized_name.pdf",
    "file_type": "pdf",
    "status": "ready",
    "created_at": "2026-01-01T00:00:00Z"
  }
  ```
- Validation failure (wrong file type): `415 Unsupported Media Type`, `{"detail": "Only PDF files are supported."}`. No `Document` row is created.
- Validation failure (too large): `413 Request Entity Too Large`, `{"detail": "File exceeds maximum size of 25 MB."}`. No `Document` row is created.
- Processing failure (no extractable text or corrupted file): `422 Unprocessable Entity`
  ```
  {
    "detail": {
      "message": "No extractable text found in this document. Scanned/image-only PDFs are not supported.",
      "document_id": "uuid"
    }
  }
  ```
  A `Document` row **is** created and persisted with `status = failed` (see §9 design decision).

### `GET /documents`
- `200 OK`, array of `DocumentResponse`, ordered by `created_at` descending (newest first).

### `GET /documents/{document_id}`
- `200 OK` with `DocumentResponse` if found.
- `404 Not Found`, `{"detail": "Document not found."}` if no matching row.
- Malformed UUID in the path is handled by FastAPI's own path validation (automatic 422) — no custom handling required.

### `DELETE /documents/{document_id}`
- `204 No Content` on success.
- `404 Not Found`, `{"detail": "Document not found."}` if no matching row.
- Deletion cascades to `document_chunks` at the database level (FK `ON DELETE CASCADE` already configured in Phase 3); no application code needs to delete chunks explicitly, even though the table is empty at this phase.

---

## 6. Database Interactions

Sequence for `POST /documents/upload`:
1. Validation (type, size) happens **before** any database write. On failure, return the error immediately — no row is created.
2. Insert one `documents` row with `status = "processing"`, commit immediately, so the row exists even if extraction subsequently fails or the process crashes mid-extraction.
3. Run extraction in memory (no DB activity during this step).
4. Update that same row's `status` to `"ready"` or `"failed"` based on the outcome, commit.
5. Return the final row state to the caller.

`GET /documents`, `GET /documents/{id}`: plain `SELECT` via SQLAlchemy session (`get_db()` dependency), no raw SQL.

`DELETE /documents/{id}`: `SELECT` to confirm existence (for the 404 case), then delete via the ORM session; rely on the DB-level cascade for `document_chunks` cleanup rather than deleting chunks manually.

No code in this phase writes to `document_chunks`.

---

## 7. Validation Behavior

**File type:**
- Filename must end in `.pdf` (case-insensitive). This is the primary check.
- If the client-supplied `Content-Type` header is present and is unambiguously not PDF-like (e.g. `image/png`, `text/plain`), reject early. Do not rely solely on `Content-Type`, since browsers/clients send it inconsistently — the authoritative check is PyMuPDF actually opening the file during extraction.

**File size:**
- Read the uploaded content length in bytes and compare against `settings.max_upload_size_mb` (25 MB default from config, not hard-coded in the route).
- Reject before creating any `Document` row if the limit is exceeded.

**Filename sanitization:**
- Strip any path components (use basename only) to prevent path traversal.
- Allow only a safe character set (letters, digits, `.`, `_`, `-`); replace anything else with `_`.
- Enforce a reasonable maximum length (e.g. 255 characters).
- Ensure the sanitized name still ends in `.pdf`.
- If sanitization would produce an empty or degenerate name, fall back to a generated name using the document's UUID (e.g. `document_<uuid>.pdf`).
- The **sanitized** name is what gets stored in `documents.filename` — the original client-supplied name is not persisted separately.

**Extractable-text threshold:**
- A document is considered to have usable text if the combined non-whitespace character count across all extracted pages exceeds a small fixed threshold (documented as a named constant in `parser.py`, not an environment variable — this is an internal implementation detail, not a user-facing configuration point).
- Zero pages, or total content below that threshold, is treated as "no extractable text."

---

## 8. Error Handling

| Scenario | Trigger | `Document` row state | HTTP response |
|---|---|---|---|
| Wrong file type | filename/content-type check fails | none created | 415 |
| File too large | byte length exceeds config limit | none created | 413 |
| Corrupted/unreadable PDF | PyMuPDF cannot open the file stream | created, `status = failed` | 422 |
| No extractable text (scanned/image-only) | opened successfully, text below threshold | created, `status = failed` | 422 |
| Successful extraction | opened successfully, text above threshold | created, `status = ready` | 201 |
| Unexpected exception during extraction | any uncaught error mid-process | created, `status = failed` (via try/except that always resolves the row before re-raising a controlled error) | 422 |
| Document not found (GET/DELETE) | no matching `id` | n/a | 404 |

No scenario should leave a `Document` row permanently stuck in `processing` — every code path that creates a row must resolve it to `ready` or `failed` before the request returns, including on unexpected exceptions (wrap extraction in try/except, update status to `failed` in the except block, then respond with 422).

---

## 9. Design Decision: Why Failed Extractions Still Return 422 With a Created Row

Rejected files (wrong type/size) never touch the database — there's nothing to record. But once a file passes validation and a `processing` row exists, a failed extraction is a real event worth keeping (the user did upload something; the system should be able to show them "this failed" rather than silently vanishing). The row persists with `status = failed`, and the API still returns 422 to make the client-facing failure unambiguous. The `document_id` is included in the error body specifically so the client (or user) can call `DELETE /documents/{id}` to clean it up if desired. Failed documents remain visible via `GET /documents` unless explicitly deleted.

---

## 10. Extraction Flow

1. Read the full uploaded file into memory as bytes (already size-validated).
2. Attempt to open the bytes as a PDF (PyMuPDF, in-memory stream — never write to disk). If this raises an exception, classify as "corrupted/unreadable PDF."
3. Iterate every page in order; extract text per page; record `(page_number, text)` pairs in memory only, with `page_number` starting at 1.
4. Strip and sum non-whitespace character counts across all pages.
5. If the total is below the minimum-extractable-content threshold (or there are zero pages), classify as "no extractable text."
6. Otherwise, classify as successful extraction.
7. Close/release the PDF object properly.
8. **Note on persistence:** the per-page text extracted here is used only to make the ready/failed decision in this phase — it is not written to `document_chunks` (that table requires a non-null `embedding`, which doesn't exist until Phase 6). The identical parser function will be called again by the Phase 7 ingestion pipeline, at which point its output feeds the chunker and embedding service. This is an intentional, temporary duplication of extraction work between Phase 4 and Phase 7, not a bug — it keeps each phase independently testable per the project's phased build strategy.

---

## 11. Testing Requirements

**TEST 1 — Regression check**
Command: `GET /health`
Expected: unchanged, `{"status":"ok"}`, 200.
Proves: new routes didn't break existing app wiring.

**TEST 2 — Valid PDF upload**
Command: `POST /documents/upload` with a real multi-page, text-based PDF.
Expected: 201, `status: "ready"` in response; a corresponding row exists in `documents` with the same id and status.
Proves: full success path works end to end.

**TEST 3 — Wrong file type**
Command: upload a `.png` or `.txt` file.
Expected: 415, no row created in `documents`.
Proves: type validation runs before any DB write.

**TEST 4 — Oversized file**
Command: upload a PDF larger than 25 MB (or temporarily lower `MAX_UPLOAD_SIZE_MB` in `.env` for testing).
Expected: 413, no row created.
Proves: size validation is enforced from configuration, not hard-coded.

**TEST 5 — Scanned/image-only PDF**
Command: upload a PDF known to contain only scanned images, no text layer.
Expected: 422 with the "no extractable text" message; a row exists with `status = failed`.
Proves: the no-OCR rejection path works and still records the attempt.

**TEST 6 — Corrupted file with `.pdf` extension**
Command: upload a non-PDF file renamed to `.pdf` (e.g. a text file renamed).
Expected: 422 with a "could not parse" style message; a row exists with `status = failed`.
Proves: extraction-level corruption is distinguished from the no-text case and handled without crashing the server.

**TEST 7 — List documents**
Command: `GET /documents` after several uploads (mix of ready/failed).
Expected: 200, array includes all created rows (both statuses), newest first.
Proves: listing doesn't filter out failed documents and ordering is correct.

**TEST 8 — Get single document**
Command: `GET /documents/{valid_id}` and `GET /documents/{random_uuid}`.
Expected: 200 with correct data for the valid id; 404 for the nonexistent one.

**TEST 9 — Delete document**
Command: `DELETE /documents/{valid_id}`, then `GET /documents/{same_id}`.
Expected: 204 on delete; 404 on the subsequent GET.
Proves: deletion actually removes the row.

**TEST 10 — Filename sanitization**
Command: upload a file with an unsafe name, e.g. `../../etc/passwd.pdf` or `my file (final) v2.pdf`.
Expected: the stored `filename` in the response/DB is sanitized (no path segments, no unsafe characters) while still ending in `.pdf`.
Proves: sanitization logic runs on every upload, not just "normal" filenames.

**TEST 11 — No disk persistence (manual/code-review check)**
Check: after an upload completes, confirm no PDF bytes were written anywhere on the container filesystem (no temp files left behind, no upload directory created).
Proves: requirement of zero persistent file storage is actually honored, not just assumed.

---

## 12. Acceptance Criteria

- [ ] `pymupdf` extraction logic lives in `app/rag/parser.py`, with no HTTP/DB code inside it.
- [ ] `POST /documents/upload` accepts multipart PDF uploads and enforces both type and size limits from configuration (`MAX_UPLOAD_SIZE_MB`), not hard-coded values.
- [ ] Filenames are sanitized before storage; no path traversal or unsafe characters can reach the database.
- [ ] A `Document` row is created with `status = "processing"` before extraction begins, and every code path resolves it to `"ready"` or `"failed"` before the request completes — no row is ever left stuck in `"processing"`.
- [ ] Extraction preserves `page_number` per page during processing (verified via logs/tests), even though page text is not persisted in this phase.
- [ ] Scanned/image-only PDFs and corrupted PDFs are both rejected (422) without crashing the server, and both leave a `status = "failed"` row.
- [ ] `GET /documents`, `GET /documents/{id}`, and `DELETE /documents/{id}` all behave exactly as specified in §5, with correct status codes for found/not-found cases.
- [ ] All request/response shapes match the `DocumentResponse` schema; no extra or missing fields.
- [ ] No code in this phase reads from or writes to `document_chunks`.
- [ ] No PDF file bytes are persisted to disk or any external storage at any point.
- [ ] All 11 tests in §11 pass.
- [ ] `ARCHITECTURE.md` constraints are respected: no new frameworks introduced, no authentication added, no raw SQL, secrets untouched, CORS unchanged.
