from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = PROJECT_ROOT if (PROJECT_ROOT / "static").is_dir() else Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_mode: Literal["demo", "api"] = "demo"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    embedding_mode: Literal["hash", "api"] = "hash"
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=8, le=8192)
    data_dir: Path = Path("data")
    api_token: str = ""
    max_upload_mb: int = Field(default=20, ge=1, le=100)
    max_chunks: int = Field(default=5000, ge=1)
    max_extracted_chars: int = Field(default=2_000_000, ge=1000)
    chunk_size: int = Field(default=700, ge=100, le=4000)
    chunk_overlap: int = Field(default=120, ge=0)
    top_k: int = Field(default=4, ge=1, le=12)
    min_score: float = Field(default=0.1, ge=-1, le=1)
    request_timeout: float = Field(default=30, ge=1, le=180)
    max_tool_rounds: int = Field(default=5, ge=1, le=10)
    max_tool_calls: int = Field(default=8, ge=1, le=20)
    seed_demo: bool = True
    tool_files_dir: Path = ASSET_ROOT / "examples" / "files"
    serial_mode: Literal["mock", "real"] = "mock"
    serial_port: str = ""
    serial_baudrate: int = Field(default=115200, gt=0)

    @model_validator(mode="after")
    def validate_configuration(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("CHUNK_OVERLAP must be smaller than CHUNK_SIZE")
        if self.llm_mode == "api" and not self.llm_api_key:
            raise ValueError("LLM_API_KEY is required in api mode")
        if self.embedding_mode == "api" and not self.embedding_api_key:
            raise ValueError("EMBEDDING_API_KEY is required in api mode")
        if self.serial_mode == "real" and not self.serial_port:
            raise ValueError("SERIAL_PORT is required in real mode")
        return self

    @property
    def vector_dimensions(self) -> int:
        return 512 if self.embedding_mode == "hash" else self.embedding_dimensions

    @property
    def embedding_signature(self) -> str:
        if self.embedding_mode == "hash":
            return "hash-ngram-v1:512"
        return f"api:{self.embedding_base_url}:{self.embedding_model}:{self.vector_dimensions}"
