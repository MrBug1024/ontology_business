"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    debug: bool = False

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
    scenario_model_llm_timeout: float = 600.0
    # Hard ceiling for one compound document compilation, including malformed
    # JSON retries, timeout fallback chunks and recursive truncation splits.
    # A job that reaches this limit fails closed and is not retried under the
    # same execution fingerprint.
    scenario_model_max_llm_calls: int = 24
    max_upload_bytes: int = 50 * 1024 * 1024
    allow_unsafe_workflow_nodes: bool = False
    # HTTP Action 默认只允许公网 HTTPS 目标。开发环境如确有受控本地模拟端点，
    # 必须由部署配置显式开启，不能由 API 请求覆盖。
    allow_insecure_http_actions: bool = False

    # A process is deployed to exactly one governed runtime environment.  This
    # value is deliberately server-side: callers cannot select prod/staging by
    # adding a request parameter to an Action or workflow invocation.
    runtime_environment: Literal["dev", "staging", "prod"] = "dev"

    # Authentication / mail
    auth_cookie_name: str = "ontology_session"
    auth_cookie_secure: bool = False
    auth_session_days: int = 7
    verification_code_minutes: int = 10
    mail_username: str = ""
    mail_password: str = ""
    mail_from: str = ""
    mail_port: int = 465
    mail_server: str = ""
    mail_starttls: bool = False
    mail_ssl_tls: bool = True
    mail_use_credentials: bool = True
    mail_timeout_seconds: int = 20


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BUCKETS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
