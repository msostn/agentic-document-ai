"""Database setup is deferred until DATABASE_URL is configured (Phase 3)."""

from app.config import settings

engine = None
SessionLocal = None


def database_configured() -> bool:
    return bool(settings.DATABASE_URL.strip())
