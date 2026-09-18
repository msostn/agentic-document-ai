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
    OLLAMA_TIMEOUT_SECONDS: int = 120
    OLLAMA_TEMPERATURE: float = 0.1
    OLLAMA_NUM_PREDICT: int = 512
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
    RAG_CONTEXT_MAX_CHARS: int = 8000
    RAG_MIN_SIMILARITY: float = 0.30
    AGENT_MAX_ITERATIONS: int = 3
    AGENT_MAX_TOOL_CALLS: int = 3

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
        if self.RAG_CONTEXT_MAX_CHARS < 1:
            raise ValueError("RAG_CONTEXT_MAX_CHARS must be greater than 0")
        if self.RAG_MIN_SIMILARITY < -1.0 or self.RAG_MIN_SIMILARITY > 1.0:
            raise ValueError(
                f"RAG_MIN_SIMILARITY must be between -1.0 and 1.0 "
                f"(got {self.RAG_MIN_SIMILARITY})"
            )
        if self.OLLAMA_TIMEOUT_SECONDS < 1:
            raise ValueError(
                "OLLAMA_TIMEOUT_SECONDS must be at least 1 "
                f"(got {self.OLLAMA_TIMEOUT_SECONDS})"
            )
        if self.OLLAMA_NUM_PREDICT < 1:
            raise ValueError(
                "OLLAMA_NUM_PREDICT must be at least 1 "
                f"(got {self.OLLAMA_NUM_PREDICT})"
            )
        if self.AGENT_MAX_ITERATIONS < 1:
            raise ValueError(
                "AGENT_MAX_ITERATIONS must be at least 1 "
                f"(got {self.AGENT_MAX_ITERATIONS})"
            )
        if self.AGENT_MAX_TOOL_CALLS < 1:
            raise ValueError(
                "AGENT_MAX_TOOL_CALLS must be at least 1 "
                f"(got {self.AGENT_MAX_TOOL_CALLS})"
            )
        return self

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]


settings = Settings()
