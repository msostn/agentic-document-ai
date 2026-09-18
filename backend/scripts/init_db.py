"""Create documents and document_chunks tables. Run once from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\init_db.py

Idempotent: safe to run repeatedly. ``create_all`` creates tables that do
not exist yet; the ALTER statements below bring an existing Phase 3 database
up to the Phase 7 schema (new documents columns + the ``empty`` status) and
are no-ops once applied.
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402, F401

# Phase 7 schema sync for databases created by Phase 3's init script.
# create_all cannot alter existing tables, so the documents columns and the
# status CHECK constraint are brought up to date explicitly. All statements
# are idempotent (the constraint is dropped and recreated with the same name,
# now also permitting the Phase 7 'empty' status).
_PHASE7_SYNC_SQL = [
    "ALTER TABLE documents DROP CONSTRAINT IF EXISTS ck_documents_status",
    "ALTER TABLE documents ADD CONSTRAINT ck_documents_status "
    "CHECK (status IN ('processing', 'ready', 'failed', 'empty'))",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message text",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS chunk_count integer",
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS processed_at timestamptz",
]


def main() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for statement in _PHASE7_SYNC_SQL:
            conn.execute(text(statement))
    print("Created tables: documents, document_chunks")
    print("Synced documents columns: error_message, chunk_count, processed_at")
    print("Synced documents status constraint to include 'empty'")
    print("Deferred: cosine vector index on document_chunks.embedding (Phase 7/8)")


if __name__ == "__main__":
    main()
