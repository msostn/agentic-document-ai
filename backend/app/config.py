from pathlib import Path
import logging

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
    OLLAMA_NUM_PREDICT: int = 2048
    EMBEDDING_MODEL: str = "all-MiniLM-L6-v2"
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_DEVICE: str = "cpu"
    MAX_UPLOAD_SIZE_MB: int = 25
    ENVIRONMENT: str = "development"
    ALLOWED_ORIGINS: str = "http://localhost:5173"
    LOG_LEVEL: str = "INFO"
    CHUNK_SIZE: int = 500
    CHUNK_OVERLAP: int = 100
    MIN_CHUNK_SIZE: int = 100
    RETRIEVAL_TOP_K_DEFAULT: int = 5
    RETRIEVAL_TOP_K_MAX: int = 20
    MAX_QUERY_LENGTH: int = 8000
    RAG_CONTEXT_MAX_CHARS: int = 8000
    RAG_MIN_SIMILARITY: float = 0.30
    AGENT_MAX_ITERATIONS: int = 3
    AGENT_MAX_TOOL_CALLS: int = 3
    # Phase 14 — observability / evaluation
    ENABLE_METRICS_ENDPOINT: bool = True
    ENABLE_REQUEST_LOGGING: bool = True
    EVAL_GOLDEN_SET_DIR: str = str(BACKEND_DIR.parent / "eval" / "golden_sets")
    EVAL_REPORT_DIR: str = str(BACKEND_DIR.parent / "eval" / "reports")

    @model_validator(mode="after")
    def _validate_chunking(self) -> "Settings":
        if self.CHUNK_SIZE < 1:
            raise ValueError(
                f"CHUNK_SIZE must be at least 1 (got {self.CHUNK_SIZE})"
            )
        if self.CHUNK_OVERLAP < 0:
            raise ValueError(
                f"CHUNK_OVERLAP must be >= 0 (got {self.CHUNK_OVERLAP})"
            )
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
        if self.MAX_UPLOAD_SIZE_MB < 1:
            raise ValueError(
                "MAX_UPLOAD_SIZE_MB must be at least 1 "
                f"(got {self.MAX_UPLOAD_SIZE_MB})"
            )
        if self.MAX_QUERY_LENGTH < 1:
            raise ValueError(
                "MAX_QUERY_LENGTH must be at least 1 "
                f"(got {self.MAX_QUERY_LENGTH})"
            )
        if self.EMBEDDING_BATCH_SIZE < 1:
            raise ValueError(
                "EMBEDDING_BATCH_SIZE must be at least 1 "
                f"(got {self.EMBEDDING_BATCH_SIZE})"
            )
        if self.RETRIEVAL_TOP_K_DEFAULT < 1:
            raise ValueError(
                "RETRIEVAL_TOP_K_DEFAULT must be at least 1 "
                f"(got {self.RETRIEVAL_TOP_K_DEFAULT})"
            )
        if self.RETRIEVAL_TOP_K_MAX < self.RETRIEVAL_TOP_K_DEFAULT:
            raise ValueError(
                "RETRIEVAL_TOP_K_MAX must be >= RETRIEVAL_TOP_K_DEFAULT "
                f"(got max={self.RETRIEVAL_TOP_K_MAX}, "
                f"default={self.RETRIEVAL_TOP_K_DEFAULT})"
            )
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
        # Phase 11 safety boundary: these two cap how many Ollama calls and
        # search_document executions a single /ask may make. They must stay
        # bounded; raising them would expand the agent's autonomy.
        if not 1 <= self.AGENT_MAX_ITERATIONS <= 3:
            raise ValueError(
                "AGENT_MAX_ITERATIONS must be between 1 and 3 "
                f"(got {self.AGENT_MAX_ITERATIONS})"
            )
        if not 1 <= self.AGENT_MAX_TOOL_CALLS <= 3:
            raise ValueError(
                "AGENT_MAX_TOOL_CALLS must be between 1 and 3 "
                f"(got {self.AGENT_MAX_TOOL_CALLS})"
            )
        if not self.ENVIRONMENT.strip():
            raise ValueError("ENVIRONMENT must be a non-empty string")
        level_value = getattr(logging, self.LOG_LEVEL.upper(), None)
        if not isinstance(level_value, int):
            raise ValueError(
                f"LOG_LEVEL must be a standard logging level "
                f"(got {self.LOG_LEVEL!r})"
            )
        origins = self.allowed_origins_list
        # An empty list is a valid configuration (it disables cross-origin
        # access); what is not valid is a listed origin that is not one.
        for origin in origins:
            if not origin.startswith(("http://", "https://")):
                raise ValueError(
                    "ALLOWED_ORIGINS entries must be absolute http(s) origins "
                    f"(got {origin!r})"
                )
        return self

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]


settings = Settings()
