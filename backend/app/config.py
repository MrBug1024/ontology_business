"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root (backend/)
BACKEND_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_ROOT / "data"
SKILLS_DIR = BACKEND_ROOT / "skills"
BUCKETS_DIR = DATA_DIR / "buckets"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_ROOT / ".env"), extra="ignore")

    app_name: str = "Ontology Business Agent Platform"
    app_version: str = "1.0.0"
    debug: bool = True

    # API
    api_prefix: str = "/api"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Storage
    database_url: str = f"sqlite:///{DATA_DIR / 'platform.db'}"

    # OCR service (used by the ocr-parser skill and PDF/image fallback)
    ocr_base_url: str = "https://ocr.rhzy.ai"
    ocr_api_key: str = ""

    # Agent runtime
    max_tool_rounds: int = 20
    max_query_rows: int = 200
    llm_timeout: float = 120.0


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BUCKETS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
