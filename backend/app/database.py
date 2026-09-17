import re
from collections.abc import Generator
from urllib.parse import parse_qsl, unquote

from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

_URL_PATTERN = re.compile(
    r"^(?P<driver>[^:]+)://"
    r"(?P<user>[^:]+):"
    r"(?P<password>.*)@"
    r"(?P<host>[^:/]+):"
    r"(?P<port>\d+)/"
    r"(?P<database>[^?]+)"
    r"(?:\?(?P<query>.*))?$"
)


def _build_database_url() -> URL:
    raw = settings.DATABASE_URL.strip()
    if not raw:
        raise RuntimeError("DATABASE_URL is not configured.")

    match = _URL_PATTERN.match(raw)
    if match is None:
        raise RuntimeError("DATABASE_URL is not a valid PostgreSQL URL.")

    password = unquote(match.group("password"))
    if password.startswith("[") and password.endswith("]"):
        password = password[1:-1]

    query = dict(parse_qsl(match.group("query") or "", keep_blank_values=True))
    if "sslmode" not in query:
        query["sslmode"] = "require"

    driver = match.group("driver")
    if driver == "postgresql":
        driver = "postgresql+psycopg"

    return URL.create(
        drivername=driver,
        username=unquote(match.group("user")),
        password=password,
        host=match.group("host"),
        port=int(match.group("port")),
        database=match.group("database"),
        query=query,
    )


engine = create_engine(_build_database_url(), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
