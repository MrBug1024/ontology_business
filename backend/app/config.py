"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

# Project root (backend/)
BACKEND_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = BACKEND_ROOT / "skills"
DEFAULT_POSTGRESQL_DATABASE = "ontology_platform"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_ROOT / ".env"), extra="ignore")

    app_name: str = "Ontology Business Agent Platform"
    app_version: str = "1.0.0"
    debug: bool = False

    # API
    api_prefix: str = "/api"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Storage. The control plane has one supported database backend.
    database_url: str = ""
    database_backend: Literal["postgresql"] = "postgresql"

    postgresql_host: str = ""
    postgresql_port: int = Field(default=5432, ge=1, le=65535)
    postgresql_database: str = ""
    postgresql_user: str = ""
    postgresql_password: str = ""
    postgresql_maintenance_database: str = "postgres"
    postgresql_admin_user: str = ""
    postgresql_admin_password: str = ""

    database_pool_size: int = Field(default=10, ge=1, le=200)
    database_max_overflow: int = Field(default=20, ge=0, le=400)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_statement_timeout_ms: int = Field(default=120_000, ge=1_000, le=3_600_000)
    database_lock_timeout_ms: int = Field(default=10_000, ge=100, le=300_000)

    redis_host: str = ""
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_password: str = ""

    minio_aliyun_endpoint: str = ""
    minio_aliyun_access_key_id: str = ""
    minio_aliyun_access_key_secret: str = ""
    minio_aliyun_file_path: str = ""
    minio_bucketname: str = ""
    # A durable upload intent protects the process-crash window between MinIO
    # PUT and the authoritative PostgreSQL metadata commit. Requests must
    # finish inside this generous lease or fail closed when the worker claims it.
    minio_upload_intent_timeout_seconds: int = Field(
        default=3600, ge=300, le=604800
    )
    minio_late_put_cleanup_grace_seconds: int = Field(
        default=600, ge=300, le=3600
    )

    # OCR service (used by the ocr-parser skill and PDF/image fallback)
    ocr_base_url: str = "https://ocr.rhzy.ai"
    ocr_api_key: str = ""

    # Agent runtime
    # New validation Agents use the capability runtime by default.  Keeping
    # this deployment-controlled preserves an immediate rollback path without
    # rewriting existing Agent rows, whose database default remains legacy.
    new_agent_runtime_binding_mode: Literal["legacy", "capability_only"] = (
        "capability_only"
    )
    max_tool_rounds: int = 20
    max_query_rows: int = 200
    dataset_query_timeout_seconds: float = Field(default=30.0, ge=0.1, le=600.0)
    dataset_query_max_concurrency: int = Field(default=4, ge=1, le=64)
    dataset_duckdb_memory_limit_bytes: int = Field(
        default=512 * 1024 * 1024, ge=64 * 1024 * 1024, le=64 * 1024 * 1024 * 1024
    )
    dataset_duckdb_threads: int = Field(default=2, ge=1, le=32)
    dataset_duckdb_temp_directory: str = ""
    dataset_duckdb_max_temp_directory_bytes: int = Field(
        default=1024 * 1024 * 1024,
        ge=64 * 1024 * 1024,
        le=256 * 1024 * 1024 * 1024,
    )
    dataset_cache_max_object_bytes: int = Field(
        default=1024 * 1024 * 1024,
        ge=1024 * 1024,
        le=256 * 1024 * 1024 * 1024,
    )
    dataset_cache_max_bytes: int = Field(
        default=10 * 1024 * 1024 * 1024,
        ge=64 * 1024 * 1024,
        le=1024 * 1024 * 1024 * 1024,
    )
    dataset_cache_max_age_seconds: int = Field(
        default=7 * 24 * 60 * 60, ge=60, le=365 * 24 * 60 * 60
    )
    llm_timeout: float = 120.0
    scenario_model_llm_timeout: float = 600.0
    # Independent document slices within one staged modelling task are safe to
    # analyse concurrently.  Keep this bounded so one long attachment cannot
    # monopolise the provider or database connection pool.
    scenario_model_max_parallel_chunks: int = Field(default=3, ge=1, le=4)
    # Hard ceiling for one compound document compilation, including malformed
    # JSON retries, timeout fallback chunks and recursive truncation splits.
    # A job that reaches this limit fails closed and is not retried under the
    # same execution fingerprint.
    scenario_model_max_llm_calls: int = 24
    max_upload_bytes: int = 50 * 1024 * 1024
    allow_unsafe_workflow_nodes: bool = False
    # Async workflow inputs are sealed with an externally managed AES-256-GCM
    # key ring before they enter the database.  The JSON object maps stable key
    # ids to URL-safe base64 encoded 32-byte keys; the active id selects the key
    # for new payloads while old ids remain available for decrypting retries.
    # There is deliberately no built-in/default key: enqueue fails closed until
    # the deployment supplies both values.
    workflow_payload_encryption_keys: str = ""
    workflow_payload_active_key_id: str = ""
    # HTTP Action 默认只允许公网 HTTPS 目标。开发环境如确有受控本地模拟端点，
    # 必须由部署配置显式开启，不能由 API 请求覆盖。
    allow_insecure_http_actions: bool = False
    # Tenant-supplied stdio commands execute on the API host, so they remain
    # disabled unless a trusted single-tenant deployment opts in explicitly.
    allow_mcp_stdio: bool = False
    # Remote MCP defaults to public HTTPS.  Controlled development deployments
    # may opt into HTTP and explicitly allow exact/private host names.
    allow_insecure_mcp_http: bool = False
    mcp_private_host_allowlist: str = ""
    mcp_operation_timeout_seconds: float = 90.0
    # Agent publications share one authenticated Streamable HTTP endpoint.
    # Production deployments should set the public URL and exact proxy Host.
    agent_mcp_public_url: str = ""
    agent_mcp_allowed_hosts: str = "localhost,localhost:*,127.0.0.1,127.0.0.1:*,testserver"

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

    @model_validator(mode="after")
    def resolve_database_url(self) -> "Settings":
        explicit_url = self.database_url.strip()
        if explicit_url:
            if make_url(explicit_url).get_backend_name() != "postgresql":
                raise ValueError("平台数据库仅支持 PostgreSQL")
            self.database_url = explicit_url
            return self

        if self.database_backend != "postgresql":
            raise ValueError("平台数据库仅支持 PostgreSQL")
        host = self.postgresql_host.strip()
        database = self.postgresql_database.strip() or DEFAULT_POSTGRESQL_DATABASE
        user = self.postgresql_user.strip()
        password = self.postgresql_password
        missing = [
            name
            for name, value in (
                ("POSTGRESQL_HOST", host),
                ("POSTGRESQL_USER", user),
            )
            if not value
        ]
        if missing:
            raise ValueError("PostgreSQL 配置不完整，缺少：" + ", ".join(missing))
        self.postgresql_database = database
        url = URL.create(
            "postgresql+psycopg",
            username=user,
            password=password,
            host=host,
            port=self.postgresql_port,
            database=database,
        )
        self.database_url = url.render_as_string(hide_password=False)
        return self

    @property
    def redis_configured(self) -> bool:
        return bool(self.redis_host.strip())

    @property
    def minio_configured(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.minio_aliyun_endpoint,
                self.minio_aliyun_access_key_id,
                self.minio_aliyun_access_key_secret,
                self.minio_bucketname,
            )
        )

    @property
    def uses_postgresql_database(self) -> bool:
        return make_url(self.database_url).get_backend_name() == "postgresql"


def ensure_runtime_directories(settings: Settings | None = None) -> None:
    """Create the platform directories required before the API can start.

    Directory creation is intentionally limited to the application-owned
    skill directory. Database and object data are remote services.
    """
    directories = {SKILLS_DIR}

    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    ensure_runtime_directories(settings)
    return settings
