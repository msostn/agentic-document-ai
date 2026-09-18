from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    DATABASE_URL: str = ""
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "qwen3:4b"
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_DEVICE: str = "cpu"
    MAX_UPLOAD_SIZE_MB: int = 25
    ALLOWED_ORIGINS: str = "http://localhost:5173"
    CHUNK_SIZE: int = 800
    CHUNK_OVERLAP: int = 150
    MIN_CHUNK_SIZE: int = 100
    RETRIEVAL_TOP_K_DEFAULT: int = 5
    RETRIEVAL_TOP_K_MAX: int = 20
    MAX_QUERY_LENGTH: int = 8000

    @model_validator(mode="after")
    def _validate_chunking(self) -> "Settings":
        if self.CHUNK_OVERLAP >= self.CHUNK_SIZE:
            raise ValueError(
                f"CHUNK_OVERLAP must be smaller than CHUNK_SIZE "
                f"(got overlap={self.CHUNK_OVERLAP}, size={self.CHUNK_SIZE})"
            )
        if self.MIN_CHUNK_SIZE >= self.CHUNK_SIZE:
            raise ValueError(
                f"MIN_CHUNK_SIZE must be smaller than CHUNK_SIZE "
                f"(got min={self.MIN_CHUNK_SIZE}, size={self.CHUNK_SIZE})"
            )
        if self.MIN_CHUNK_SIZE < 1:
            raise ValueError("MIN_CHUNK_SIZE must be at least 1")
        return self

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]


settings = Settings()
