"""Create documents and document_chunks tables. Run once from backend/:

    .\\.venv\\Scripts\\python.exe scripts\\init_db.py
"""

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

from app.database import Base, engine  # noqa: E402
from app.models import Document, DocumentChunk  # noqa: E402, F401


def main() -> None:
    Base.metadata.create_all(bind=engine)
    print("Created tables: documents, document_chunks")
    print("Deferred: cosine vector index on document_chunks.embedding (Phase 7/8)")


if __name__ == "__main__":
    main()
