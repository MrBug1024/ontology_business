"""Migrate the two retained local scenarios to MySQL and MinIO.

The command is deliberately split into four explicit phases::

    python -m scripts.migrate_local_to_services dry-run
    python -m scripts.migrate_local_to_services execute --confirm-execute ...
    python -m scripts.migrate_local_to_services verify
    python -m scripts.migrate_local_to_services cleanup --confirm-cleanup ...

``dry-run`` opens every SQLite database read-only, resolves every file through
its logical bucket boundary, performs a read-only MinIO versioning capability
probe and writes a credential-free manifest.  Later phases refuse to proceed
if the source closure, source table hashes, file hashes, MinIO capability or
the immutable part of that manifest has changed.

The migration contract is intentionally narrow.  It retains exactly the
medical-insurance audit and bookkeeping scenarios identified below, carries
their current ORM dependency closure, and excludes the six abandoned tables,
deprecated medical model rows, retired medical workflows, all other scenarios
and unowned bucket directories.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gc
import hashlib
import json
import math
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence
from urllib.parse import quote as url_quote


MANIFEST_FORMAT_VERSION = 3
MIGRATION_NAME = "local-to-mysql-minio-v1"
SUPERSEDE_MODE_V2_DATETIME6 = "v2-executed-to-v3-datetime6-rebuild"

BOOKKEEPING_SCENARIO_ID = "56e2006148e8499e8599f5c7c8145e60"
MEDICAL_SCENARIO_ID = "cc5d3ff36d2a468596dfa9f8ef2995da"
TARGET_SCENARIO_IDS = (BOOKKEEPING_SCENARIO_ID, MEDICAL_SCENARIO_ID)

BOOKKEEPING_SQL_SOURCE_ID = "68fcb44b941a40d48c7aba1efb14e7f6"
BOOKKEEPING_BUCKET_SOURCE_ID = "7296fec756624e939e813c2253c83482"
MEDICAL_SQL_SOURCE_ID = "a2d20a398ed744e7839acb910f377d6a"
MEDICAL_BUCKET_SOURCE_ID = "76de17773bf24d86891c627dc7981c9b"
TARGET_DATA_SOURCE_IDS = (
    BOOKKEEPING_SQL_SOURCE_ID,
    BOOKKEEPING_BUCKET_SOURCE_ID,
    MEDICAL_SQL_SOURCE_ID,
    MEDICAL_BUCKET_SOURCE_ID,
)

EXPECTED_BUCKET_FILE_COUNT = 41
MYSQL_DATETIME_PRECISION = 6
MINIO_VERSIONING_ENABLED = "Enabled"
MINIO_VERSIONING_SUPPORTED = "Supported"
MINIO_VERSIONING_UNSUPPORTED = "Unsupported"
MINIO_VERSIONING_CAPABILITIES = (
    MINIO_VERSIONING_ENABLED,
    MINIO_VERSIONING_SUPPORTED,
    MINIO_VERSIONING_UNSUPPORTED,
)
MINIO_OBJECT_KEY_STRATEGY = "canonical-bucket-file-id-v1"
BUSINESS_VIEW_TARGET_SEMANTICS = "mysql-fixed-select-unordered-v1"
DEPRECATED_ENTITY_STATUS = "deprecated"
RETIRED_WORKFLOW_MARKER = "[recovery-pack:medical-audit-v2:workflow-retired]"
RETIRED_WORKFLOW_IDS = frozenset(
    {
        "48968bf1066d453898f61e58e30fc904",
        "fdca6ca5b70d4015a8c34002fd108eea",
    }
)

# The installed platform has 64 non-internal SQLite tables.  These six are
# abandoned tables with no current ORM model and are never copied.
EXCLUDED_LEGACY_PLATFORM_TABLES = frozenset(
    {
        "incident_case_history",
        "incident_cases",
        "ontology_advanced_assets",
        "ontology_advanced_records",
        "ontology_advanced_runs",
        "ontology_model_feedback",
    }
)

# Fixed allow-list of the 58 source SQLite tables.  The target also contains
# the new transactional object-deletion outbox, which intentionally starts
# empty because it did not exist in the local source.
SOURCE_PLATFORM_TABLES = (
    "action_execution_logs",
    "agents",
    "artifact_template_versions",
    "artifact_templates",
    "assistant_attachments",
    "assistant_audit_logs",
    "assistant_compilation_jobs",
    "assistant_messages",
    "assistant_proposal_applications",
    "assistant_route_decisions",
    "assistant_threads",
    "auth_sessions",
    "authorization_grants",
    "bucket_files",
    "business_scenarios",
    "connector_bindings",
    "conversations",
    "data_mapping_refresh_jobs",
    "data_mappings",
    "data_sources",
    "document_chunks",
    "document_index_jobs",
    "email_verification_codes",
    "event_envelopes",
    "external_api_key_audit_events",
    "external_api_keys",
    "function_definitions",
    "function_runs",
    "llm_configs",
    "llm_evaluation_records",
    "llm_invocation_traces",
    "mcp_configs",
    "messages",
    "ontology_actions",
    "ontology_branches",
    "ontology_entities",
    "ontology_events",
    "ontology_instances",
    "ontology_properties",
    "ontology_proposals",
    "ontology_relations",
    "ontology_releases",
    "ontology_reviews",
    "ontology_rollbacks",
    "ontology_rules",
    "ontology_snapshots",
    "ontology_workflows",
    "organization_members",
    "organization_roles",
    "organizations",
    "relation_data_mappings",
    "relation_instances",
    "scenario_model_draft_resources",
    "skills",
    "tenants",
    "users",
    "workflow_approval_requests",
    "workflow_runs",
)

PLATFORM_TABLES = (*SOURCE_PLATFORM_TABLES, "object_deletion_jobs")

if len(SOURCE_PLATFORM_TABLES) != 58:  # pragma: no cover - import-time invariant.
    raise RuntimeError("平台源表白名单必须恰好包含 58 张 SQLite 表")
if len(PLATFORM_TABLES) != 59:  # pragma: no cover - import-time invariant.
    raise RuntimeError("平台目标表白名单必须恰好包含 59 张当前 ORM 表")

BOOKKEEPING_TABLES = (
    "accounts",
    "audit_adjustments",
    "audit_papers",
    "audit_projects",
    "audit_reports",
    "audited_statements",
    "communication_records",
    "confirmations",
    "customers",
    "financial_statements",
    "review_records",
    "statement_notes",
    "tax_returns",
    "voucher_lines",
    "vouchers",
)
BOOKKEEPING_VIEWS = ("audit_project_view",)
MEDICAL_TABLES = ("就诊表", "结算表", "规则表", "项目明细表")
MEDICAL_VIEWS = ("医保服务项目视图", "医疗机构视图")

MINIO_BUCKET_FILE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("storage_provider", "VARCHAR(20) NOT NULL DEFAULT 'local'"),
    ("bucket_name", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("object_key", "VARCHAR(2048) NOT NULL DEFAULT ''"),
    ("object_version_id", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("etag", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("object_url", "VARCHAR(4096) NOT NULL DEFAULT ''"),
)

MINIO_ASSISTANT_ATTACHMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("storage_provider", "VARCHAR(20) NOT NULL DEFAULT 'none'"),
    ("bucket_name", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("object_key", "VARCHAR(2048) NOT NULL DEFAULT ''"),
    ("object_version_id", "VARCHAR(255) NOT NULL DEFAULT ''"),
    ("etag", "VARCHAR(128) NOT NULL DEFAULT ''"),
    ("object_url", "VARCHAR(4096) NOT NULL DEFAULT ''"),
)

DECIMAL_QUANTUM = Decimal("0.00000001")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
MYSQL_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
MYSQL_ACCOUNT_HOST_RE = re.compile(r"^[A-Za-z0-9.%_:-]{1,255}$")
RUNTIME_MYSQL_USER_DEFAULT = "ontology_app"
RUNTIME_MYSQL_PRIVILEGES = frozenset(
    {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "CREATE",
        "ALTER",
        "INDEX",
        "REFERENCES",
        "CREATE VIEW",
        "SHOW VIEW",
        "CREATE TEMPORARY TABLES",
    }
)


class MigrationError(RuntimeError):
    """A fail-closed migration contract or verification error."""


@dataclass(frozen=True)
class ScenarioContract:
    id: str
    name: str
    namespace: str
    sql_source_id: str
    bucket_source_id: str
    sqlite_filename: str
    readonly_user_env: str
    readonly_password_env: str
    readonly_user_default: str


SCENARIOS: tuple[ScenarioContract, ...] = (
    ScenarioContract(
        id=BOOKKEEPING_SCENARIO_ID,
        name="代理记账业务",
        namespace="bookkeeping_audit",
        sql_source_id=BOOKKEEPING_SQL_SOURCE_ID,
        bucket_source_id=BOOKKEEPING_BUCKET_SOURCE_ID,
        sqlite_filename="demo_bookkeeping.db",
        readonly_user_env="BOOKKEEPING_MYSQL_USER",
        readonly_password_env="BOOKKEEPING_MYSQL_PASSWORD",
        readonly_user_default="ontology_bookkeeping_ro",
    ),
    ScenarioContract(
        id=MEDICAL_SCENARIO_ID,
        name="医保违规审计",
        namespace="medical_audit",
        sql_source_id=MEDICAL_SQL_SOURCE_ID,
        bucket_source_id=MEDICAL_BUCKET_SOURCE_ID,
        sqlite_filename="yibao_audit.db",
        readonly_user_env="MEDICAL_MYSQL_USER",
        readonly_password_env="MEDICAL_MYSQL_PASSWORD",
        readonly_user_default="ontology_medical_ro",
    ),
)
SCENARIO_BY_ID = {item.id: item for item in SCENARIOS}


@dataclass(frozen=True)
class RuntimePaths:
    backend_root: Path
    data_root: Path
    platform_db: Path
    buckets_root: Path
    manifest_path: Path
    env_file: Path


@dataclass(frozen=True)
class ServiceSettings:
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_admin_user: str
    mysql_admin_password: str
    mysql_runtime_user: str
    mysql_runtime_password: str
    mysql_account_host: str
    readonly_accounts: Mapping[str, tuple[str, str]]
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_prefix: str
    minio_secure: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _q_sqlite(identifier: str) -> str:
    return '"' + str(identifier).replace('"', '""') + '"'


def _q_mysql(identifier: str) -> str:
    return "`" + str(identifier).replace("`", "``") + "`"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(sep=" ", timespec="microseconds")


def _parse_utc_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError as exc:
            raise MigrationError(f"无效时间值：{value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_decimal(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        number = Decimal(str(value)).quantize(DECIMAL_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise MigrationError(f"无法转换为 DECIMAL(30,8) 的值：{value!r}") from exc
    if not number.is_finite():
        raise MigrationError("数值列不能包含 NaN 或 Infinity")
    integer_digits = max(1, len(format(abs(number), "f").split(".", 1)[0].lstrip("0")))
    if integer_digits > 22:
        raise MigrationError(f"数值超出 DECIMAL(30,8) 范围：{value!r}")
    return format(number, ".8f")


def _canonical_value(value: Any, declared_type: str = "") -> Any:
    if value is None:
        return None
    type_name = str(declared_type or "").upper()
    if "JSON" in type_name:
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise MigrationError("平台 JSON 列包含无效 JSON") from exc
        return value
    if "BOOL" in type_name:
        return bool(value)
    if "DATE" in type_name or "TIME" in type_name:
        return _normalize_datetime(value)
    if "INT" in type_name:
        return int(value)
    if any(token in type_name for token in ("REAL", "FLOAT", "DOUBLE")):
        number = float(value)
        if not math.isfinite(number):
            raise MigrationError("浮点列不能包含 NaN 或 Infinity")
        return format(number, ".17g")
    if "DECIMAL" in type_name or "NUMERIC" in type_name:
        return _normalize_decimal(value)
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (datetime, date)):
        return _normalize_datetime(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def _hash_rows(
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
    column_types: Mapping[str, str],
) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        normalized = [
            _canonical_value(row.get(column), column_types.get(column, ""))
            for column in columns
        ]
        digest.update(_canonical_json(normalized).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def _hash_rows_unordered(
    rows: Iterable[Mapping[str, Any]],
    columns: Sequence[str],
    column_types: Mapping[str, str],
) -> tuple[int, str]:
    """Hash a small result set independently of database collation order."""
    encoded: list[bytes] = []
    for row in rows:
        normalized = [
            _canonical_value(row.get(column), column_types.get(column, ""))
            for column in columns
        ]
        encoded.append(_canonical_json(normalized).encode("utf-8"))
    digest = hashlib.sha256()
    for item in sorted(encoded):
        digest.update(item)
        digest.update(b"\n")
    return len(encoded), digest.hexdigest()


def _deep_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key)
            yield from _deep_strings(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _deep_strings(item)


def _json_load(value: Any, *, default: Any) -> Any:
    if value in (None, ""):
        return copy.deepcopy(default)
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise MigrationError("平台 JSON 列包含无效 JSON") from exc
    return copy.deepcopy(value)


def _json_dump(value: Any) -> str:
    return _canonical_json(value)


def _parse_bool(value: str | None, default: bool = True) -> bool:
    if value is None or not str(value).strip():
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise MigrationError(f"布尔配置值无效：{value!r}")


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _required_env(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise MigrationError(f"缺少环境变量 {name}")
    return value


def load_service_settings(env_file: Path) -> ServiceSettings:
    _load_env_file(env_file)
    annual_user = _required_env("ANNUAL_MYSQL_USER")
    annual_password = _required_env("ANNUAL_MYSQL_PASSWORD")
    admin_user_override = str(os.environ.get("MIGRATION_MYSQL_ADMIN_USER") or "").strip()
    admin_password_override = str(
        os.environ.get("MIGRATION_MYSQL_ADMIN_PASSWORD") or ""
    )
    if bool(admin_user_override) != bool(admin_password_override):
        raise MigrationError(
            "MIGRATION_MYSQL_ADMIN_USER/PASSWORD 必须同时提供"
        )
    admin_user = admin_user_override or annual_user
    admin_password = admin_password_override or annual_password
    runtime_user = str(
        os.environ.get("MIGRATION_MYSQL_APP_USER") or RUNTIME_MYSQL_USER_DEFAULT
    ).strip()
    if not MYSQL_ACCOUNT_RE.fullmatch(runtime_user):
        raise MigrationError("MIGRATION_MYSQL_APP_USER 格式无效")
    account_host = str(
        os.environ.get("MIGRATION_MYSQL_ACCOUNT_HOST") or "%"
    ).strip()
    if not MYSQL_ACCOUNT_HOST_RE.fullmatch(account_host):
        raise MigrationError("MIGRATION_MYSQL_ACCOUNT_HOST 格式无效")
    runtime_password = (
        annual_password
        if annual_user == runtime_user
        else secrets.token_urlsafe(32)
    )
    if len(runtime_password) < 16:
        raise MigrationError("MySQL 运行账号密码至少需要 16 个字符")
    accounts: dict[str, tuple[str, str]] = {}
    for scenario in SCENARIOS:
        username = str(
            os.environ.get(scenario.readonly_user_env) or scenario.readonly_user_default
        ).strip()
        if not MYSQL_ACCOUNT_RE.fullmatch(username):
            raise MigrationError(
                f"{scenario.readonly_user_env} 必须是 1-32 位字母、数字或下划线"
            )
        password = str(os.environ.get(scenario.readonly_password_env) or "")
        if not password:
            # Generated credentials live only in process memory, the dedicated
            # MySQL account and the migrated DataSource.config.  They are never
            # written to the manifest or emitted to stdout/stderr.
            password = secrets.token_urlsafe(32)
        if len(password) < 16:
            raise MigrationError(
                f"{scenario.readonly_password_env} 至少需要 16 个字符，不能使用示例弱口令"
            )
        accounts[scenario.id] = (username, password)
    usernames = [username for username, _password in accounts.values()]
    if len(set(usernames)) != len(usernames):
        raise MigrationError("两个业务场景必须使用不同的 MySQL 只读账号")
    if runtime_user in usernames:
        raise MigrationError("MySQL 运行账号不能与场景只读账号复用")
    try:
        mysql_port = int(str(os.environ.get("ANNUAL_MYSQL_PORT") or "3306"))
    except ValueError as exc:
        raise MigrationError("ANNUAL_MYSQL_PORT 必须是整数") from exc
    if not 1 <= mysql_port <= 65535:
        raise MigrationError("ANNUAL_MYSQL_PORT 超出有效范围")
    prefix = str(os.environ.get("MINIO_ALIYUN_FILE_PATH") or "").strip().strip("/")
    return ServiceSettings(
        mysql_host=_required_env("ANNUAL_MYSQL_HOST"),
        mysql_port=mysql_port,
        mysql_database=_required_env("ANNUAL_MYSQL_DATABASE"),
        mysql_admin_user=admin_user,
        mysql_admin_password=admin_password,
        mysql_runtime_user=runtime_user,
        mysql_runtime_password=runtime_password,
        mysql_account_host=account_host,
        readonly_accounts=accounts,
        minio_endpoint=_required_env("MINIO_ALIYUN_ENDPOINT")
        .removeprefix("https://")
        .removeprefix("http://")
        .rstrip("/"),
        minio_access_key=_required_env("MINIO_ALIYUN_ACCESS_KEY_ID"),
        minio_secret_key=_required_env("MINIO_ALIYUN_ACCESS_KEY_SECRET"),
        minio_bucket=_required_env("MINIO_BUCKETNAME"),
        minio_prefix=prefix,
        minio_secure=_parse_bool(os.environ.get("MINIO_ALIYUN_SECURE"), default=True),
    )


@contextmanager
def open_sqlite_readonly(path: Path) -> Iterator[sqlite3.Connection]:
    resolved = path.resolve(strict=True)
    uri = "file:" + url_quote(resolved.as_posix(), safe="/: ") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _view_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='view'"
        )
    }


def _table_info(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "name": str(row[1]),
            "type": str(row[2] or ""),
            "notnull": bool(row[3]),
            "default": row[4],
            "pk_order": int(row[5] or 0),
        }
        for row in connection.execute(f"PRAGMA table_info({_q_sqlite(table)})")
    ]


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        {
            "id": int(row[0]),
            "seq": int(row[1]),
            "parent_table": str(row[2]),
            "child_column": str(row[3]),
            "parent_column": str(row[4]),
            "on_update": str(row[5]),
            "on_delete": str(row[6]),
        }
        for row in connection.execute(f"PRAGMA foreign_key_list({_q_sqlite(table)})")
    ]


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _select_rows(
    connection: sqlite3.Connection,
    table: str,
    where: str = "",
    parameters: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM {_q_sqlite(table)}"
    if where:
        sql += " WHERE " + where
    info = _table_info(connection, table)
    pk = [item["name"] for item in sorted(info, key=lambda item: item["pk_order"]) if item["pk_order"]]
    order = pk or ["rowid"]
    sql += " ORDER BY " + ", ".join(_q_sqlite(item) if item != "rowid" else "rowid" for item in order)
    return [_row_dict(row) for row in connection.execute(sql, tuple(parameters))]


def _where_in(column: str, values: Sequence[Any]) -> tuple[str, tuple[Any, ...]]:
    if not values:
        return "1=0", ()
    return (
        f"{_q_sqlite(column)} IN ({','.join('?' for _ in values)})",
        tuple(values),
    )


def _primary_key_tuple(row: Mapping[str, Any], pk_columns: Sequence[str]) -> tuple[Any, ...]:
    return tuple(row.get(column) for column in pk_columns)


def _add_rows(
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
    schemas: Mapping[str, Mapping[str, Any]],
    table: str,
    rows: Iterable[Mapping[str, Any]],
) -> int:
    pk_columns = list(schemas[table]["pk_columns"])
    if not pk_columns:
        raise MigrationError(f"平台表 {table} 缺少主键，无法构造稳定依赖闭包")
    added = 0
    bucket = selected.setdefault(table, {})
    for raw in rows:
        row = dict(raw)
        key = _primary_key_tuple(row, pk_columns)
        if any(value is None for value in key):
            raise MigrationError(f"平台表 {table} 存在空主键")
        if key not in bucket:
            bucket[key] = row
            added += 1
    return added


def _remove_rows(
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
    table: str,
    predicate,
) -> None:
    selected[table] = {
        key: row for key, row in selected.get(table, {}).items() if predicate(row)
    }


def _ids(selected: Mapping[str, Mapping[tuple[Any, ...], Mapping[str, Any]]], table: str) -> set[str]:
    return {
        str(row.get("id") or row.get("proposal_id") or "")
        for row in selected.get(table, {}).values()
        if str(row.get("id") or row.get("proposal_id") or "")
    }


def _is_retired_workflow(row: Mapping[str, Any]) -> bool:
    return (
        str(row.get("id") or "") in RETIRED_WORKFLOW_IDS
        or RETIRED_WORKFLOW_MARKER in str(row.get("description") or "")
    )


OWNED_CHILD_EDGES: tuple[tuple[str, str, str], ...] = (
    ("ontology_entities", "ontology_properties", "entity_id"),
    ("data_sources", "bucket_files", "data_source_id"),
    ("data_sources", "document_chunks", "data_source_id"),
    ("data_sources", "document_index_jobs", "data_source_id"),
    ("bucket_files", "document_chunks", "bucket_file_id"),
    ("bucket_files", "document_index_jobs", "bucket_file_id"),
    ("artifact_templates", "artifact_template_versions", "template_id"),
    ("agents", "conversations", "agent_id"),
    ("conversations", "messages", "conversation_id"),
    ("assistant_threads", "assistant_messages", "thread_id"),
    ("assistant_threads", "assistant_attachments", "thread_id"),
    ("assistant_threads", "assistant_proposal_applications", "thread_id"),
    ("assistant_messages", "assistant_proposal_applications", "message_id"),
    ("ontology_proposals", "ontology_reviews", "proposal_id"),
    ("workflow_runs", "workflow_approval_requests", "workflow_run_id"),
    ("external_api_keys", "external_api_key_audit_events", "api_key_id"),
    ("llm_configs", "llm_evaluation_records", "llm_config_id"),
)


def _schema_for_table(connection: sqlite3.Connection, table: str) -> dict[str, Any]:
    columns = _table_info(connection, table)
    pk_columns = [
        item["name"]
        for item in sorted(columns, key=lambda item: item["pk_order"])
        if item["pk_order"]
    ]
    sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return {
        "columns": columns,
        "column_names": [item["name"] for item in columns],
        "column_types": {item["name"]: item["type"] for item in columns},
        "json_columns": [
            item["name"] for item in columns if "JSON" in item["type"].upper()
        ],
        "pk_columns": pk_columns,
        "foreign_keys": _foreign_keys(connection, table),
        "ddl_sha256": hashlib.sha256(str(sql_row[0] or "").encode("utf-8")).hexdigest()
        if sql_row
        else "",
    }


def _query_children(
    connection: sqlite3.Connection,
    schemas: Mapping[str, Mapping[str, Any]],
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
    parent_table: str,
    child_table: str,
    child_column: str,
) -> int:
    if child_table not in schemas:
        return 0
    parent_ids = sorted(_ids(selected, parent_table))
    if not parent_ids:
        return 0
    where, parameters = _where_in(child_column, parent_ids)
    rows = _select_rows(connection, child_table, where, parameters)
    # A child carrying scenario_id may only be admitted when it belongs to one
    # of the two retained scenarios.  Shared parents (notably users) can never
    # pull another scenario into this closure.
    rows = [
        row
        for row in rows
        if "scenario_id" not in row
        or row.get("scenario_id") in (None, "", *TARGET_SCENARIO_IDS)
    ]
    return _add_rows(selected, schemas, child_table, rows)


def _query_by_ids(
    connection: sqlite3.Connection,
    schemas: Mapping[str, Mapping[str, Any]],
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
    table: str,
    values: Iterable[str],
    *,
    column: str = "id",
) -> int:
    resolved = sorted({str(value) for value in values if str(value)})
    if not resolved or table not in schemas:
        return 0
    where, parameters = _where_in(column, resolved)
    return _add_rows(
        selected,
        schemas,
        table,
        _select_rows(connection, table, where, parameters),
    )


def _referenced_hex_ids(
    selected: Mapping[str, Mapping[tuple[Any, ...], Mapping[str, Any]]],
    schemas: Mapping[str, Mapping[str, Any]],
) -> set[str]:
    values: set[str] = set()
    for table, rows in selected.items():
        for row in rows.values():
            for column in schemas[table]["json_columns"]:
                payload = _json_load(row.get(column), default={})
                for text in _deep_strings(payload):
                    candidate = text.strip().lower()
                    if HEX32_RE.fullmatch(candidate):
                        values.add(candidate)
    return values


def _semantic_filter_platform_rows(
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
    *,
    snapshot_time: datetime,
) -> dict[str, set[str]]:
    # Verification codes are one-time transient secrets and are never useful
    # after cutover.  Sessions remain valid only when they had not expired at
    # the immutable dry-run snapshot time.
    selected["email_verification_codes"] = {}
    _remove_rows(
        selected,
        "auth_sessions",
        lambda row: _parse_utc_datetime(row.get("expires_at")) > snapshot_time,
    )
    _remove_rows(
        selected,
        "ontology_entities",
        lambda row: str(row.get("lifecycle_status") or "active")
        != DEPRECATED_ENTITY_STATUS,
    )
    active_entities = _ids(selected, "ontology_entities")

    _remove_rows(
        selected,
        "ontology_relations",
        lambda row: str(row.get("source_entity_id") or "") in active_entities
        and str(row.get("target_entity_id") or "") in active_entities,
    )
    active_relations = _ids(selected, "ontology_relations")
    _remove_rows(
        selected,
        "ontology_instances",
        lambda row: str(row.get("entity_id") or "") in active_entities,
    )
    active_instances = _ids(selected, "ontology_instances")
    _remove_rows(
        selected,
        "data_mappings",
        lambda row: str(row.get("entity_id") or "") in active_entities,
    )
    active_mappings = _ids(selected, "data_mappings")
    _remove_rows(
        selected,
        "relation_data_mappings",
        lambda row: str(row.get("relation_id") or "") in active_relations
        and str(row.get("source_mapping_id") or "") in active_mappings
        and str(row.get("target_mapping_id") or "") in active_mappings,
    )
    _remove_rows(
        selected,
        "relation_instances",
        lambda row: str(row.get("relation_id") or "") in active_relations
        and str(row.get("source_instance_id") or "") in active_instances
        and str(row.get("target_instance_id") or "") in active_instances,
    )
    for table in ("ontology_actions", "ontology_rules"):
        _remove_rows(
            selected,
            table,
            lambda row: not row.get("entity_id")
            or str(row.get("entity_id")) in active_entities,
        )

    _remove_rows(selected, "ontology_workflows", lambda row: not _is_retired_workflow(row))
    active_workflows = _ids(selected, "ontology_workflows")
    _remove_rows(
        selected,
        "workflow_runs",
        lambda row: str(row.get("workflow_id") or "") in active_workflows,
    )
    active_runs = _ids(selected, "workflow_runs")
    _remove_rows(
        selected,
        "workflow_approval_requests",
        lambda row: str(row.get("workflow_run_id") or "") in active_runs,
    )
    active_actions = _ids(selected, "ontology_actions")
    active_rules = _ids(selected, "ontology_rules")

    def execution_log_is_retained(row: Mapping[str, Any]) -> bool:
        target_type = str(row.get("target_type") or "")
        target_id = str(row.get("target_id") or "")
        if target_type == "workflow":
            return target_id in active_workflows
        if target_type == "action":
            return target_id in active_actions
        if target_type == "rule":
            return target_id in active_rules
        return True

    _remove_rows(selected, "action_execution_logs", execution_log_is_retained)
    _remove_rows(
        selected,
        "data_mapping_refresh_jobs",
        lambda row: str(row.get("mapping_id") or "") in active_mappings,
    )
    active_functions = _ids(selected, "function_definitions")
    _remove_rows(
        selected,
        "function_runs",
        lambda row: not row.get("function_id")
        or str(row.get("function_id")) in active_functions,
    )
    active_events = _ids(selected, "ontology_events")
    _remove_rows(
        selected,
        "event_envelopes",
        lambda row: str(row.get("event_id") or "") in active_events,
    )
    return {
        "entities": active_entities,
        "relations": active_relations,
        "instances": active_instances,
        "mappings": active_mappings,
        "workflows": active_workflows,
        "actions": active_actions,
        "rules": active_rules,
        "runs": active_runs,
    }


def _add_identity_infrastructure(
    connection: sqlite3.Connection,
    schemas: Mapping[str, Mapping[str, Any]],
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
) -> None:
    tenant_ids = {
        str(row.get("tenant_id"))
        for row in selected.get("business_scenarios", {}).values()
        if row.get("tenant_id")
    }
    _query_by_ids(connection, schemas, selected, "tenants", tenant_ids)
    if not tenant_ids:
        raise MigrationError("两个目标场景都缺少 tenant_id")
    tenant_where, tenant_parameters = _where_in("tenant_id", sorted(tenant_ids))
    for table in ("users", "organizations"):
        _add_rows(
            selected,
            schemas,
            table,
            _select_rows(connection, table, tenant_where, tenant_parameters),
        )

    organization_ids = _ids(selected, "organizations")
    if organization_ids:
        where, parameters = _where_in("organization_id", sorted(organization_ids))
        for table in ("organization_roles", "organization_members"):
            _add_rows(
                selected,
                schemas,
                table,
                _select_rows(connection, table, where, parameters),
            )
    user_ids = _ids(selected, "users")
    if user_ids:
        where, parameters = _where_in("user_id", sorted(user_ids))
        for table in ("auth_sessions", "email_verification_codes", "external_api_keys"):
            _add_rows(
                selected,
                schemas,
                table,
                _select_rows(connection, table, where, parameters),
            )

    key_ids = _ids(selected, "external_api_keys")
    if key_ids:
        where, parameters = _where_in("api_key_id", sorted(key_ids))
        _add_rows(
            selected,
            schemas,
            "external_api_key_audit_events",
            _select_rows(connection, "external_api_key_audit_events", where, parameters),
        )

    # Authorization grants have a polymorphic resource_id rather than an FK.
    # Keep only wildcard or retained-resource grants, never grants into a
    # discarded scenario/model.
    retained_resource_ids = {"*", *TARGET_SCENARIO_IDS}
    for rows in selected.values():
        retained_resource_ids.update(
            str(row.get("id") or row.get("proposal_id") or "")
            for row in rows.values()
            if row.get("id") or row.get("proposal_id")
        )
    if organization_ids:
        where, parameters = _where_in("organization_id", sorted(organization_ids))
        grants = _select_rows(connection, "authorization_grants", where, parameters)
        grants = [
            row
            for row in grants
            if str(row.get("resource_id") or "") in retained_resource_ids
        ]
        _add_rows(selected, schemas, "authorization_grants", grants)


def _expand_owned_and_parent_closure(
    connection: sqlite3.Connection,
    schemas: Mapping[str, Mapping[str, Any]],
    selected: MutableMapping[str, dict[tuple[Any, ...], dict[str, Any]]],
) -> None:
    for _round in range(20):
        added = 0
        for parent, child, child_column in OWNED_CHILD_EDGES:
            added += _query_children(
                connection,
                schemas,
                selected,
                parent,
                child,
                child_column,
            )

        # Every selected FK parent is required even when it is a shared user,
        # tenant, LLM config or immutable governance snapshot.
        for child_table, rows in list(selected.items()):
            if not rows:
                continue
            grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
            for foreign_key in schemas[child_table]["foreign_keys"]:
                parent_table = str(foreign_key["parent_table"])
                if parent_table not in schemas:
                    continue
                child_column = str(foreign_key["child_column"])
                parent_column = str(foreign_key["parent_column"])
                for row in rows.values():
                    value = row.get(child_column)
                    if value not in (None, ""):
                        grouped[(parent_table, parent_column)].add(str(value))
            for (parent_table, parent_column), values in grouped.items():
                added += _query_by_ids(
                    connection,
                    schemas,
                    selected,
                    parent_table,
                    values,
                    column=parent_column,
                )
        if not added:
            break
    else:  # pragma: no cover - bounded graph should converge quickly.
        raise MigrationError("平台依赖闭包在 20 轮内未收敛")


def _validate_platform_closure(
    schemas: Mapping[str, Mapping[str, Any]],
    selected: Mapping[str, Mapping[tuple[Any, ...], Mapping[str, Any]]],
) -> None:
    scenario_ids = _ids(selected, "business_scenarios")
    if scenario_ids != set(TARGET_SCENARIO_IDS):
        raise MigrationError(
            f"场景闭包不精确，预期 {sorted(TARGET_SCENARIO_IDS)}，实际 {sorted(scenario_ids)}"
        )
    for table, rows in selected.items():
        for row in rows.values():
            scenario_id = row.get("scenario_id")
            if scenario_id not in (None, "", *TARGET_SCENARIO_IDS):
                raise MigrationError(f"{table} 闭包意外包含其他场景 {scenario_id}")

    deprecated = [
        row
        for row in selected.get("ontology_entities", {}).values()
        if str(row.get("lifecycle_status") or "") == DEPRECATED_ENTITY_STATUS
    ]
    if deprecated:
        raise MigrationError("平台闭包仍包含 deprecated 实体")
    if any(_is_retired_workflow(row) for row in selected.get("ontology_workflows", {}).values()):
        raise MigrationError("平台闭包仍包含 retired workflow")

    actual_sources = _ids(selected, "data_sources")
    if actual_sources != set(TARGET_DATA_SOURCE_IDS):
        raise MigrationError(
            f"目标数据源闭包不精确：{sorted(actual_sources)}"
        )
    if len(selected.get("bucket_files", {})) != EXPECTED_BUCKET_FILE_COUNT:
        raise MigrationError(
            f"目标 BucketFile 应为 {EXPECTED_BUCKET_FILE_COUNT} 条，实际为 "
            f"{len(selected.get('bucket_files', {}))} 条"
        )

    # Verify every selected FK either has its selected parent or is nullable and
    # null.  This is independent of SQLite's source-wide FK state.
    for child_table, rows in selected.items():
        for foreign_key in schemas[child_table]["foreign_keys"]:
            parent_table = str(foreign_key["parent_table"])
            if parent_table not in schemas:
                continue
            child_column = str(foreign_key["child_column"])
            parent_column = str(foreign_key["parent_column"])
            parent_values = {
                str(parent.get(parent_column))
                for parent in selected.get(parent_table, {}).values()
            }
            for row in rows.values():
                value = row.get(child_column)
                if value not in (None, "") and str(value) not in parent_values:
                    raise MigrationError(
                        f"闭包 FK 缺失：{child_table}.{child_column}={value} -> "
                        f"{parent_table}.{parent_column}"
                    )

    # Immutable snapshots cannot be silently rewritten.  A target installation
    # that later gains such a snapshot must be reviewed and re-snapshotted.
    excluded_ids = set(RETIRED_WORKFLOW_IDS)
    for table in ("ontology_snapshots", "ontology_releases", "ontology_rollbacks"):
        for row in selected.get(table, {}).values():
            for column in schemas[table]["json_columns"]:
                strings = set(_deep_strings(_json_load(row.get(column), default={})))
                if strings & excluded_ids:
                    raise MigrationError(
                        f"不可变治理记录 {table}:{row.get('id')} 引用了 retired workflow"
                    )


def _source_snapshot_schema_for_empty_target_table(
    schema: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep target-only empty-table source fingerprints stable across v2/v3."""
    stable = copy.deepcopy(dict(schema))
    for column in stable.get("columns", []):
        declared = str(column.get("type") or "")
        if declared.upper().startswith(("DATETIME(", "TIMESTAMP(")):
            column["type"] = declared.split("(", 1)[0]
    for column, declared_value in list(stable.get("column_types", {}).items()):
        declared = str(declared_value)
        if declared.upper().startswith(("DATETIME(", "TIMESTAMP(")):
            stable["column_types"][column] = declared.split("(", 1)[0]
    stable.pop("datetime_precisions", None)
    return stable


def collect_platform_snapshot(
    platform_db: Path,
    *,
    expected_tables: Sequence[str] = SOURCE_PLATFORM_TABLES,
    strict_contract: bool = True,
    snapshot_time: str | datetime | None = None,
) -> dict[str, Any]:
    cutoff = (
        _parse_utc_datetime(snapshot_time)
        if snapshot_time is not None
        else datetime.now(timezone.utc)
    )
    with open_sqlite_readonly(platform_db) as connection:
        available = _table_names(connection)
        expected = set(expected_tables)
        missing = expected - available
        unexpected = available - expected - EXCLUDED_LEGACY_PLATFORM_TABLES
        if missing:
            raise MigrationError(f"platform.db 缺少当前 ORM 表：{sorted(missing)}")
        if strict_contract and unexpected:
            raise MigrationError(
                "platform.db 出现未审计的新表，请先更新迁移白名单："
                + ", ".join(sorted(unexpected))
            )
        schemas = {
            table: _schema_for_table(connection, table) for table in expected_tables
        }
        selected: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {
            table: {} for table in expected_tables
        }
        target_only_tables = set(PLATFORM_TABLES) - set(expected_tables)
        if target_only_tables != {"object_deletion_jobs"}:
            raise MigrationError(
                "源/目标平台表差异不符合仅新增空 outbox 的迁移契约"
            )
        target_schemas = _platform_target_schemas({})
        for table in sorted(target_only_tables):
            # This table has no SQLite rows or DDL.  Its source-side fingerprint
            # must stay equal to the v2 manifest; DATETIME(6) is a target DDL
            # concern and is captured separately in the v3 expected schema.
            schemas[table] = _source_snapshot_schema_for_empty_target_table(
                target_schemas[table]
            )
            selected[table] = {}

        scenario_where, scenario_parameters = _where_in("id", TARGET_SCENARIO_IDS)
        _add_rows(
            selected,
            schemas,
            "business_scenarios",
            _select_rows(
                connection,
                "business_scenarios",
                scenario_where,
                scenario_parameters,
            ),
        )
        for table in expected_tables:
            if table == "business_scenarios":
                continue
            if "scenario_id" not in schemas[table]["column_names"]:
                continue
            where, parameters = _where_in("scenario_id", TARGET_SCENARIO_IDS)
            _add_rows(
                selected,
                schemas,
                table,
                _select_rows(connection, table, where, parameters),
            )

        active = _semantic_filter_platform_rows(selected, snapshot_time=cutoff)

        # Rows whose current schema lacks a scenario FK still carry strong
        # pseudo-links to the retained Agent/conversation/thread.
        agent_ids = sorted(_ids(selected, "agents"))
        conversation_rows: list[dict[str, Any]] = []
        if agent_ids:
            where, parameters = _where_in("agent_id", agent_ids)
            conversation_rows = _select_rows(connection, "conversations", where, parameters)
            _add_rows(selected, schemas, "conversations", conversation_rows)
        conversation_ids = sorted(_ids(selected, "conversations"))
        trace_predicates: list[str] = []
        trace_parameters: list[Any] = []
        if agent_ids:
            clause, values = _where_in("agent_id", agent_ids)
            trace_predicates.append(clause)
            trace_parameters.extend(values)
        if conversation_ids:
            clause, values = _where_in("conversation_id", conversation_ids)
            trace_predicates.append(clause)
            trace_parameters.extend(values)
        if trace_predicates:
            _add_rows(
                selected,
                schemas,
                "llm_invocation_traces",
                _select_rows(
                    connection,
                    "llm_invocation_traces",
                    " OR ".join(f"({item})" for item in trace_predicates),
                    trace_parameters,
                ),
            )

        thread_ids = sorted(_ids(selected, "assistant_threads"))
        if thread_ids:
            for table in (
                "assistant_route_decisions",
                "assistant_audit_logs",
                "assistant_compilation_jobs",
            ):
                if "thread_id" not in schemas[table]["column_names"]:
                    continue
                where, parameters = _where_in("thread_id", thread_ids)
                _add_rows(
                    selected,
                    schemas,
                    table,
                    _select_rows(connection, table, where, parameters),
                )

        _add_identity_infrastructure(connection, schemas, selected)
        _expand_owned_and_parent_closure(connection, schemas, selected)

        # Agent JSON lists and runtime configs are polymorphic references.
        referenced_ids = _referenced_hex_ids(selected, schemas)
        for table in ("skills", "mcp_configs", "llm_configs"):
            _query_by_ids(connection, schemas, selected, table, referenced_ids)
        _expand_owned_and_parent_closure(connection, schemas, selected)

        # Re-apply semantic filters after closure to guard against a malformed
        # parent link reintroducing an explicitly excluded definition.
        _semantic_filter_platform_rows(selected, snapshot_time=cutoff)
        _validate_platform_closure(schemas, selected)

        table_manifest: dict[str, Any] = {}
        for table in PLATFORM_TABLES:
            rows = list(selected[table].values())
            pk_columns = list(schemas[table]["pk_columns"])
            rows.sort(key=lambda row: tuple(str(row.get(column) or "") for column in pk_columns))
            count, row_hash = _hash_rows(
                rows,
                schemas[table]["column_names"],
                schemas[table]["column_types"],
            )
            table_manifest[table] = {
                "row_count": count,
                "row_sha256": row_hash,
                "pk_columns": pk_columns,
                "primary_keys": [
                    list(_primary_key_tuple(row, pk_columns)) for row in rows
                ],
                "ddl_sha256": schemas[table].get("ddl_sha256", ""),
            }

        return {
            "path": str(platform_db.resolve()),
            "snapshot_time": cutoff.isoformat(),
            "tables": table_manifest,
            "schema_sha256": _sha256_json(
                {
                    table: {
                        "columns": schemas[table]["columns"],
                        "foreign_keys": schemas[table]["foreign_keys"],
                    }
                    for table in PLATFORM_TABLES
                }
            ),
            "excluded_legacy_tables_present": sorted(
                available & EXCLUDED_LEGACY_PLATFORM_TABLES
            ),
            "_schemas": schemas,
            "_rows": selected,
            "_active": active,
        }


MEDICAL_INDEX_SPECS: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "项目明细表": (
        ("ix_medical_detail_catalog_facility", ("医保目录名称", "定点医药机构名称")),
        (
            "ix_medical_detail_encounter_catalog_facility",
            ("就诊ID", "医保目录名称", "定点医药机构名称"),
        ),
        ("ix_medical_detail_encounter", ("就诊ID",)),
        ("ix_medical_detail_catalog_code", ("医保目录编码",)),
    ),
    "就诊表": (
        ("ix_medical_encounter_id", ("就诊ID",)),
        ("ix_medical_encounter_facility_name", ("定点医药机构名称",)),
        (
            "ix_medical_encounter_id_facility",
            ("就诊ID", "定点医药机构名称"),
        ),
    ),
    "结算表": (("ix_medical_settlement_encounter", ("就诊ID",)),),
}

BOOKKEEPING_INDEX_SPECS: Mapping[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "audit_adjustments": (("ix_audit_adjustments_project", ("project_id",)),),
    "audit_papers": (("ix_audit_papers_project", ("project_id",)),),
    "audit_reports": (("ix_audit_reports_project", ("project_id",)),),
    "audited_statements": (("ix_audited_statements_project", ("project_id",)),),
    "communication_records": (("ix_communications_customer", ("customer_id",)),),
    "confirmations": (("ix_confirmations_project", ("project_id",)),),
    "financial_statements": (("ix_financial_statements_customer", ("customer_id",)),),
    "review_records": (("ix_review_records_project", ("project_id",)),),
    "statement_notes": (("ix_statement_notes_project", ("project_id",)),),
    "tax_returns": (("ix_tax_returns_customer", ("customer_id",)),),
    "voucher_lines": (
        ("ix_voucher_lines_voucher", ("voucher_id",)),
        ("ix_voucher_lines_account", ("account_code",)),
    ),
    "vouchers": (("ix_vouchers_customer", ("customer_id",)),),
}

MEDICAL_NUMERIC_COLUMNS = frozenset(
    {
        "年龄",
        "住院天数",
        "孕周数",
        "胎次",
        "胎儿数",
        "第三方赔付比例",
        "数量",
        "单价",
        "明细项目费用总额",
        "定价上限金额",
        "自付比例",
        "全自费金额",
        "超限价自费费用",
        "先行自付金额",
        "符合范围金额",
        "公务员床位费金额",
        "医院减免金额",
        "报销比例",
        "周期天数",
        "医疗费总额",
        "起付标准",
        "本次起付线",
        "实际支付起付线",
        "统筹基金支出",
        "基本医疗统筹支付比例",
        "医保认可费用总额",
        "公务员医疗补助资金支出",
        "补充医疗保险基金支出",
        "大病补充医疗保险基金支出",
        "大额医疗补助基金支出",
        "伤残人员医疗保障基金支出",
        "医疗救助基金支出",
        "其它基金支付",
        "基金支付总额",
        "个人支付金额",
        "个人账户支出",
        "现金支付金额",
        "自费中医院负担部分",
        "余额",
        "账户共济支付金额",
        "按病种结算支付金额",
        "除外项目基金支付金额",
    }
)

MEDICAL_VARCHAR_NAME_RE = re.compile(
    r"(ID|编号|编码|名称|姓名|类型|类别|标志|状态|等级|年度|日期|时间|"
    r"科室|医师|医生|床位|方式|关系|行业|民族|性别|文号|流水号)$",
    re.IGNORECASE,
)

BOOKKEEPING_VARCHAR_NAME_RE = re.compile(
    r"(_id|_code|_no|_type|_status|_date|_period|_year|_name)$",
    re.IGNORECASE,
)


MEDICAL_VIEW_DDL: Mapping[str, str] = {
    "医疗机构视图": """
CREATE OR REPLACE VIEW `医疗机构视图` AS
SELECT
    CAST(`定点医药机构编号` AS CHAR(191)) AS `定点医药机构编号`,
    MAX(CAST(`定点医药机构名称` AS CHAR(191))) AS `定点医药机构名称`,
    MAX(CAST(`医院等级` AS CHAR(191))) AS `医院等级`,
    MAX(CAST(`定点归属医保区划` AS CHAR(191))) AS `定点归属医保区划`
FROM `就诊表`
WHERE `定点医药机构编号` IS NOT NULL
  AND TRIM(CAST(`定点医药机构编号` AS CHAR)) <> ''
GROUP BY CAST(`定点医药机构编号` AS CHAR(191))
""".strip(),
    "医保服务项目视图": """
CREATE OR REPLACE VIEW `医保服务项目视图` AS
SELECT
    CAST(`医保目录编码` AS CHAR(191)) AS `医保目录编码`,
    MAX(CAST(`医保目录名称` AS CHAR(191))) AS `医保目录名称`,
    MAX(CAST(`目录类别` AS CHAR(191))) AS `目录类别`,
    MAX(CAST(`医疗收费项目类别` AS CHAR(191))) AS `医疗收费项目类别`,
    MAX(CAST(`规格` AS CHAR(191))) AS `规格`,
    MAX(CAST(NULLIF(TRIM(CAST(`单价` AS CHAR)), '') AS DECIMAL(30,8))) AS `参考单价`
FROM `项目明细表`
WHERE `医保目录编码` IS NOT NULL
  AND TRIM(CAST(`医保目录编码` AS CHAR)) <> ''
GROUP BY CAST(`医保目录编码` AS CHAR(191))
""".strip(),
}

BOOKKEEPING_VIEW_DDL: Mapping[str, str] = {
    "audit_project_view": """
CREATE OR REPLACE VIEW `audit_project_view` AS
SELECT p.*, CAST(c.`company_name` AS CHAR) AS `company_name`
FROM `audit_projects` AS p
LEFT JOIN `customers` AS c ON c.`customer_id` = p.`customer_id`
""".strip()
}


def _fixed_target_view_ddl(view: str) -> str:
    if view in MEDICAL_VIEW_DDL:
        return MEDICAL_VIEW_DDL[view]
    if view in BOOKKEEPING_VIEW_DDL:
        return BOOKKEEPING_VIEW_DDL[view]
    raise MigrationError(f"没有固定 MySQL 视图定义：{view}")


def _fixed_target_view_select(view: str) -> str:
    ddl = _fixed_target_view_ddl(view)
    prefix = f"CREATE OR REPLACE VIEW {_q_mysql(view)} AS\n"
    if not ddl.startswith(prefix):
        raise MigrationError(f"固定 MySQL 视图定义无法提取 SELECT：{view}")
    select_sql = ddl[len(prefix) :].strip()
    if re.match(r"^SELECT(?=\s)", select_sql, flags=re.IGNORECASE) is None:
        raise MigrationError(f"固定 MySQL 视图定义不是 SELECT：{view}")
    return select_sql


def _mapped_medical_column_hints(platform: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    rows = platform["_rows"]
    property_types: dict[tuple[str, str], str] = {}
    for prop in rows.get("ontology_properties", {}).values():
        property_types[(str(prop.get("entity_id")), str(prop.get("name")))] = str(
            prop.get("data_type") or "string"
        ).lower()
    hints: dict[str, dict[str, str]] = defaultdict(dict)
    view_to_table = {
        "医疗机构视图": "就诊表",
        "医保服务项目视图": "项目明细表",
    }
    for mapping in rows.get("data_mappings", {}).values():
        if str(mapping.get("scenario_id")) != MEDICAL_SCENARIO_ID:
            continue
        table = view_to_table.get(str(mapping.get("table_name")), str(mapping.get("table_name")))
        column_map = _json_load(mapping.get("column_map"), default={})
        for property_name, source_column in column_map.items():
            data_type = property_types.get(
                (str(mapping.get("entity_id")), str(property_name)), "string"
            )
            hints[table][str(source_column)] = data_type
    return {table: dict(columns) for table, columns in hints.items()}


def _column_max_lengths(
    connection: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
) -> dict[str, int]:
    if not columns:
        return {}
    expressions = [
        f"COALESCE(MAX(LENGTH(CAST({_q_sqlite(column)} AS TEXT))), 0)"
        for column in columns
    ]
    row = connection.execute(
        f"SELECT {', '.join(expressions)} FROM {_q_sqlite(table)}"
    ).fetchone()
    assert row is not None
    return {column: int(row[index] or 0) for index, column in enumerate(columns)}


def _varchar_length(observed: int, *, indexed: bool) -> int:
    if observed < 0:
        observed = 0
    minimum = 32
    maximum = 191 if indexed else 255
    return max(minimum, min(maximum, observed + max(8, observed // 5)))


def _medical_table_plan(
    connection: sqlite3.Connection,
    table: str,
    hints: Mapping[str, str],
) -> dict[str, Any]:
    source_columns = _table_info(connection, table)
    indexed_columns = {
        column
        for _index_name, columns in MEDICAL_INDEX_SPECS.get(table, ())
        for column in columns
    }
    varchar_candidates: list[str] = []
    roles: dict[str, str] = {}
    for column in source_columns:
        name = str(column["name"])
        data_type = str(hints.get(name) or "").lower()
        if name in MEDICAL_NUMERIC_COLUMNS or data_type in {
            "integer",
            "number",
            "float",
            "decimal",
        }:
            roles[name] = "decimal"
        elif name in indexed_columns or MEDICAL_VARCHAR_NAME_RE.search(name):
            roles[name] = "varchar"
            varchar_candidates.append(name)
        else:
            roles[name] = "longtext"
    max_lengths = _column_max_lengths(connection, table, varchar_candidates)
    columns: list[dict[str, Any]] = [
        {
            "name": "__migration_row_id",
            "source_name": None,
            "target_type": "BIGINT",
            "role": "surrogate",
            "nullable": False,
            "primary_key": True,
        }
    ]
    for source in source_columns:
        name = str(source["name"])
        role = roles[name]
        if role == "decimal":
            target_type = "DECIMAL(30,8)"
        elif role == "varchar":
            observed = max_lengths.get(name, 0)
            if observed > 255:
                if name in indexed_columns:
                    raise MigrationError(
                        f"{table}.{name} 最大长度 {observed}，不能安全建立要求的索引"
                    )
                role = "longtext"
                target_type = "LONGTEXT"
            else:
                target_type = f"VARCHAR({_varchar_length(observed, indexed=name in indexed_columns)})"
        else:
            target_type = "LONGTEXT"
        columns.append(
            {
                "name": name,
                "source_name": name,
                "target_type": target_type,
                "role": role,
                "nullable": True,
                "primary_key": False,
            }
        )
    for _index_name, index_columns in MEDICAL_INDEX_SPECS.get(table, ()):
        missing = set(index_columns) - {item["name"] for item in columns}
        if missing:
            raise MigrationError(f"{table} 缺少必要索引列：{sorted(missing)}")
    return {
        "columns": columns,
        "source_order": "rowid",
        "indexes": [
            {"name": name, "columns": list(index_columns)}
            for name, index_columns in MEDICAL_INDEX_SPECS.get(table, ())
        ],
    }


def _bookkeeping_table_plan(
    connection: sqlite3.Connection,
    table: str,
) -> dict[str, Any]:
    source_columns = _table_info(connection, table)
    pk_columns = {
        item["name"] for item in source_columns if int(item["pk_order"] or 0) > 0
    }
    varchar_candidates = [
        item["name"]
        for item in source_columns
        if item["name"] in pk_columns or BOOKKEEPING_VARCHAR_NAME_RE.search(item["name"])
    ]
    max_lengths = _column_max_lengths(connection, table, varchar_candidates)
    columns: list[dict[str, Any]] = []
    for source in source_columns:
        name = str(source["name"])
        declared = str(source["type"] or "").upper()
        if any(token in declared for token in ("REAL", "FLOAT", "DOUBLE")):
            role = "double"
            target_type = "DOUBLE"
        elif name in varchar_candidates:
            role = "varchar"
            target_type = f"VARCHAR({_varchar_length(max_lengths.get(name, 0), indexed=name in pk_columns)})"
        else:
            role = "longtext"
            target_type = "LONGTEXT"
        columns.append(
            {
                "name": name,
                "source_name": name,
                "target_type": target_type,
                "role": role,
                "nullable": name not in pk_columns,
                "primary_key": name in pk_columns,
            }
        )
    order = [
        item["name"]
        for item in sorted(source_columns, key=lambda item: item["pk_order"])
        if int(item["pk_order"] or 0) > 0
    ] or ["rowid"]
    return {
        "columns": columns,
        "source_order": order,
        "indexes": [
            {"name": name, "columns": list(index_columns)}
            for name, index_columns in BOOKKEEPING_INDEX_SPECS.get(table, ())
        ],
    }


def _source_row_iterator(
    connection: sqlite3.Connection,
    table: str,
    plan: Mapping[str, Any],
    *,
    batch_size: int = 2000,
) -> Iterator[dict[str, Any]]:
    order = list(plan["source_order"] if isinstance(plan["source_order"], list) else [plan["source_order"]])
    order_sql = ", ".join(
        "rowid" if item == "rowid" else _q_sqlite(item) for item in order
    )
    cursor = connection.execute(
        f"SELECT rowid AS {_q_sqlite('__source_rowid')}, * "
        f"FROM {_q_sqlite(table)} ORDER BY {order_sql}"
    )
    try:
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                break
            for row in batch:
                yield _row_dict(row)
    finally:
        cursor.close()


def _transform_business_row(
    source_row: Mapping[str, Any],
    plan: Mapping[str, Any],
    ordinal: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for column in plan["columns"]:
        name = str(column["name"])
        role = str(column["role"])
        source_name = column.get("source_name")
        value = ordinal if role == "surrogate" else source_row.get(str(source_name))
        if value is None:
            result[name] = None
        elif role == "decimal":
            normalized = _normalize_decimal(value)
            result[name] = Decimal(normalized) if normalized else None
        elif role == "double":
            number = float(value)
            if not math.isfinite(number):
                raise MigrationError("代理记账浮点列包含 NaN 或 Infinity")
            result[name] = number
        elif role in {"varchar", "longtext"}:
            result[name] = str(value)
        else:
            result[name] = int(value)
    return result


def _target_column_types(plan: Mapping[str, Any]) -> dict[str, str]:
    return {str(item["name"]): str(item["target_type"]) for item in plan["columns"]}


def _hash_business_table(
    connection: sqlite3.Connection,
    table: str,
    plan: Mapping[str, Any],
) -> tuple[int, str, str]:
    source_columns = [item["name"] for item in _table_info(connection, table)]
    source_types = {item["name"]: item["type"] for item in _table_info(connection, table)}
    source_digest = hashlib.sha256()
    target_digest = hashlib.sha256()
    count = 0
    target_columns = [str(item["name"]) for item in plan["columns"]]
    target_types = _target_column_types(plan)
    for ordinal, source_row in enumerate(
        _source_row_iterator(connection, table, plan), start=1
    ):
        source_values = [
            _canonical_value(source_row.get(column), source_types.get(column, ""))
            for column in source_columns
        ]
        source_digest.update(_canonical_json(source_values).encode("utf-8"))
        source_digest.update(b"\n")
        target_row = _transform_business_row(source_row, plan, ordinal)
        target_values = [
            _canonical_value(target_row.get(column), target_types.get(column, ""))
            for column in target_columns
        ]
        target_digest.update(_canonical_json(target_values).encode("utf-8"))
        target_digest.update(b"\n")
        count += 1
    return count, source_digest.hexdigest(), target_digest.hexdigest()


def _view_manifest(
    connection: sqlite3.Connection,
    view: str,
    *,
    decimal_columns: Iterable[str] = (),
) -> dict[str, Any]:
    info = _table_info(connection, view)
    columns = [item["name"] for item in info]
    decimal_set = set(decimal_columns)
    types = {
        column: "DECIMAL(30,8)" if column in decimal_set else "LONGTEXT"
        for column in columns
    }
    rows = connection.execute(f"SELECT * FROM {_q_sqlite(view)}")
    try:
        count, digest = _hash_rows_unordered(
            ({key: row[key] for key in row.keys()} for row in rows),
            columns,
            types,
        )
    finally:
        rows.close()
    return {
        "row_count": count,
        "target_row_sha256": digest,
        "columns": columns,
        "column_types": types,
    }


def collect_business_snapshot(
    path: Path,
    scenario: ScenarioContract,
    platform: Mapping[str, Any],
) -> dict[str, Any]:
    expected_tables = MEDICAL_TABLES if scenario.id == MEDICAL_SCENARIO_ID else BOOKKEEPING_TABLES
    expected_views = MEDICAL_VIEWS if scenario.id == MEDICAL_SCENARIO_ID else BOOKKEEPING_VIEWS
    with open_sqlite_readonly(path) as connection:
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise MigrationError(f"{path.name} quick_check 失败：{quick_check}")
        actual_tables = _table_names(connection)
        actual_views = _view_names(connection)
        if actual_tables != set(expected_tables):
            raise MigrationError(
                f"{path.name} 业务表集合不匹配，预期 {sorted(expected_tables)}，"
                f"实际 {sorted(actual_tables)}"
            )
        if actual_views != set(expected_views):
            raise MigrationError(
                f"{path.name} 视图集合不匹配，预期 {sorted(expected_views)}，"
                f"实际 {sorted(actual_views)}"
            )

        hints = _mapped_medical_column_hints(platform) if scenario.id == MEDICAL_SCENARIO_ID else {}
        tables: dict[str, Any] = {}
        for table in expected_tables:
            plan = (
                _medical_table_plan(connection, table, hints.get(table, {}))
                if scenario.id == MEDICAL_SCENARIO_ID
                else _bookkeeping_table_plan(connection, table)
            )
            count, source_hash, target_hash = _hash_business_table(connection, table, plan)
            tables[table] = {
                "row_count": count,
                "source_row_sha256": source_hash,
                "target_row_sha256": target_hash,
                "plan": plan,
            }

        views: dict[str, Any] = {}
        for view in expected_views:
            decimal_columns = {"参考单价"} if view == "医保服务项目视图" else set()
            item = _view_manifest(connection, view, decimal_columns=decimal_columns)
            ddl = (
                MEDICAL_VIEW_DDL[view]
                if scenario.id == MEDICAL_SCENARIO_ID
                else BOOKKEEPING_VIEW_DDL[view]
            )
            item["target_ddl"] = ddl
            item["target_ddl_sha256"] = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
            views[view] = item
        return {
            "path": str(path.resolve()),
            "file_size": path.stat().st_size,
            "file_sha256": _sha256_file(path),
            "quick_check": quick_check,
            "tables": tables,
            "views": views,
        }


def _safe_filename(filename: str) -> str:
    value = str(filename or "").strip()
    if (
        not value
        or len(value) > 500
        or Path(value).name != value
        or "/" in value
        or "\\" in value
        or value in {".", ".."}
        or any(ord(character) < 32 for character in value)
    ):
        raise MigrationError(f"BucketFile 文件名不安全：{filename!r}")
    return value


def build_object_key(
    prefix: str,
    tenant_id: str,
    scenario_id: str,
    data_source_id: str,
    file_id: str,
    filename: str,
) -> str:
    if scenario_id not in TARGET_SCENARIO_IDS:
        raise MigrationError("MinIO object key 只能属于目标场景")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", str(tenant_id)):
        raise MigrationError("MinIO object key 的 tenant_id 无效")
    for label, value in (
        ("data_source_id", data_source_id),
        ("file_id", file_id),
    ):
        if not HEX32_RE.fullmatch(str(value).lower()):
            raise MigrationError(f"{label} 不是 32 位十六进制 ID")
    parts = [str(prefix or "").strip().strip("/")]
    parts.extend(
        (
            "tenants",
            str(tenant_id),
            "scenarios",
            scenario_id,
            "data-sources",
            data_source_id,
            "files",
            file_id,
            _safe_filename(filename),
        )
    )
    return "/".join(part for part in parts if part)


def build_object_url(*, endpoint: str, secure: bool, bucket: str, object_key: str) -> str:
    del endpoint, secure  # Stable URLs are endpoint-independent locators.
    encoded_key = url_quote(object_key.strip("/"), safe="/-_.~")
    return f"minio://{bucket}/{encoded_key}"


def _assert_non_reusable_object_keys(files: Sequence[Mapping[str, Any]]) -> None:
    """Prove that one durable BucketFile identity owns exactly one object key."""
    file_ids: set[str] = set()
    object_keys: set[str] = set()
    for item in files:
        file_id = str(item.get("file_id") or "")
        object_key = str(item.get("object_key") or "")
        if not file_id or not object_key:
            raise MigrationError("MinIO 文件清单缺少 file_id 或 object_key")
        if file_id in file_ids:
            raise MigrationError(f"MinIO 文件清单重复 file_id：{file_id}")
        if object_key in object_keys:
            raise MigrationError(f"MinIO object key 冲突：{object_key}")
        if f"/files/{file_id}/" not in f"/{object_key}":
            raise MigrationError(
                f"MinIO object key 未由不可复用 file_id 隔离：{object_key}"
            )
        file_ids.add(file_id)
        object_keys.add(object_key)


def _resolve_bucket_file(
    row: Mapping[str, Any],
    *,
    buckets_root: Path,
    data_root: Path,
) -> Path:
    data_source_id = str(row.get("data_source_id") or "")
    filename = _safe_filename(str(row.get("filename") or ""))
    expected_size = int(row.get("size") or 0)
    root = (buckets_root / data_source_id).resolve(strict=True)
    try:
        root.relative_to(buckets_root.resolve(strict=True))
    except ValueError as exc:
        raise MigrationError("文件桶目录越界") from exc
    if root.is_symlink():
        raise MigrationError(f"文件桶不能是符号链接：{root}")

    candidates: list[Path] = []
    stored_path = Path(str(row.get("stored_path") or ""))
    if stored_path.is_file():
        candidates.append(stored_path.resolve(strict=True))
    for candidate in root.rglob(filename):
        if candidate.is_file() and not candidate.is_symlink():
            resolved = candidate.resolve(strict=True)
            if resolved not in candidates:
                candidates.append(resolved)
    valid: list[Path] = []
    data_root_resolved = data_root.resolve(strict=True)
    for candidate in candidates:
        try:
            candidate.relative_to(root)
            candidate.relative_to(data_root_resolved)
        except ValueError:
            continue
        if candidate.stat().st_size == expected_size:
            valid.append(candidate)
    if len(valid) != 1:
        raise MigrationError(
            f"BucketFile {row.get('id')} 无法唯一解析：匹配到 {len(valid)} 个文件"
        )
    return valid[0]


def collect_file_snapshot(
    paths: RuntimePaths,
    platform: Mapping[str, Any],
    *,
    minio_endpoint: str,
    minio_bucket: str,
    minio_prefix: str,
    minio_secure: bool,
) -> list[dict[str, Any]]:
    source_to_scenario = {
        scenario.bucket_source_id: scenario.id for scenario in SCENARIOS
    }
    scenario_tenants = {
        str(row.get("id")): str(row.get("tenant_id") or "")
        for row in platform["_rows"]["business_scenarios"].values()
    }
    files: list[dict[str, Any]] = []
    object_keys: set[str] = set()
    for row in platform["_rows"]["bucket_files"].values():
        data_source_id = str(row.get("data_source_id") or "")
        scenario_id = source_to_scenario.get(data_source_id)
        if not scenario_id:
            raise MigrationError(f"BucketFile 意外属于非目标数据源 {data_source_id}")
        resolved = _resolve_bucket_file(
            row,
            buckets_root=paths.buckets_root,
            data_root=paths.data_root,
        )
        digest = _sha256_file(resolved)
        recorded_digest = str(row.get("content_sha256") or "").lower()
        if recorded_digest and recorded_digest != digest:
            raise MigrationError(f"BucketFile {row.get('id')} content_sha256 与实体文件不一致")
        object_key = build_object_key(
            minio_prefix,
            scenario_tenants.get(scenario_id, ""),
            scenario_id,
            data_source_id,
            str(row.get("id") or ""),
            str(row.get("filename") or ""),
        )
        if object_key in object_keys:
            raise MigrationError(f"MinIO object key 冲突：{object_key}")
        object_keys.add(object_key)
        files.append(
            {
                "file_id": str(row.get("id")),
                "scenario_id": scenario_id,
                "data_source_id": data_source_id,
                "filename": str(row.get("filename")),
                "source_path": str(resolved),
                "source_relative_path": resolved.relative_to(paths.data_root.resolve()).as_posix(),
                "size": resolved.stat().st_size,
                "sha256": digest,
                "mime": str(row.get("mime") or mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"),
                "bucket_name": minio_bucket,
                "object_key": object_key,
                "object_url": build_object_url(
                    endpoint=minio_endpoint,
                    secure=minio_secure,
                    bucket=minio_bucket,
                    object_key=object_key,
                ),
                "stored_path_was_stale": not Path(str(row.get("stored_path") or "")).is_file(),
            }
        )
    files.sort(key=lambda item: item["file_id"])
    if len(files) != EXPECTED_BUCKET_FILE_COUNT:
        raise MigrationError(
            f"物理文件清单应为 {EXPECTED_BUCKET_FILE_COUNT} 个，实际 {len(files)} 个"
        )
    _assert_non_reusable_object_keys(files)
    return files


def _cleanup_inventory(
    data_root: Path,
    *,
    hash_cache: MutableMapping[Path, str] | None = None,
) -> list[dict[str, Any]]:
    root = data_root.resolve(strict=True)
    cache = hash_cache if hash_cache is not None else {}
    inventory: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise MigrationError(f"cleanup 不接受符号链接：{path}")
        if not path.is_file():
            continue
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:  # pragma: no cover - guarded by rglob.
            raise MigrationError("cleanup 文件越界") from exc
        digest = cache.get(resolved)
        if digest is None:
            digest = _sha256_file(resolved)
            cache[resolved] = digest
        stat = resolved.stat()
        inventory.append(
            {
                "relative_path": relative.as_posix(),
                "size": stat.st_size,
                "sha256": digest,
            }
        )
    return inventory


def _public_platform_snapshot(platform: Mapping[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in platform.items() if not key.startswith("_")}


def _validate_scenario_contract(platform: Mapping[str, Any], paths: RuntimePaths) -> None:
    scenario_rows = {
        str(row.get("id")): row
        for row in platform["_rows"]["business_scenarios"].values()
    }
    source_rows = {
        str(row.get("id")): row for row in platform["_rows"]["data_sources"].values()
    }
    for contract in SCENARIOS:
        row = scenario_rows.get(contract.id)
        if not row:
            raise MigrationError(f"缺少目标场景 {contract.id}")
        if str(row.get("name")) != contract.name or str(row.get("namespace")) != contract.namespace:
            raise MigrationError(
                f"目标场景身份不匹配：{contract.id} 实际为 "
                f"{row.get('name')}/{row.get('namespace')}"
            )
        sql_source = source_rows.get(contract.sql_source_id)
        bucket_source = source_rows.get(contract.bucket_source_id)
        if not sql_source or str(sql_source.get("type")) != "sqlite":
            raise MigrationError(f"{contract.name} 缺少固定 SQLite 数据源")
        if not bucket_source or str(bucket_source.get("type")) != "file_bucket":
            raise MigrationError(f"{contract.name} 缺少固定 file_bucket 数据源")
        config = _json_load(sql_source.get("config"), default={})
        configured_path = Path(str(config.get("path") or "")).resolve()
        expected_path = (paths.data_root / contract.sqlite_filename).resolve()
        if configured_path != expected_path:
            raise MigrationError(
                f"{contract.name} SQLite 路径不匹配：{configured_path} != {expected_path}"
            )


def _manifest_contract() -> dict[str, Any]:
    return {
        "migration_name": MIGRATION_NAME,
        "scenario_ids": list(TARGET_SCENARIO_IDS),
        "data_source_ids": list(TARGET_DATA_SOURCE_IDS),
        "source_platform_tables": list(SOURCE_PLATFORM_TABLES),
        "platform_tables": list(PLATFORM_TABLES),
        "target_empty_tables": ["object_deletion_jobs"],
        "excluded_legacy_platform_tables": sorted(EXCLUDED_LEGACY_PLATFORM_TABLES),
        "excluded_entity_lifecycle": DEPRECATED_ENTITY_STATUS,
        "retired_workflow_ids": sorted(RETIRED_WORKFLOW_IDS),
        "retired_workflow_marker": RETIRED_WORKFLOW_MARKER,
        "transient_data_policy": {
            "email_verification_codes": "exclude_all",
            "auth_sessions": "expires_after_manifest_snapshot",
        },
        "business_objects": {
            BOOKKEEPING_SCENARIO_ID: {
                "tables": list(BOOKKEEPING_TABLES),
                "views": list(BOOKKEEPING_VIEWS),
            },
            MEDICAL_SCENARIO_ID: {
                "tables": list(MEDICAL_TABLES),
                "views": list(MEDICAL_VIEWS),
            },
        },
        "expected_bucket_files": EXPECTED_BUCKET_FILE_COUNT,
        "mysql_datetime_precision": MYSQL_DATETIME_PRECISION,
        "business_view_target_semantics": BUSINESS_VIEW_TARGET_SEMANTICS,
        "minio_bucket_versioning_capabilities": list(
            MINIO_VERSIONING_CAPABILITIES
        ),
        "minio_object_key": {
            "strategy": MINIO_OBJECT_KEY_STRATEGY,
            "unique_identity": "bucket_files.id",
            "existing_object_policy": "same-key-same-bytes-only",
        },
    }


def _legacy_v2_manifest_contract() -> dict[str, Any]:
    contract = _manifest_contract()
    contract.pop("mysql_datetime_precision")
    contract.pop("business_view_target_semantics")
    return contract


def _validate_supersedes_descriptor(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "mode",
        "old_plan_digest",
        "old_expected_sha256",
    }:
        raise MigrationError("supersedes 描述符结构无效")
    descriptor = {str(key): str(item) for key, item in value.items()}
    if descriptor["mode"] != SUPERSEDE_MODE_V2_DATETIME6:
        raise MigrationError("supersedes 恢复模式不受支持")
    for key in ("old_plan_digest", "old_expected_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", descriptor[key]):
            raise MigrationError(f"supersedes {key} 不是有效 SHA-256")
    return descriptor


def _validate_v2_supersede_manifest(manifest: Mapping[str, Any]) -> dict[str, str]:
    if int(manifest.get("format_version") or 0) != 2:
        raise MigrationError("--supersede-manifest 只接受 format_version=2")
    if manifest.get("contract") != _legacy_v2_manifest_contract():
        raise MigrationError("旧 v2 manifest 契约不是当前 DATETIME(6) 契约的直接前身")
    old_digest = str(manifest.get("plan_digest") or "")
    if old_digest != _sha256_json(
        {
            "contract": manifest.get("contract"),
            "source": manifest.get("source"),
            "target": manifest.get("target"),
        }
    ):
        raise MigrationError("旧 v2 manifest 不可变部分已被修改")
    state = manifest.get("state")
    if not isinstance(state, Mapping):
        raise MigrationError("旧 v2 manifest 缺少执行状态")
    if (
        state.get("executed") is not True
        or state.get("verified") is not False
        or state.get("cleaned") is not False
    ):
        raise MigrationError(
            "旧 v2 manifest 必须严格为 executed=true、verified=false、cleaned=false"
        )
    expected = state.get("target_expected")
    if not isinstance(expected, Mapping) or not expected:
        raise MigrationError("旧 v2 manifest 缺少 target_expected")
    expected_sha = str(state.get("target_expected_sha256") or "")
    if expected_sha != _sha256_json(expected):
        raise MigrationError("旧 v2 manifest 的 target_expected SHA-256 不自洽")
    if int(expected.get("format_version") or 0) != 2:
        raise MigrationError("旧 v2 target_expected 格式无效")
    if str(expected.get("plan_digest") or "") != old_digest:
        raise MigrationError("旧 v2 target_expected plan_digest 不匹配")
    return {
        "mode": SUPERSEDE_MODE_V2_DATETIME6,
        "old_plan_digest": old_digest,
        "old_expected_sha256": expected_sha,
    }


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise MigrationError(f"{label} 不存在：{path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{label} 无法读取") from exc
    if not isinstance(payload, dict):
        raise MigrationError(f"{label} 根节点必须是对象")
    return payload


def _normalize_minio_versioning_capability(value: Any) -> str:
    capability = str(value or "").strip()
    if capability not in MINIO_VERSIONING_CAPABILITIES:
        raise MigrationError(f"MinIO versioning 能力状态无效：{capability!r}")
    return capability


def _redacted_target_descriptor(
    *,
    mysql_host: str,
    mysql_port: int,
    mysql_database: str,
    minio_endpoint: str,
    minio_bucket: str,
    minio_prefix: str,
    minio_secure: bool,
    minio_versioning: str,
    mysql_runtime_user: str = RUNTIME_MYSQL_USER_DEFAULT,
    mysql_account_host: str = "%",
    readonly_users: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "mysql": {
            "host": mysql_host,
            "port": mysql_port,
            "database": mysql_database,
            "runtime_user": mysql_runtime_user,
            "account_host": mysql_account_host,
            "readonly_users": dict(readonly_users or {}),
        },
        "minio": {
            "endpoint": minio_endpoint,
            "bucket": minio_bucket,
            "prefix": minio_prefix,
            "secure": minio_secure,
            "versioning": _normalize_minio_versioning_capability(
                minio_versioning
            ),
            "object_key_strategy": MINIO_OBJECT_KEY_STRATEGY,
        },
    }


def _dry_run_target_settings(env_file: Path) -> dict[str, Any]:
    _load_env_file(env_file)
    try:
        mysql_port = int(str(os.environ.get("ANNUAL_MYSQL_PORT") or "3306"))
    except ValueError as exc:
        raise MigrationError("ANNUAL_MYSQL_PORT 必须是整数") from exc
    endpoint = str(os.environ.get("MINIO_ALIYUN_ENDPOINT") or "").strip()
    endpoint = endpoint.removeprefix("https://").removeprefix("http://").rstrip("/")
    bucket = str(os.environ.get("MINIO_BUCKETNAME") or "").strip()
    mysql_host = str(os.environ.get("ANNUAL_MYSQL_HOST") or "").strip()
    mysql_database = str(os.environ.get("ANNUAL_MYSQL_DATABASE") or "").strip()
    runtime_user = str(
        os.environ.get("MIGRATION_MYSQL_APP_USER") or RUNTIME_MYSQL_USER_DEFAULT
    ).strip()
    account_host = str(
        os.environ.get("MIGRATION_MYSQL_ACCOUNT_HOST") or "%"
    ).strip()
    readonly_users = {
        scenario.id: str(
            os.environ.get(scenario.readonly_user_env)
            or scenario.readonly_user_default
        ).strip()
        for scenario in SCENARIOS
    }
    if not mysql_host or not mysql_database:
        raise MigrationError("dry-run 需要 ANNUAL_MYSQL_HOST 和 ANNUAL_MYSQL_DATABASE")
    if not MYSQL_ACCOUNT_RE.fullmatch(runtime_user):
        raise MigrationError("MIGRATION_MYSQL_APP_USER 格式无效")
    if not MYSQL_ACCOUNT_HOST_RE.fullmatch(account_host):
        raise MigrationError("MIGRATION_MYSQL_ACCOUNT_HOST 格式无效")
    if any(not MYSQL_ACCOUNT_RE.fullmatch(user) for user in readonly_users.values()):
        raise MigrationError("场景只读账号格式无效")
    if runtime_user in readonly_users.values() or len(set(readonly_users.values())) != 2:
        raise MigrationError("运行账号和两个场景只读账号必须互不相同")
    if not endpoint or not bucket:
        raise MigrationError("dry-run 需要 MINIO_ALIYUN_ENDPOINT 和 MINIO_BUCKETNAME")
    minio_secure = _parse_bool(
        os.environ.get("MINIO_ALIYUN_SECURE"), default=True
    )
    minio_client = _new_minio_client(
        endpoint=endpoint,
        access_key=_required_env("MINIO_ALIYUN_ACCESS_KEY_ID"),
        secret_key=_required_env("MINIO_ALIYUN_ACCESS_KEY_SECRET"),
        secure=minio_secure,
    )
    minio_versioning = _probe_minio_versioning(
        minio_client,
        bucket,
        prefix=str(os.environ.get("MINIO_ALIYUN_FILE_PATH") or "")
        .strip()
        .strip("/"),
    )
    return _redacted_target_descriptor(
        mysql_host=mysql_host,
        mysql_port=mysql_port,
        mysql_database=mysql_database,
        minio_endpoint=endpoint,
        minio_bucket=bucket,
        minio_prefix=str(os.environ.get("MINIO_ALIYUN_FILE_PATH") or "").strip().strip("/"),
        minio_secure=minio_secure,
        minio_versioning=minio_versioning,
        mysql_runtime_user=runtime_user,
        mysql_account_host=account_host,
        readonly_users=readonly_users,
    )


def build_dry_run_manifest(
    paths: RuntimePaths,
    *,
    include_cleanup_inventory: bool = True,
    snapshot_time: str | datetime | None = None,
    supersedes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    manifest_resolved = paths.manifest_path.resolve()
    data_resolved = paths.data_root.resolve(strict=True)
    try:
        manifest_resolved.relative_to(data_resolved)
    except ValueError:
        pass
    else:
        raise MigrationError("manifest 必须位于 backend/data 之外，避免 cleanup 自删")

    target = _dry_run_target_settings(paths.env_file)
    platform = collect_platform_snapshot(paths.platform_db, snapshot_time=snapshot_time)
    _validate_scenario_contract(platform, paths)
    business: dict[str, Any] = {}
    hash_cache: dict[Path, str] = {}
    for scenario in SCENARIOS:
        source_path = paths.data_root / scenario.sqlite_filename
        snapshot = collect_business_snapshot(source_path, scenario, platform)
        business[scenario.id] = snapshot
        hash_cache[source_path.resolve()] = str(snapshot["file_sha256"])

    minio_target = target["minio"]
    files = collect_file_snapshot(
        paths,
        platform,
        minio_endpoint=str(minio_target["endpoint"]),
        minio_bucket=str(minio_target["bucket"]),
        minio_prefix=str(minio_target["prefix"]),
        minio_secure=bool(minio_target["secure"]),
    )
    for item in files:
        hash_cache[Path(str(item["source_path"])).resolve()] = str(item["sha256"])
    cleanup = (
        _cleanup_inventory(paths.data_root, hash_cache=hash_cache)
        if include_cleanup_inventory
        else []
    )
    source = {
        "platform": _public_platform_snapshot(platform),
        "business": business,
        "files": files,
        "cleanup_inventory": cleanup,
    }
    contract = _manifest_contract()
    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "created_at": _utc_now(),
        "contract": contract,
        "source": source,
        "target": target,
        "state": {
            "executed": False,
            "executed_at": None,
            "verified": False,
            "verified_at": None,
            "cleaned": False,
            "cleaned_at": None,
        },
    }
    if supersedes is not None:
        manifest["supersedes"] = _validate_supersedes_descriptor(supersedes)
    plan_digest = _sha256_json(_manifest_immutable_payload(manifest))
    manifest["plan_digest"] = plan_digest
    manifest["confirm_execute"] = f"MIGRATE_{plan_digest[:16].upper()}"
    manifest["confirm_cleanup"] = f"CLEANUP_{plan_digest[:16].upper()}"
    return manifest


def build_superseding_dry_run_manifest(
    paths: RuntimePaths, supersede_manifest_path: Path
) -> dict[str, Any]:
    old_manifest = _read_json_object(
        supersede_manifest_path.resolve(), label="旧 v2 supersede manifest"
    )
    descriptor = _validate_v2_supersede_manifest(old_manifest)
    try:
        snapshot_time = old_manifest["source"]["platform"]["snapshot_time"]
    except (KeyError, TypeError) as exc:
        raise MigrationError("旧 v2 manifest 缺少平台 snapshot_time") from exc
    rebuilt = build_dry_run_manifest(
        paths,
        include_cleanup_inventory=True,
        snapshot_time=snapshot_time,
        supersedes=descriptor,
    )
    if rebuilt["source"] != old_manifest.get("source"):
        raise MigrationError("新 v3 source 与旧 v2 manifest 不完全相同，拒绝 supersede")
    if rebuilt["target"] != old_manifest.get("target"):
        raise MigrationError("新 v3 target 与旧 v2 manifest 不完全相同，拒绝 supersede")
    return rebuilt


def _manifest_immutable_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "contract": manifest.get("contract"),
        "source": manifest.get("source"),
        "target": manifest.get("target"),
    }
    if "supersedes" in manifest:
        payload["supersedes"] = manifest.get("supersedes")
    return payload


def validate_manifest(manifest: Mapping[str, Any]) -> None:
    if int(manifest.get("format_version") or 0) != MANIFEST_FORMAT_VERSION:
        raise MigrationError("manifest 格式版本不受支持")
    expected = _sha256_json(_manifest_immutable_payload(manifest))
    if expected != str(manifest.get("plan_digest") or ""):
        raise MigrationError("manifest 不可变部分已被修改")
    if manifest.get("contract") != _manifest_contract():
        raise MigrationError("manifest 迁移契约与当前脚本不一致")
    if "supersedes" in manifest:
        _validate_supersedes_descriptor(manifest.get("supersedes"))


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


def load_manifest(path: Path) -> dict[str, Any]:
    payload = _read_json_object(path, label="manifest")
    validate_manifest(payload)
    return payload


def assert_source_unchanged(paths: RuntimePaths, manifest: Mapping[str, Any]) -> None:
    rebuilt = build_dry_run_manifest(
        paths,
        include_cleanup_inventory=True,
        snapshot_time=manifest["source"]["platform"]["snapshot_time"],
        supersedes=manifest.get("supersedes"),
    )
    if rebuilt["plan_digest"] != manifest["plan_digest"]:
        raise MigrationError(
            "本地 SQLite/文件/cleanup 清单在 dry-run 后发生变化；必须重新 dry-run 并重新审核"
        )


def _binding_key(data_source_name: str) -> str:
    return f"data_source:{data_source_name}:mysql"


def _rewrite_runtime_binding_payload(
    value: Any,
    replacements: Mapping[str, str],
) -> Any:
    if isinstance(value, list):
        return [_rewrite_runtime_binding_payload(item, replacements) for item in value]
    if not isinstance(value, Mapping):
        return value
    result = {
        str(key): _rewrite_runtime_binding_payload(item, replacements)
        for key, item in value.items()
    }
    raw_key = result.get("data_source_binding_key")
    if isinstance(raw_key, str) and raw_key in replacements:
        result["data_source_binding_key"] = replacements[raw_key]
        reference = result.get("data_source_binding_ref")
        if isinstance(reference, Mapping):
            result["data_source_binding_ref"] = {
                **dict(reference),
                "adapter": "mysql",
                "required_capabilities": sorted(
                    {
                        *[
                            str(item)
                            for item in reference.get("required_capabilities", [])
                            if str(item)
                        ],
                        "sql_read",
                    }
                ),
            }
    return result


def _connector_signature_for_row(row: Mapping[str, Any]) -> str:
    config = row.get("config")
    if not isinstance(config, Mapping):
        config = _json_load(config, default={})
    payload = {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "adapter": str(row.get("type") or ""),
        "tenant_id": str(row.get("tenant_id") or ""),
        "enabled": True,
        "connector_revision": int(row.get("connector_revision") or 1),
        "scenario_id": str(row.get("scenario_id") or ""),
        "config_keys": sorted(str(key) for key in config),
        "status": str(row.get("status") or "unknown"),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _deterministic_binding_id(scenario_id: str, key: str) -> str:
    return hashlib.sha256(
        f"{MIGRATION_NAME}:{scenario_id}:dev:{key}".encode("utf-8")
    ).hexdigest()[:32]


def _parsed_platform_rows(platform: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    schemas = platform["_schemas"]
    result: dict[str, list[dict[str, Any]]] = {}
    for table in PLATFORM_TABLES:
        rows: list[dict[str, Any]] = []
        for source in platform["_rows"][table].values():
            row = copy.deepcopy(dict(source))
            for column in schemas[table]["json_columns"]:
                row[column] = _json_load(row.get(column), default={})
            rows.append(row)
        pk_columns = list(schemas[table]["pk_columns"])
        rows.sort(key=lambda item: tuple(str(item.get(column) or "") for column in pk_columns))
        result[table] = rows
    return result


def transform_platform_rows(
    platform: Mapping[str, Any],
    settings: ServiceSettings,
    uploaded_files: Mapping[str, Mapping[str, Any]],
    *,
    executed_at: str,
) -> dict[str, list[dict[str, Any]]]:
    rows = _parsed_platform_rows(platform)
    data_sources = {str(row["id"]): row for row in rows["data_sources"]}
    old_to_new_binding_key: dict[str, str] = {}
    scenario_source_config: dict[str, tuple[ScenarioContract, dict[str, Any]]] = {}

    for scenario in SCENARIOS:
        sql_source = data_sources.get(scenario.sql_source_id)
        bucket_source = data_sources.get(scenario.bucket_source_id)
        if not sql_source or not bucket_source:
            raise MigrationError(f"{scenario.name} 的固定数据源不在平台闭包中")
        username, password = settings.readonly_accounts[scenario.id]
        old_key = f"data_source:{sql_source['name']}:sqlite"
        new_key = _binding_key(str(sql_source["name"]))
        old_to_new_binding_key[old_key] = new_key
        sql_source["type"] = "mysql"
        sql_source["config"] = {
            "host": settings.mysql_host,
            "port": settings.mysql_port,
            "database": settings.mysql_database,
            "user": username,
            "password": password,
            "charset": "utf8mb4",
        }
        sql_source["connector_revision"] = int(sql_source.get("connector_revision") or 1) + 1
        sql_source["status"] = "ok"
        sql_source["last_error"] = ""

        bucket_source["config"] = {
            "storage_backend": "minio",
            "bucket_name": settings.minio_bucket,
            # datasource_service._managed_minio_location reads ``prefix``.
            # Keep this key aligned with runtime validation: a superficially
            # similar ``object_prefix`` makes every migrated object unreadable.
            "prefix": settings.minio_prefix.strip("/"),
        }
        bucket_source["connector_revision"] = int(bucket_source.get("connector_revision") or 1) + 1
        bucket_source["status"] = "ok"
        bucket_source["last_error"] = ""
        scenario_source_config[scenario.id] = (scenario, sql_source)

    for row in rows["data_mappings"]:
        source = data_sources.get(str(row.get("data_source_id") or ""))
        if source and str(source.get("type")) == "mysql":
            row["data_source_binding_key"] = _binding_key(str(source["name"]))
            row["data_source_binding_ref"] = {
                "adapter": "mysql",
                "required_capabilities": ["sql_read"],
            }
    for row in rows["relation_data_mappings"]:
        source = data_sources.get(str(row.get("data_source_id") or ""))
        if source and str(source.get("type")) == "mysql":
            row["data_source_binding_key"] = _binding_key(str(source["name"]))
            row["data_source_binding_ref"] = {
                "adapter": "mysql",
                "required_capabilities": ["sql_read"],
            }

    selected_by_capability = {
        "actions": {str(row["id"]) for row in rows["ontology_actions"]},
        "rules": {str(row["id"]) for row in rows["ontology_rules"]},
        "events": {str(row["id"]) for row in rows["ontology_events"]},
        "workflows": {str(row["id"]) for row in rows["ontology_workflows"]},
        "functions": {str(row["id"]) for row in rows["function_definitions"]},
    }
    for agent in rows["agents"]:
        scope = agent.get("capability_scope")
        if not isinstance(scope, Mapping):
            continue
        normalized_scope = copy.deepcopy(dict(scope))
        for kind, retained_ids in selected_by_capability.items():
            group = normalized_scope.get(kind)
            if not isinstance(group, Mapping):
                continue
            values = [
                str(item)
                for item in group.get("selected_ids", [])
                if str(item) in retained_ids
            ]
            normalized_scope[kind] = {**dict(group), "selected_ids": values}
        agent["capability_scope"] = normalized_scope

    # Rewrite only runtime definition JSON, not immutable execution history.
    runtime_json_tables = (
        "ontology_actions",
        "ontology_rules",
        "ontology_workflows",
        "function_definitions",
        "ontology_snapshots",
    )
    schemas = platform["_schemas"]
    for table in runtime_json_tables:
        for row in rows[table]:
            for column in schemas[table]["json_columns"]:
                row[column] = _rewrite_runtime_binding_payload(
                    row.get(column), old_to_new_binding_key
                )

    for bucket_file in rows["bucket_files"]:
        file_id = str(bucket_file.get("id") or "")
        uploaded = uploaded_files.get(file_id)
        if not uploaded:
            raise MigrationError(f"BucketFile {file_id} 缺少 MinIO 上传结果")
        bucket_file.update(
            {
                "storage_provider": "minio",
                "bucket_name": str(uploaded["bucket_name"]),
                "object_key": str(uploaded["object_key"]),
                "object_version_id": str(uploaded.get("object_version_id") or ""),
                "etag": str(uploaded.get("etag") or ""),
                "object_url": str(uploaded["object_url"]),
                "stored_path": str(uploaded["object_url"]),
                "content_sha256": str(uploaded["sha256"]),
                "size": int(uploaded["size"]),
            }
        )

    for attachment in rows["assistant_attachments"]:
        # Legacy temporary attachments did not retain their original bytes.
        # Preserve that fact explicitly while creating the current ORM shape.
        attachment.setdefault("storage_provider", "none")
        for name, _ddl in MINIO_ASSISTANT_ATTACHMENT_COLUMNS:
            if name != "storage_provider":
                attachment.setdefault(name, "")

    # One healthy dev binding per retained SQL source.  The existing medical
    # binding keeps its ID; bookkeeping receives a deterministic ID.
    bindings_by_scenario: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for binding in rows["connector_bindings"]:
        if str(binding.get("connector_kind")) == "data_source":
            bindings_by_scenario[str(binding.get("scenario_id"))].append(binding)
    final_bindings: list[dict[str, Any]] = [
        binding
        for binding in rows["connector_bindings"]
        if str(binding.get("connector_kind")) != "data_source"
    ]
    for scenario_id, (scenario, source) in scenario_source_config.items():
        key = _binding_key(str(source["name"]))
        candidates = [
            item
            for item in bindings_by_scenario.get(scenario_id, [])
            if str(item.get("connector_id")) == scenario.sql_source_id
        ]
        if len(candidates) > 1:
            raise MigrationError(f"{scenario.name} 存在多个冲突的数据源绑定")
        if candidates:
            binding = candidates[0]
        else:
            scenario_row = next(
                item for item in rows["business_scenarios"] if item["id"] == scenario_id
            )
            binding = {
                "id": _deterministic_binding_id(scenario_id, key),
                "tenant_id": scenario_row["tenant_id"],
                "scenario_id": scenario_id,
                "environment": "dev",
                "binding_key": key,
                "reference_label": f"迁移后的 {source['name']}（MySQL 只读）",
                "connector_kind": "data_source",
                "connector_id": scenario.sql_source_id,
                "health_status": "healthy",
                "health_message": "",
                "connector_signature": "",
                "checked_at": executed_at,
                "created_by_user_id": None,
                "created_at": executed_at,
                "updated_at": executed_at,
            }
        binding.update(
            {
                "environment": "dev",
                "binding_key": key,
                "reference_label": f"迁移后的 {source['name']}（MySQL 只读）",
                "connector_kind": "data_source",
                "connector_id": scenario.sql_source_id,
                "health_status": "healthy",
                "health_message": "",
                "connector_signature": _connector_signature_for_row(source),
                "checked_at": executed_at,
                "updated_at": executed_at,
            }
        )
        final_bindings.append(binding)
    final_bindings.sort(key=lambda item: str(item.get("id") or ""))
    rows["connector_bindings"] = final_bindings
    return rows


def _platform_target_schemas(platform: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    del platform
    metadata = _orm_platform_metadata()
    try:
        from sqlalchemy.dialects.mysql import pymysql
    except ImportError as exc:  # pragma: no cover
        raise MigrationError("迁移需要 SQLAlchemy MySQL dialect") from exc
    dialect = pymysql.dialect()
    schemas: dict[str, dict[str, Any]] = {}
    for table_name in PLATFORM_TABLES:
        table = metadata.tables[table_name]
        foreign_keys: list[dict[str, Any]] = []
        for foreign_key_id, constraint in enumerate(table.foreign_key_constraints):
            for sequence, element in enumerate(constraint.elements):
                foreign_keys.append(
                    {
                        "id": foreign_key_id,
                        "seq": sequence,
                        "parent_table": element.column.table.name,
                        "child_column": element.parent.name,
                        "parent_column": element.column.name,
                        "on_update": str(constraint.onupdate or "NO ACTION"),
                        "on_delete": str(constraint.ondelete or "NO ACTION"),
                    }
                )
        column_types = {
            column.name: str(column.type.compile(dialect=dialect))
            for column in table.columns
        }
        pk_order = {
            column.name: index + 1
            for index, column in enumerate(table.primary_key.columns)
        }
        schemas[table_name] = {
            "columns": [
                {
                    "name": column.name,
                    "type": column_types[column.name],
                    "notnull": not column.nullable,
                    "default": None,
                    "pk_order": pk_order.get(column.name, 0),
                }
                for column in table.columns
            ],
            "column_names": [column.name for column in table.columns],
            "column_types": column_types,
            "nullable": {
                column.name: bool(column.nullable) for column in table.columns
            },
            "mysql_data_types": {
                column.name: {
                    "INTEGER": "int",
                    "BOOL": "tinyint",
                }.get(
                    column_types[column.name].split("(", 1)[0].upper(),
                    column_types[column.name].split("(", 1)[0].lower(),
                )
                for column in table.columns
            },
            "character_lengths": {
                column.name: getattr(column.type, "length", None)
                for column in table.columns
            },
            "datetime_precisions": {
                column.name: int(getattr(column.type, "fsp", 0) or 0)
                for column in table.columns
                if column_types[column.name]
                .split("(", 1)[0]
                .upper()
                in {"DATETIME", "TIMESTAMP"}
            },
            "json_columns": [
                column.name
                for column in table.columns
                if "JSON" in column_types[column.name].upper()
            ],
            "pk_columns": [column.name for column in table.primary_key.columns],
            "foreign_keys": foreign_keys,
        }
    return schemas


def _serialized_platform_text(value: Any, *, is_json: bool) -> str:
    if is_json:
        return _json_dump(_json_load(value, default={}))
    if isinstance(value, (Mapping, list, tuple)):
        return _json_dump(value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(
                "平台有界字符串列包含非 UTF-8 二进制值"
            ) from exc
    return str(value)


def _platform_target_width_violations(
    transformed: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    metadata: Any | None = None,
) -> list[dict[str, Any]]:
    """Return bounded target-column overflows without exposing row values."""
    target_metadata = metadata or _orm_platform_metadata()
    violations: list[dict[str, Any]] = []
    for table_name in sorted(set(transformed) & set(target_metadata.tables)):
        rows = transformed[table_name]
        table = target_metadata.tables[table_name]
        for column in table.columns:
            type_name = str(column.type).upper()
            is_json = "JSON" in type_name
            limit = getattr(column.type, "length", None)
            if not is_json and not isinstance(limit, int):
                continue
            maximum = 0
            overflow_rows = 0
            for row in rows:
                value = row.get(column.name)
                if value is None:
                    continue
                length = len(
                    _serialized_platform_text(value, is_json=is_json)
                )
                maximum = max(maximum, length)
                if isinstance(limit, int) and length > limit:
                    overflow_rows += 1
            if isinstance(limit, int) and overflow_rows:
                violations.append(
                    {
                        "table": table_name,
                        "column": column.name,
                        "target_length": limit,
                        "maximum_length": maximum,
                        "overflow_rows": overflow_rows,
                    }
                )
    return violations


def _assert_platform_target_widths(
    transformed: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    metadata: Any | None = None,
) -> None:
    violations = _platform_target_width_violations(
        transformed, metadata=metadata
    )
    if not violations:
        return
    details = ", ".join(
        f"{item['table']}.{item['column']}(target={item['target_length']},"
        f"max={item['maximum_length']},rows={item['overflow_rows']})"
        for item in violations
    )
    raise MigrationError(
        "平台目标列宽预检失败；请先扩宽 ORM，未输出任何源值：" + details
    )


def _manifest_upload_preview(
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    files = list(manifest["source"]["files"])
    _assert_non_reusable_object_keys(files)
    return {
        str(item["file_id"]): {
            **dict(item),
            "etag": "",
            "object_version_id": "",
        }
        for item in files
    }


def platform_expected_manifest(
    platform: Mapping[str, Any],
    transformed_rows: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    schemas = _platform_target_schemas(platform)
    result: dict[str, Any] = {}
    for table in PLATFORM_TABLES:
        rows = list(transformed_rows[table])
        pk_columns = list(schemas[table]["pk_columns"])
        rows.sort(key=lambda item: tuple(str(item.get(column) or "") for column in pk_columns))
        count, digest = _hash_rows(
            rows,
            schemas[table]["column_names"],
            schemas[table]["column_types"],
        )
        result[table] = {
            "row_count": count,
            "row_sha256": digest,
            "columns": list(schemas[table]["column_names"]),
            "column_types": dict(schemas[table]["column_types"]),
            "nullable": dict(schemas[table]["nullable"]),
            "mysql_data_types": dict(schemas[table]["mysql_data_types"]),
            "character_lengths": dict(schemas[table]["character_lengths"]),
            "datetime_precisions": dict(
                schemas[table]["datetime_precisions"]
            ),
            "pk_columns": list(schemas[table]["pk_columns"]),
            "foreign_keys": copy.deepcopy(schemas[table].get("foreign_keys", [])),
        }
    return result


CONTROL_TABLE = "__ontology_local_migration_state"
_PHASE_LOCK_STATE = threading.local()


@dataclass
class _PhaseLockHandle:
    key: str
    resource: Any | None
    reentrant: bool = False


def _thread_lock_depths() -> dict[str, int]:
    depths = getattr(_PHASE_LOCK_STATE, "depths", None)
    if depths is None:
        depths = {}
        _PHASE_LOCK_STATE.depths = depths
    return depths


def _acquire_local_phase_lock(manifest_path: Path) -> _PhaseLockHandle:
    lock_path = manifest_path.with_suffix(manifest_path.suffix + ".lock").resolve()
    key = f"file:{lock_path}"
    depths = _thread_lock_depths()
    if depths.get(key, 0):
        depths[key] += 1
        return _PhaseLockHandle(key=key, resource=None, reentrant=True)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - production migration currently runs Windows.
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise MigrationError("另一个迁移阶段正在使用该 manifest，拒绝并发执行") from exc
    depths[key] = 1
    return _PhaseLockHandle(key=key, resource=handle)


def _release_local_phase_lock(lock: _PhaseLockHandle | None) -> None:
    if lock is None:
        return
    depths = _thread_lock_depths()
    depth = depths.get(lock.key, 0)
    if depth > 1:
        depths[lock.key] = depth - 1
        return
    depths.pop(lock.key, None)
    if lock.reentrant or lock.resource is None:
        return
    handle = lock.resource
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _mysql_lock_name(settings: ServiceSettings) -> str:
    digest = hashlib.sha256(
        f"{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_database}:"
        f"{MIGRATION_NAME}".encode("utf-8")
    ).hexdigest()
    return f"ontology-migration-{digest[:40]}"


def _acquire_mysql_phase_lock(engine, settings: ServiceSettings) -> _PhaseLockHandle:
    lock_name = _mysql_lock_name(settings)
    key = f"mysql:{lock_name}"
    depths = _thread_lock_depths()
    if depths.get(key, 0):
        depths[key] += 1
        return _PhaseLockHandle(key=key, resource=None, reentrant=True)
    connection = engine.connect()
    try:
        acquired = connection.exec_driver_sql(
            "SELECT GET_LOCK(%s, 0)", (lock_name,)
        ).scalar_one()
        if int(acquired or 0) != 1:
            raise MigrationError("另一个 MySQL 迁移阶段正在执行，拒绝并发")
    except Exception:
        connection.close()
        raise
    depths[key] = 1
    return _PhaseLockHandle(key=key, resource=connection)


def _release_mysql_phase_lock(lock: _PhaseLockHandle | None) -> None:
    if lock is None:
        return
    depths = _thread_lock_depths()
    depth = depths.get(lock.key, 0)
    if depth > 1:
        depths[lock.key] = depth - 1
        return
    depths.pop(lock.key, None)
    if lock.reentrant or lock.resource is None:
        return
    connection = lock.resource
    try:
        lock_name = lock.key.removeprefix("mysql:")
        try:
            connection.exec_driver_sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
        except Exception:
            # Closing the dedicated connection releases a named lock even if
            # the explicit release query cannot be delivered.
            pass
    finally:
        connection.close()


def _mysql_engine(settings: ServiceSettings, *, user: str | None = None, password: str | None = None):
    try:
        from sqlalchemy import create_engine, event
        from sqlalchemy.engine import URL
    except ImportError as exc:  # pragma: no cover - declared runtime dependency.
        raise MigrationError("execute/verify 需要 SQLAlchemy 与 PyMySQL") from exc
    url = URL.create(
        "mysql+pymysql",
        username=user or settings.mysql_admin_user,
        password=password if password is not None else settings.mysql_admin_password,
        host=settings.mysql_host,
        port=settings.mysql_port,
        database=settings.mysql_database,
        query={"charset": "utf8mb4"},
    )
    engine = create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        hide_parameters=True,
    )

    @event.listens_for(engine, "connect")
    def _force_innodb(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            # The deployed server currently defaults to MyISAM.  Every admin
            # connection must override it even though every DDL below also
            # carries ENGINE=InnoDB explicitly.
            cursor.execute("SET SESSION default_storage_engine=InnoDB")
            cursor.execute("SET NAMES utf8mb4")
        finally:
            cursor.close()

    return engine


def _assert_mysql_server_contract(version: str, sql_mode: str) -> None:
    normalized_version = str(version or "").strip()
    lowered = normalized_version.lower()
    rejected_vendors = ("mariadb", "percona", "aurora", "tidb", "oceanbase")
    if any(vendor in lowered for vendor in rejected_vendors):
        raise MigrationError(f"目标数据库不是允许的 Oracle MySQL 8：{version}")
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", normalized_version)
    if not match or (int(match.group(1)), int(match.group(2))) < (8, 0):
        raise MigrationError(f"目标数据库必须是 Oracle MySQL >= 8.0：{version}")
    modes = {item.strip().upper() for item in str(sql_mode or "").split(",") if item.strip()}
    if not modes & {"STRICT_TRANS_TABLES", "STRICT_ALL_TABLES"}:
        raise MigrationError(
            "目标 MySQL 必须启用 STRICT_TRANS_TABLES 或 STRICT_ALL_TABLES"
        )


def _verify_mysql_server(connection) -> None:
    row = connection.exec_driver_sql(
        "SELECT VERSION(), @@SESSION.sql_mode"
    ).one()
    _assert_mysql_server_contract(str(row[0]), str(row[1] or ""))


def _orm_platform_metadata():
    try:
        from sqlalchemy import DateTime, MetaData
        from sqlalchemy.dialects import mysql
        from app.database import Base
        # Import modules for their declarative registrations.
        import app.external_api_models  # noqa: F401
        import app.models  # noqa: F401
    except ImportError as exc:  # pragma: no cover
        raise MigrationError("execute 需要 SQLAlchemy") from exc
    if set(Base.metadata.tables) != set(PLATFORM_TABLES):
        raise MigrationError(
            "当前 ORM 表集合与固定 59 表迁移契约不一致："
            f"缺少={sorted(set(PLATFORM_TABLES) - set(Base.metadata.tables))}，"
            f"额外={sorted(set(Base.metadata.tables) - set(PLATFORM_TABLES))}"
        )
    metadata = MetaData()
    for table_name in PLATFORM_TABLES:
        Base.metadata.tables[table_name].to_metadata(metadata)
    for table in metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, DateTime):
                column.type = mysql.DATETIME(
                    timezone=bool(getattr(column.type, "timezone", False)),
                    fsp=MYSQL_DATETIME_PRECISION,
                )
        table.dialect_options["mysql"]["engine"] = "InnoDB"
        table.dialect_options["mysql"]["charset"] = "utf8mb4"
        table.dialect_options["mysql"]["collate"] = "utf8mb4_unicode_ci"
    return metadata


def _source_platform_metadata(platform_db: Path):
    # Resolve solely to keep the execute precondition explicit.  Target DDL
    # comes from the current ORM, never from stale SQLite declarations.
    platform_db.resolve(strict=True)
    return _orm_platform_metadata()


def _target_objects(connection) -> dict[str, dict[str, Any]]:
    rows = connection.exec_driver_sql(
        "SELECT TABLE_NAME, TABLE_TYPE, ENGINE "
        "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
        (connection.engine.url.database,),
    ).mappings()
    return {
        str(row["TABLE_NAME"]): {
            "type": str(row["TABLE_TYPE"]),
            "engine": str(row["ENGINE"] or ""),
        }
        for row in rows
    }


def _target_row_count(connection, table: str) -> int:
    return int(
        connection.exec_driver_sql(
            f"SELECT COUNT(*) FROM {_q_mysql(table)}"
        ).scalar_one()
    )


def _read_control_state(connection) -> dict[str, Any] | None:
    objects = _target_objects(connection)
    if CONTROL_TABLE not in objects:
        return None
    row = connection.exec_driver_sql(
        f"SELECT migration_name, plan_digest, status, expected_json, created_at, updated_at "
        f"FROM {_q_mysql(CONTROL_TABLE)} WHERE migration_name=%s",
        (MIGRATION_NAME,),
    ).mappings().first()
    if not row:
        return None
    expected_raw = row["expected_json"]
    try:
        expected = json.loads(str(expected_raw or "{}"))
    except json.JSONDecodeError as exc:
        raise MigrationError("目标迁移控制表 expected_json 损坏") from exc
    return {
        "migration_name": str(row["migration_name"]),
        "plan_digest": str(row["plan_digest"]),
        "status": str(row["status"]),
        "expected": expected,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _create_control_table(connection) -> None:
    connection.exec_driver_sql(
        f"CREATE TABLE IF NOT EXISTS {_q_mysql(CONTROL_TABLE)} ("
        "migration_name VARCHAR(120) NOT NULL PRIMARY KEY, "
        "plan_digest CHAR(64) NOT NULL, "
        "status VARCHAR(20) NOT NULL, "
        "expected_json LONGTEXT NOT NULL, "
        "created_at DATETIME(6) NOT NULL, "
        "updated_at DATETIME(6) NOT NULL"
        ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )


def _mysql_accounts_present(
    connection, usernames: Iterable[str], *, account_host: str
) -> set[str]:
    present: set[str] = set()
    for username in sorted(set(usernames)):
        row = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM mysql.user WHERE User=%s AND Host=%s",
            (username, account_host),
        ).scalar_one()
        if int(row or 0):
            present.add(username)
    return present


def _drop_empty_unowned_target(
    connection,
    *,
    allowed_tables: set[str],
    allowed_views: set[str],
) -> list[str]:
    objects = _target_objects(connection)
    unexpected = set(objects) - allowed_tables - allowed_views - {CONTROL_TABLE}
    if unexpected:
        raise MigrationError(
            "目标库含迁移契约外对象，拒绝自动清理：" + ", ".join(sorted(unexpected))
        )
    if CONTROL_TABLE in objects:
        raise MigrationError("目标库存在无可识别状态的迁移控制表")
    base_tables = sorted(
        name for name, item in objects.items() if item["type"] == "BASE TABLE"
    )
    nonempty = {
        table: _target_row_count(connection, table)
        for table in base_tables
        if table != CONTROL_TABLE
    }
    nonempty = {table: count for table, count in nonempty.items() if count != 0}
    if nonempty:
        raise MigrationError(
            "目标库已有数据，绝不自动删除："
            + ", ".join(f"{table}={count}" for table, count in sorted(nonempty.items()))
        )
    dropped: list[str] = []
    connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=0")
    try:
        for view in sorted(set(objects) & allowed_views):
            connection.exec_driver_sql(f"DROP VIEW {_q_mysql(view)}")
            dropped.append(view)
        for table in base_tables:
            connection.exec_driver_sql(f"DROP TABLE {_q_mysql(table)}")
            dropped.append(table)
    finally:
        connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=1")
    return dropped


def _drop_owned_running_schema(
    connection,
    *,
    managed_tables: set[str],
    managed_views: set[str],
) -> None:
    """Rebuild owned partial objects while retaining the crash anchor."""
    objects = _target_objects(connection)
    unexpected = set(objects) - managed_tables - managed_views - {CONTROL_TABLE}
    if unexpected:
        raise MigrationError(
            "目标库含迁移契约外对象，拒绝重建：" + ", ".join(sorted(unexpected))
        )
    connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=0")
    try:
        for view in sorted(set(objects) & managed_views):
            connection.exec_driver_sql(f"DROP VIEW {_q_mysql(view)}")
        for table in sorted(set(objects) & managed_tables):
            if objects[table]["type"] != "BASE TABLE":
                raise MigrationError(f"目标对象 {table} 不是基础表")
            connection.exec_driver_sql(f"DROP TABLE {_q_mysql(table)}")
        # Never drop CONTROL_TABLE here.  MySQL DDL auto-commits; this
        # ownership anchor must survive a crash between individual DROP/CREATE
        # statements so the next retry can safely resume.
    finally:
        connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=1")


def _recover_readonly_accounts(connection, settings: ServiceSettings) -> ServiceSettings:
    objects = _target_objects(connection)
    if "data_sources" not in objects:
        return settings
    recovered = dict(settings.readonly_accounts)
    for scenario in SCENARIOS:
        row = connection.exec_driver_sql(
            "SELECT config FROM data_sources WHERE id=%s", (scenario.sql_source_id,)
        ).first()
        if not row:
            continue
        raw = row[0]
        if isinstance(raw, str):
            try:
                config = json.loads(raw)
            except json.JSONDecodeError:
                continue
        else:
            config = raw or {}
        username = str(config.get("user") or "")
        password = str(config.get("password") or "")
        if MYSQL_ACCOUNT_RE.fullmatch(username) and len(password) >= 16:
            recovered[scenario.id] = (username, password)
    return replace(settings, readonly_accounts=recovered)


def _is_safe_stale_running_takeover(
    state: Mapping[str, Any], manifest: Mapping[str, Any]
) -> bool:
    """Allow only an unfinished plan that never published expected results."""
    if str(state.get("plan_digest") or "") == str(
        manifest.get("plan_digest") or ""
    ):
        return False
    if state.get("status") != "running" or state.get("expected") != {}:
        raise MigrationError(
            "目标库已由另一份 manifest 管理；只有 running 且 expected_json={} "
            "的未完成计划可受控接管"
        )
    return True


def _supersede_descriptor(manifest: Mapping[str, Any]) -> dict[str, str] | None:
    if "supersedes" not in manifest:
        return None
    return _validate_supersedes_descriptor(manifest.get("supersedes"))


def _remote_supersede_expected(
    state: Mapping[str, Any] | None, manifest: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    """Return legacy expected data, or None after a successful prior CAS."""
    descriptor = _supersede_descriptor(manifest)
    if descriptor is None:
        raise MigrationError("当前 manifest 未声明 supersedes 恢复")
    if not state:
        raise MigrationError("目标库缺少待 supersede 的旧 v2 控制状态")
    state_digest = str(state.get("plan_digest") or "")
    new_digest = str(manifest.get("plan_digest") or "")
    expected = state.get("expected")
    if state_digest == new_digest:
        status = str(state.get("status") or "")
        if status == "running" and expected == {}:
            return None
        if (
            status in {"executed", "verified"}
            and isinstance(expected, Mapping)
            and str(expected.get("plan_digest") or "") == new_digest
        ):
            return None
        raise MigrationError("新 v3 supersede 控制状态不完整，拒绝继续")
    if state_digest != descriptor["old_plan_digest"]:
        raise MigrationError("远端旧计划 digest 与 supersedes 描述符不匹配")
    if str(state.get("status") or "") != "executed":
        raise MigrationError("supersedes 只允许接管严格的旧 v2 executed 状态")
    if not isinstance(expected, Mapping) or not expected:
        raise MigrationError("远端旧 v2 executed 状态缺少 expected_json")
    if int(expected.get("format_version") or 0) != 2:
        raise MigrationError("远端旧 expected_json 不是 v2 格式")
    if str(expected.get("plan_digest") or "") != descriptor["old_plan_digest"]:
        raise MigrationError("远端旧 expected_json plan_digest 不匹配")
    if _sha256_json(expected) != descriptor["old_expected_sha256"]:
        raise MigrationError("远端旧 expected_json SHA-256 与 supersedes 描述符不匹配")
    return expected


def _cas_superseded_control_to_running(
    connection,
    *,
    manifest: Mapping[str, Any],
    old_expected: Mapping[str, Any],
) -> None:
    descriptor = _supersede_descriptor(manifest)
    if descriptor is None:
        raise MigrationError("不能在无 supersedes 描述符时改写旧控制状态")
    canonical_expected = _canonical_json(old_expected)
    if _sha256_json(old_expected) != descriptor["old_expected_sha256"]:
        raise MigrationError("CAS 前旧 expected_json SHA-256 已变化")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = connection.exec_driver_sql(
        f"UPDATE {_q_mysql(CONTROL_TABLE)} SET "
        "plan_digest=%s, status='running', expected_json='{}', updated_at=%s "
        "WHERE migration_name=%s AND plan_digest=%s AND status='executed' "
        "AND BINARY expected_json=BINARY %s",
        (
            str(manifest["plan_digest"]),
            now,
            MIGRATION_NAME,
            descriptor["old_plan_digest"],
            canonical_expected,
        ),
    )
    if int(result.rowcount or 0) != 1:
        raise MigrationError("旧 v2 executed 控制状态在 supersede CAS 期间发生变化")


def _replace_stale_running_control(
    connection,
    *,
    old_plan_digest: str,
    new_plan_digest: str,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = connection.exec_driver_sql(
        f"UPDATE {_q_mysql(CONTROL_TABLE)} SET "
        "plan_digest=%s, status='running', expected_json='{}', "
        "updated_at=%s WHERE migration_name=%s AND plan_digest=%s "
        "AND status='running' AND expected_json='{}'",
        (new_plan_digest, now, MIGRATION_NAME, old_plan_digest),
    )
    if int(result.rowcount or 0) != 1:
        raise MigrationError("旧 running 迁移状态在接管期间发生变化")


def _business_create_table_ddl(table: str, plan: Mapping[str, Any]) -> str:
    definitions: list[str] = []
    primary_keys: list[str] = []
    for column in plan["columns"]:
        name = str(column["name"])
        definition = f"{_q_mysql(name)} {column['target_type']}"
        if column["role"] == "surrogate":
            definition += " NOT NULL AUTO_INCREMENT"
        elif column.get("nullable", True):
            definition += " NULL"
        else:
            definition += " NOT NULL"
        definitions.append(definition)
        if column.get("primary_key"):
            primary_keys.append(name)
    if primary_keys:
        definitions.append(
            "PRIMARY KEY (" + ", ".join(_q_mysql(item) for item in primary_keys) + ")"
        )
    return (
        f"CREATE TABLE IF NOT EXISTS {_q_mysql(table)} ("
        + ", ".join(definitions)
        + ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
    )


def _create_business_tables(connection, manifest: Mapping[str, Any]) -> None:
    for scenario in SCENARIOS:
        source = manifest["source"]["business"][scenario.id]
        for table, item in source["tables"].items():
            connection.exec_driver_sql(_business_create_table_ddl(table, item["plan"]))


def _verify_innodb_engines(connection, required_tables: Iterable[str]) -> None:
    objects = _target_objects(connection)
    bad: list[str] = []
    for table in sorted(set(required_tables)):
        item = objects.get(table)
        if not item:
            bad.append(f"{table}:missing")
        elif item["type"] != "BASE TABLE":
            bad.append(f"{table}:{item['type']}")
        elif item["engine"].lower() != "innodb":
            bad.append(f"{table}:{item['engine'] or 'none'}")
    if bad:
        raise MigrationError("目标表未全部使用 InnoDB：" + ", ".join(bad))


def _metadata_datetime_columns(metadata) -> dict[tuple[str, str], Any]:
    result: dict[tuple[str, str], Any] = {}
    for table in metadata.tables.values():
        for column in table.columns:
            type_name = str(column.type).split("(", 1)[0].upper()
            if type_name in {"DATETIME", "TIMESTAMP"}:
                result[(table.name, column.name)] = column
    return result


def _read_datetime_precisions(
    connection, required: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], int]:
    required_set = set(required)
    rows = connection.exec_driver_sql(
        "SELECT TABLE_NAME, COLUMN_NAME, DATETIME_PRECISION "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
        "AND DATA_TYPE IN ('datetime','timestamp')",
        (connection.engine.url.database,),
    ).mappings()
    return {
        (str(row["TABLE_NAME"]), str(row["COLUMN_NAME"])): int(
            row["DATETIME_PRECISION"] or 0
        )
        for row in rows
        if (str(row["TABLE_NAME"]), str(row["COLUMN_NAME"])) in required_set
    }


def _ensure_platform_datetime_precision(connection, metadata) -> None:
    """Create/upgrade every ORM temporal column without discarding micros."""
    columns = _metadata_datetime_columns(metadata)
    expected = {
        key: int(getattr(column.type, "fsp", 0) or 0)
        for key, column in columns.items()
    }
    bad_contract = {
        key: precision
        for key, precision in expected.items()
        if precision != MYSQL_DATETIME_PRECISION
    }
    if bad_contract:
        raise MigrationError("迁移 ORM metadata 未统一使用 DATETIME(6)")

    actual = _read_datetime_precisions(connection, columns)
    missing = set(columns) - set(actual)
    if missing:
        raise MigrationError(
            "目标缺少 ORM 时间列："
            + ", ".join(f"{table}.{column}" for table, column in sorted(missing))
        )
    try:
        from sqlalchemy.dialects.mysql import pymysql
        from sqlalchemy.schema import CreateColumn
    except ImportError as exc:  # pragma: no cover
        raise MigrationError("迁移需要 SQLAlchemy MySQL dialect") from exc
    dialect = pymysql.dialect()
    for key in sorted(columns):
        if actual[key] == MYSQL_DATETIME_PRECISION:
            continue
        table_name, _column_name = key
        definition = str(CreateColumn(columns[key]).compile(dialect=dialect))
        connection.exec_driver_sql(
            f"ALTER TABLE {_q_mysql(table_name)} MODIFY COLUMN {definition}"
        )

    verified = _read_datetime_precisions(connection, columns)
    wrong = {
        key: verified.get(key)
        for key in columns
        if verified.get(key) != MYSQL_DATETIME_PRECISION
    }
    if wrong:
        raise MigrationError(
            "目标 ORM 时间列未全部升级为 DATETIME(6)："
            + ", ".join(
                f"{table}.{column}={precision}"
                for (table, column), precision in sorted(wrong.items())
            )
        )


def _prepare_target_schema(
    engine,
    platform_db: Path,
    manifest: Mapping[str, Any],
    settings: ServiceSettings,
) -> tuple[Any, dict[str, Any] | None, ServiceSettings, set[str]]:
    business_tables = set(BOOKKEEPING_TABLES) | set(MEDICAL_TABLES)
    business_views = set(BOOKKEEPING_VIEWS) | set(MEDICAL_VIEWS)
    managed_tables = set(PLATFORM_TABLES) | business_tables
    with engine.begin() as connection:
        connection.exec_driver_sql("SET SESSION default_storage_engine=InnoDB")
        state = _read_control_state(connection)
        stale_takeover = bool(
            state and _is_safe_stale_running_takeover(state, manifest)
        )
        settings = _recover_readonly_accounts(connection, settings) if state else settings
        existing_accounts = _mysql_accounts_present(
            connection,
            (
                *[account[0] for account in settings.readonly_accounts.values()],
                settings.mysql_runtime_user,
            ),
            account_host=settings.mysql_account_host,
        )
        if not state and existing_accounts:
            raise MigrationError(
                "迁移专用账号已存在但不属于本迁移，拒绝接管："
                + ", ".join(sorted(existing_accounts))
            )
        if not state:
            _drop_empty_unowned_target(
                connection,
                allowed_tables=managed_tables,
                allowed_views=business_views,
            )
        elif state["status"] == "running":
            # The control row proves ownership by this exact manifest.  A
            # previous interrupted attempt can have non-empty or MyISAM
            # partial tables, so rebuild the owned schema instead of trying to
            # continue into an engine/schema mixture.  No object outside the
            # fixed contract is ever dropped.
            _drop_owned_running_schema(
                connection,
                managed_tables=managed_tables,
                managed_views=business_views,
            )
            if stale_takeover:
                # Keep the InnoDB control table as a crash-safe ownership
                # anchor, but atomically replace the obsolete running row.
                # This avoids the unrecoverable window created by dropping the
                # table between MySQL auto-committing DDL statements.
                _replace_stale_running_control(
                    connection,
                    old_plan_digest=str(state["plan_digest"]),
                    new_plan_digest=str(manifest["plan_digest"]),
                )
        elif state["status"] in {"executed", "verified"}:
            return None, state, settings, existing_accounts
        else:
            raise MigrationError(f"目标迁移状态无效：{state['status']}")

    metadata = _source_platform_metadata(platform_db)
    metadata.create_all(bind=engine, checkfirst=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("SET SESSION default_storage_engine=InnoDB")
        _ensure_platform_datetime_precision(connection, metadata)
        _create_business_tables(connection, manifest)
        _create_control_table(connection)
        state = _read_control_state(connection)
        if state and state["plan_digest"] != manifest["plan_digest"]:
            raise MigrationError("目标迁移状态在建表期间发生冲突")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        connection.exec_driver_sql(
            f"INSERT INTO {_q_mysql(CONTROL_TABLE)} "
            "(migration_name,plan_digest,status,expected_json,created_at,updated_at) "
            "VALUES (%s,%s,'running','{}',%s,%s) "
            "ON DUPLICATE KEY UPDATE status='running', expected_json='{}', updated_at=VALUES(updated_at)",
            (MIGRATION_NAME, manifest["plan_digest"], now, now),
        )
        _verify_innodb_engines(connection, managed_tables | {CONTROL_TABLE})
    return metadata, None, settings, existing_accounts


def _coerce_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise MigrationError(f"平台时间值无效：{value!r}") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _coerce_platform_value(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    normalized = str(type_name or "").upper()
    if "JSON" in normalized:
        return _json_load(value, default={})
    if "BOOL" in normalized:
        return bool(value)
    if "DATE" in normalized or "TIME" in normalized:
        return _coerce_datetime(value)
    if "INT" in normalized:
        return int(value)
    if any(item in normalized for item in ("REAL", "FLOAT", "DOUBLE")):
        return float(value)
    return value


def _insert_platform_rows(
    engine,
    metadata,
    platform: Mapping[str, Any],
    transformed: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    batch_size: int = 500,
) -> None:
    schemas = _platform_target_schemas(platform)
    with engine.begin() as connection:
        connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=0")
        try:
            for table_name in PLATFORM_TABLES:
                table = metadata.tables[table_name]
                columns = [column.name for column in table.columns]
                types = schemas[table_name]["column_types"]
                prepared = [
                    {
                        column: _coerce_platform_value(row.get(column), types.get(column, ""))
                        for column in columns
                    }
                    for row in transformed[table_name]
                ]
                for start in range(0, len(prepared), batch_size):
                    connection.execute(table.insert(), prepared[start : start + batch_size])
        finally:
            connection.exec_driver_sql("SET SESSION FOREIGN_KEY_CHECKS=1")


def _insert_business_tables(
    engine,
    paths: RuntimePaths,
    manifest: Mapping[str, Any],
    *,
    batch_size: int = 1000,
) -> None:
    for scenario in SCENARIOS:
        business = manifest["source"]["business"][scenario.id]
        source_path = paths.data_root / scenario.sqlite_filename
        if (
            source_path.stat().st_size != int(business["file_size"])
            or _sha256_file(source_path) != str(business["file_sha256"])
        ):
            raise MigrationError(f"导入前 {scenario.name} SQLite 已变化")
        with open_sqlite_readonly(source_path) as source_connection:
            for table, item in business["tables"].items():
                plan = item["plan"]
                columns = [str(column["name"]) for column in plan["columns"]]
                placeholders = ",".join("%s" for _ in columns)
                statement = (
                    f"INSERT INTO {_q_mysql(table)} "
                    f"({', '.join(_q_mysql(column) for column in columns)}) "
                    f"VALUES ({placeholders})"
                )
                with engine.begin() as target_connection:
                    batch: list[tuple[Any, ...]] = []
                    for ordinal, source_row in enumerate(
                        _source_row_iterator(source_connection, table, plan), start=1
                    ):
                        transformed = _transform_business_row(source_row, plan, ordinal)
                        batch.append(tuple(transformed[column] for column in columns))
                        if len(batch) >= batch_size:
                            target_connection.exec_driver_sql(statement, batch)
                            batch.clear()
                    if batch:
                        target_connection.exec_driver_sql(statement, batch)
        if (
            source_path.stat().st_size != int(business["file_size"])
            or _sha256_file(source_path) != str(business["file_sha256"])
        ):
            raise MigrationError(f"导入期间 {scenario.name} SQLite 发生变化")


def _create_business_indexes_and_views(engine, manifest: Mapping[str, Any]) -> None:
    with engine.begin() as connection:
        for scenario in SCENARIOS:
            business = manifest["source"]["business"][scenario.id]
            for table, item in business["tables"].items():
                existing = {
                    str(row[0])
                    for row in connection.exec_driver_sql(
                        "SELECT INDEX_NAME FROM information_schema.STATISTICS "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                        (connection.engine.url.database, table),
                    )
                }
                for index in item["plan"]["indexes"]:
                    if index["name"] in existing:
                        continue
                    connection.exec_driver_sql(
                        f"CREATE INDEX {_q_mysql(index['name'])} ON {_q_mysql(table)} "
                        f"({', '.join(_q_mysql(column) for column in index['columns'])})"
                    )
            for _view, view_item in business["views"].items():
                connection.exec_driver_sql(str(view_item["target_ddl"]))


def _target_descriptor_from_settings(
    settings: ServiceSettings, *, minio_versioning: str
) -> dict[str, Any]:
    return _redacted_target_descriptor(
        mysql_host=settings.mysql_host,
        mysql_port=settings.mysql_port,
        mysql_database=settings.mysql_database,
        minio_endpoint=settings.minio_endpoint,
        minio_bucket=settings.minio_bucket,
        minio_prefix=settings.minio_prefix,
        minio_secure=settings.minio_secure,
        minio_versioning=minio_versioning,
        mysql_runtime_user=settings.mysql_runtime_user,
        mysql_account_host=settings.mysql_account_host,
        readonly_users={
            scenario.id: settings.readonly_accounts[scenario.id][0]
            for scenario in SCENARIOS
        },
    )


def _assert_target_settings(
    manifest: Mapping[str, Any], settings: ServiceSettings
) -> None:
    target = manifest.get("target")
    if not isinstance(target, Mapping) or not isinstance(
        target.get("minio"), Mapping
    ):
        raise MigrationError("manifest 缺少 MinIO 目标契约")
    minio_versioning = _normalize_minio_versioning_capability(
        target["minio"].get("versioning")
    )
    if (
        _target_descriptor_from_settings(
            settings, minio_versioning=minio_versioning
        )
        != target
    ):
        raise MigrationError(
            "当前 MySQL/MinIO 目标与 dry-run manifest 不一致；必须重新 dry-run"
        )


def _new_minio_client(
    *, endpoint: str, access_key: str, secret_key: str, secure: bool
):
    try:
        from minio import Minio
    except ImportError as exc:  # pragma: no cover - declared dependency.
        raise MigrationError("迁移需要 minio Python SDK") from exc
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=secure,
    )


def _minio_client(settings: ServiceSettings):
    return _new_minio_client(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _is_versioning_not_implemented(exc: Exception) -> bool:
    if isinstance(exc, NotImplementedError):
        return True
    code = re.sub(
        r"[^a-z0-9]", "", str(getattr(exc, "code", "") or "").lower()
    )
    if "notimplemented" in code:
        return True
    status_candidates = (
        getattr(exc, "status", None),
        getattr(getattr(exc, "response", None), "status", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    )
    return any(str(status or "") == "501" for status in status_candidates)


def _probe_minio_versioning(client, bucket: str, *, prefix: str = "") -> str:
    """Read bucket capability without mutating bucket-level configuration."""
    try:
        current = client.get_bucket_versioning(bucket)
        status = str(getattr(current, "status", "") or "")
    except Exception as exc:
        if _is_versioning_not_implemented(exc):
            return MINIO_VERSIONING_UNSUPPORTED
        raise MigrationError("无法检查 MinIO bucket 版本控制能力") from exc
    if status.lower() == MINIO_VERSIONING_ENABLED.lower():
        return MINIO_VERSIONING_ENABLED
    # A successful GET does not prove that an S3 gateway implements version
    # listing.  Exercise the read-only API that verify will rely on; some
    # gateways return an empty status here but NotImplemented for versions.
    try:
        listed = client.list_objects(
            bucket,
            prefix=str(prefix or "").strip("/"),
            recursive=True,
            include_version=True,
        )
        next(iter(listed), None)
    except Exception as exc:
        if _is_versioning_not_implemented(exc):
            return MINIO_VERSIONING_UNSUPPORTED
        raise MigrationError("无法探测 MinIO 对象版本列举能力") from exc
    return MINIO_VERSIONING_SUPPORTED


def _assert_minio_versioning_capability(
    client, bucket: str, expected: Any, *, prefix: str = ""
) -> str:
    expected_capability = _normalize_minio_versioning_capability(expected)
    actual_capability = _probe_minio_versioning(
        client, bucket, prefix=prefix
    )
    if actual_capability != expected_capability:
        raise MigrationError(
            "MinIO bucket versioning 能力与 dry-run manifest 不一致："
            f"{actual_capability} != {expected_capability}"
        )
    return actual_capability


def _stat_minio_object(
    client,
    bucket: str,
    object_key: str,
    *,
    version_id: str = "",
):
    try:
        kwargs = {"version_id": version_id} if version_id else {}
        return client.stat_object(bucket, object_key, **kwargs)
    except Exception as exc:
        code = str(getattr(exc, "code", ""))
        if code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "NoSuchVersion"}:
            return None
        raise MigrationError(f"无法检查 MinIO 对象 {object_key}") from exc


def _metadata_sha256(stat: Any) -> str:
    metadata = getattr(stat, "metadata", None) or {}
    normalized = {str(key).lower(): str(value) for key, value in metadata.items()}
    for key in ("sha256", "x-amz-meta-sha256", "x-minio-meta-sha256"):
        digest = normalized.get(key, "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            return digest
    return ""


def _normalize_object_version_id(value: Any) -> str:
    resolved = str(value or "")
    return "" if resolved.lower() == "null" else resolved


def _downloaded_object_sha256(
    client,
    bucket: str,
    object_key: str,
    *,
    version_id: str = "",
) -> str:
    kwargs = {"version_id": version_id} if version_id else {}
    response = client.get_object(bucket, object_key, **kwargs)
    digest = hashlib.sha256()
    try:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        response.close()
        response.release_conn()
    return digest.hexdigest()


def _assert_minio_object(
    client,
    *,
    bucket: str,
    object_key: str,
    expected_size: int,
    expected_sha256: str,
    expected_version_id: str = "",
    expected_etag: str = "",
    stat: Any | None = None,
) -> Any:
    resolved_stat = stat or _stat_minio_object(
        client,
        bucket,
        object_key,
        version_id=expected_version_id,
    )
    if resolved_stat is None:
        raise MigrationError(f"MinIO 对象不存在：{object_key}")
    actual_size = int(getattr(resolved_stat, "size", -1))
    if actual_size != int(expected_size):
        raise MigrationError(
            f"MinIO 对象大小不匹配：{object_key}，{actual_size} != {expected_size}"
        )
    actual_version = _normalize_object_version_id(
        getattr(resolved_stat, "version_id", "")
    )
    if expected_version_id and actual_version != expected_version_id:
        raise MigrationError(f"MinIO 对象版本不匹配：{object_key}")
    actual_etag = str(getattr(resolved_stat, "etag", "") or "").strip('"')
    if expected_etag and actual_etag != expected_etag.strip('"'):
        raise MigrationError(f"MinIO 对象 ETag 不匹配：{object_key}")
    metadata_sha = _metadata_sha256(resolved_stat)
    if metadata_sha and metadata_sha != str(expected_sha256).lower():
        raise MigrationError(f"MinIO 对象元数据 SHA-256 不匹配：{object_key}")
    # User metadata is supplied by the uploader and is not a server-computed
    # checksum.  Always hash the stored bytes (at the exact recorded version)
    # before certifying or deleting the local source.
    actual_sha = _downloaded_object_sha256(
        client,
        bucket,
        object_key,
        version_id=expected_version_id,
    )
    if actual_sha != str(expected_sha256).lower():
        raise MigrationError(f"MinIO 对象 SHA-256 不匹配：{object_key}")
    return resolved_stat


def _upload_files_to_minio(
    client,
    settings: ServiceSettings,
    manifest: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    target_minio = manifest.get("target", {}).get("minio", {})
    capability = _normalize_minio_versioning_capability(
        target_minio.get("versioning")
    )
    if target_minio.get("object_key_strategy") != MINIO_OBJECT_KEY_STRATEGY:
        raise MigrationError("manifest 的 MinIO object key 策略不受支持")
    source_files = list(manifest["source"]["files"])
    _assert_non_reusable_object_keys(source_files)
    try:
        bucket_exists = bool(client.bucket_exists(settings.minio_bucket))
    except Exception as exc:
        raise MigrationError("无法检查 MinIO bucket") from exc
    if not bucket_exists:
        try:
            client.make_bucket(settings.minio_bucket)
        except Exception as exc:
            raise MigrationError("无法创建 MinIO bucket") from exc
    _assert_minio_versioning_capability(
        client,
        settings.minio_bucket,
        capability,
        prefix=settings.minio_prefix,
    )

    uploaded: dict[str, dict[str, Any]] = {}
    for item in source_files:
        source = Path(str(item["source_path"])).resolve(strict=True)
        if source.stat().st_size != int(item["size"]):
            raise MigrationError(f"待上传文件大小已变化：{source}")
        if _sha256_file(source) != str(item["sha256"]):
            raise MigrationError(f"待上传文件内容已变化：{source}")
        bucket = str(item["bucket_name"])
        object_key = str(item["object_key"])
        if bucket != settings.minio_bucket:
            raise MigrationError("manifest 中的 MinIO bucket 与当前配置不一致")
        expected_url = build_object_url(
            endpoint=settings.minio_endpoint,
            secure=settings.minio_secure,
            bucket=bucket,
            object_key=object_key,
        )
        if expected_url != str(item["object_url"]):
            raise MigrationError(f"manifest 中的对象 URL 不符合稳定定位规范：{object_key}")

        stat = _stat_minio_object(client, bucket, object_key)
        if stat is None:
            try:
                with source.open("rb") as handle:
                    client.put_object(
                        bucket,
                        object_key,
                        handle,
                        source.stat().st_size,
                        content_type=str(item.get("mime") or "application/octet-stream"),
                        metadata={
                            "sha256": str(item["sha256"]),
                            "file-id": str(item["file_id"]),
                            "scenario-id": str(item["scenario_id"]),
                        },
                    )
            except Exception as exc:
                raise MigrationError(f"上传 MinIO 对象失败：{object_key}") from exc
            stat = _stat_minio_object(client, bucket, object_key)
        if stat is None:
            raise MigrationError(f"上传后 MinIO 对象不存在：{object_key}")
        # Existing objects are never overwritten.  A collision is a hard
        # failure unless both size and SHA-256 prove byte identity.
        resolved_version_id = (
            ""
            if capability == MINIO_VERSIONING_UNSUPPORTED
            else _normalize_object_version_id(getattr(stat, "version_id", ""))
        )
        if capability == MINIO_VERSIONING_ENABLED and not resolved_version_id:
            raise MigrationError(f"MinIO 对象缺少不可变 version_id：{object_key}")
        stat = _assert_minio_object(
            client,
            bucket=bucket,
            object_key=object_key,
            expected_size=int(item["size"]),
            expected_sha256=str(item["sha256"]),
            expected_version_id=resolved_version_id,
            stat=stat,
        )
        etag = str(getattr(stat, "etag", "") or "").strip('"')
        if not etag:
            raise MigrationError(f"MinIO 对象缺少 ETag：{object_key}")
        uploaded[str(item["file_id"])] = {
            **dict(item),
            "etag": etag,
            "object_version_id": resolved_version_id,
            "object_url": expected_url,
        }
    if len(uploaded) != EXPECTED_BUCKET_FILE_COUNT:
        raise MigrationError("MinIO 上传结果不是固定的 41 个文件")
    _verify_minio_files(
        client,
        {
            "files": list(uploaded.values()),
            "minio": {
                "versioning": capability,
                "object_key_strategy": MINIO_OBJECT_KEY_STRATEGY,
                "prefix": settings.minio_prefix,
            },
        },
    )
    return uploaded


def _scenario_business_objects(scenario_id: str) -> tuple[str, ...]:
    if scenario_id == BOOKKEEPING_SCENARIO_ID:
        return (*BOOKKEEPING_TABLES, *BOOKKEEPING_VIEWS)
    if scenario_id == MEDICAL_SCENARIO_ID:
        return (*MEDICAL_TABLES, *MEDICAL_VIEWS)
    raise MigrationError(f"未知场景：{scenario_id}")


def _mysql_account_parts(username: str, account_host: str) -> tuple[str, str]:
    if not MYSQL_ACCOUNT_RE.fullmatch(username):
        raise MigrationError("MySQL 专用账号名称无效")
    if not MYSQL_ACCOUNT_HOST_RE.fullmatch(account_host):
        raise MigrationError("MySQL 专用账号 host 无效")
    return username, account_host


def _mysql_account(username: str, account_host: str) -> str:
    """Return a validated account literal escaped for PyMySQL formatting."""
    username, account_host = _mysql_account_parts(username, account_host)
    # SQLAlchemy's MySQL dialect calls cursor.execute(statement, ()) even for
    # exec_driver_sql statements without caller parameters.  PyMySQL therefore
    # applies percent formatting; double literal wildcards so the server sees
    # one ``%`` in GRANT/REVOKE/SHOW/DROP account specifications.
    escaped_host = account_host.replace("%", "%%")
    return f"'{username}'@'{escaped_host}'"


def _set_mysql_account_password(
    connection,
    *,
    create: bool,
    username: str,
    account_host: str,
    password: str,
) -> None:
    """Create/alter an account with every value bound through the DBAPI."""
    username, account_host = _mysql_account_parts(username, account_host)
    operation = "CREATE" if create else "ALTER"
    connection.exec_driver_sql(
        f"{operation} USER %s@%s IDENTIFIED BY %s",
        (username, account_host, password),
    )


def _configure_readonly_accounts(
    engine,
    settings: ServiceSettings,
    *,
    existing_accounts: set[str],
) -> None:
    database = _q_mysql(settings.mysql_database)
    with engine.begin() as connection:
        for scenario in SCENARIOS:
            username, password = settings.readonly_accounts[scenario.id]
            account = _mysql_account(username, settings.mysql_account_host)
            if username in existing_accounts:
                _set_mysql_account_password(
                    connection,
                    create=False,
                    username=username,
                    account_host=settings.mysql_account_host,
                    password=password,
                )
            else:
                _set_mysql_account_password(
                    connection,
                    create=True,
                    username=username,
                    account_host=settings.mysql_account_host,
                    password=password,
                )
                existing_accounts.add(username)
            connection.exec_driver_sql(
                f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}"
            )
            for object_name in _scenario_business_objects(scenario.id):
                connection.exec_driver_sql(
                    f"GRANT SELECT ON {database}.{_q_mysql(object_name)} TO {account}"
                )


def _configure_runtime_account(
    engine,
    settings: ServiceSettings,
    *,
    existing_accounts: set[str],
) -> None:
    username = settings.mysql_runtime_user
    password = settings.mysql_runtime_password
    account = _mysql_account(username, settings.mysql_account_host)
    privileges = ", ".join(sorted(RUNTIME_MYSQL_PRIVILEGES))
    with engine.begin() as connection:
        if username in existing_accounts:
            _set_mysql_account_password(
                connection,
                create=False,
                username=username,
                account_host=settings.mysql_account_host,
                password=password,
            )
        else:
            _set_mysql_account_password(
                connection,
                create=True,
                username=username,
                account_host=settings.mysql_account_host,
                password=password,
            )
            existing_accounts.add(username)
        connection.exec_driver_sql(
            f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM {account}"
        )
        connection.exec_driver_sql(
            f"GRANT {privileges} ON {_q_mysql(settings.mysql_database)}.* TO {account}"
        )


def _account_grantee(username: str, account_host: str) -> str:
    username, account_host = _mysql_account_parts(username, account_host)
    # This is a bound information_schema value, not SQL source text.  Keep the
    # logical single-percent account spelling.
    return f"'{username}'@'{account_host}'"


def _readonly_grants(connection, settings: ServiceSettings, username: str) -> set[tuple[str, str]]:
    grantee = _account_grantee(username, settings.mysql_account_host)
    rows = connection.exec_driver_sql(
        "SELECT TABLE_SCHEMA, TABLE_NAME, PRIVILEGE_TYPE "
        "FROM information_schema.TABLE_PRIVILEGES WHERE GRANTEE=%s",
        (grantee,),
    )
    return {
        (f"{row[0]}.{row[1]}", str(row[2]).upper())
        for row in rows
    }


def _verify_readonly_accounts(engine, settings: ServiceSettings) -> None:
    with engine.connect() as connection:
        for scenario in SCENARIOS:
            username, _password = settings.readonly_accounts[scenario.id]
            expected = {
                (f"{settings.mysql_database}.{object_name}", "SELECT")
                for object_name in _scenario_business_objects(scenario.id)
            }
            actual = _readonly_grants(connection, settings, username)
            if actual != expected:
                raise MigrationError(
                    f"专用账号 {username} 的表级权限不符合最小 SELECT 契约"
                )
            schema_grants = int(
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM information_schema.SCHEMA_PRIVILEGES "
                    "WHERE GRANTEE=%s",
                    (
                        _account_grantee(
                            username, settings.mysql_account_host
                        ),
                    ),
                ).scalar_one()
                or 0
            )
            if schema_grants:
                raise MigrationError(f"专用账号 {username} 意外拥有库级权限")
            global_grants = int(
                connection.exec_driver_sql(
                    "SELECT COUNT(*) FROM information_schema.USER_PRIVILEGES "
                    "WHERE GRANTEE=%s AND PRIVILEGE_TYPE <> 'USAGE'",
                    (
                        _account_grantee(
                            username, settings.mysql_account_host
                        ),
                    ),
                ).scalar_one()
                or 0
            )
            if global_grants:
                raise MigrationError(f"专用账号 {username} 意外拥有全局权限")

    try:
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError as exc:  # pragma: no cover
        raise MigrationError("verify 需要 SQLAlchemy") from exc
    for scenario in SCENARIOS:
        username, password = settings.readonly_accounts[scenario.id]
        readonly_engine = _mysql_engine(settings, user=username, password=password)
        try:
            with readonly_engine.connect() as connection:
                for object_name in _scenario_business_objects(scenario.id):
                    connection.exec_driver_sql(
                        f"SELECT * FROM {_q_mysql(object_name)} LIMIT 0"
                    )
                forbidden = ["business_scenarios"]
                forbidden.append(
                    "就诊表"
                    if scenario.id == BOOKKEEPING_SCENARIO_ID
                    else "accounts"
                )
                for object_name in forbidden:
                    try:
                        connection.exec_driver_sql(
                            f"SELECT * FROM {_q_mysql(object_name)} LIMIT 0"
                        )
                    except SQLAlchemyError:
                        continue
                    raise MigrationError(
                        f"专用账号 {username} 可越权读取 {object_name}"
                    )
        finally:
            readonly_engine.dispose()


def _verify_runtime_account(engine, settings: ServiceSettings) -> None:
    grantee = _account_grantee(
        settings.mysql_runtime_user, settings.mysql_account_host
    )
    with engine.connect() as connection:
        schema_privileges = {
            str(row[0]).upper()
            for row in connection.exec_driver_sql(
                "SELECT PRIVILEGE_TYPE FROM information_schema.SCHEMA_PRIVILEGES "
                "WHERE GRANTEE=%s AND TABLE_SCHEMA=%s",
                (grantee, settings.mysql_database),
            )
        }
        if schema_privileges != set(RUNTIME_MYSQL_PRIVILEGES):
            raise MigrationError("MySQL 运行账号的目标库权限不符合固定最小契约")
        other_schema_grants = int(
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM information_schema.SCHEMA_PRIVILEGES "
                "WHERE GRANTEE=%s AND TABLE_SCHEMA<>%s",
                (grantee, settings.mysql_database),
            ).scalar_one()
            or 0
        )
        table_grants = int(
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM information_schema.TABLE_PRIVILEGES "
                "WHERE GRANTEE=%s",
                (grantee,),
            ).scalar_one()
            or 0
        )
        global_grants = int(
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM information_schema.USER_PRIVILEGES "
                "WHERE GRANTEE=%s AND PRIVILEGE_TYPE<>'USAGE'",
                (grantee,),
            ).scalar_one()
            or 0
        )
        if other_schema_grants or table_grants or global_grants:
            raise MigrationError("MySQL 运行账号拥有目标库之外的权限")

    try:
        from sqlalchemy.exc import SQLAlchemyError
    except ImportError as exc:  # pragma: no cover
        raise MigrationError("verify 需要 SQLAlchemy") from exc
    runtime_engine = _mysql_engine(
        settings,
        user=settings.mysql_runtime_user,
        password=settings.mysql_runtime_password,
    )
    probe_user = f"ontology_probe_{secrets.token_hex(6)}"
    create_user_succeeded = False
    try:
        with runtime_engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TEMPORARY TABLE `__ontology_runtime_permission_probe` ("
                "id INTEGER NOT NULL PRIMARY KEY, value VARCHAR(20) NOT NULL"
                ") ENGINE=InnoDB"
            )
            connection.exec_driver_sql(
                "INSERT INTO `__ontology_runtime_permission_probe` VALUES (1,'a')"
            )
            connection.exec_driver_sql(
                "UPDATE `__ontology_runtime_permission_probe` SET value='b' WHERE id=1"
            )
            if connection.exec_driver_sql(
                "SELECT value FROM `__ontology_runtime_permission_probe` WHERE id=1"
            ).scalar_one() != "b":
                raise MigrationError("MySQL 运行账号 CRUD 能力验证失败")
            connection.exec_driver_sql(
                "DELETE FROM `__ontology_runtime_permission_probe` WHERE id=1"
            )
            connection.exec_driver_sql(
                "DROP TEMPORARY TABLE `__ontology_runtime_permission_probe`"
            )
            try:
                connection.exec_driver_sql("SELECT User FROM mysql.user LIMIT 0")
            except SQLAlchemyError:
                pass
            else:
                raise MigrationError("MySQL 运行账号可越权读取 mysql.user")
            try:
                _set_mysql_account_password(
                    connection,
                    create=True,
                    username=probe_user,
                    account_host="localhost",
                    password=secrets.token_urlsafe(24),
                )
                create_user_succeeded = True
            except SQLAlchemyError:
                pass
            if create_user_succeeded:
                raise MigrationError("MySQL 运行账号意外拥有 CREATE USER 权限")
    finally:
        runtime_engine.dispose()
        if create_user_succeeded:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "DROP USER IF EXISTS "
                    + _mysql_account(probe_user, "localhost")
                )


def _atomic_update_runtime_env(path: Path, settings: ServiceSettings) -> None:
    if not path.is_file():
        raise MigrationError(f"不能原子切换不存在的运行配置文件：{path}")
    original = path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in original else "\n"
    replacements = {
        "ANNUAL_MYSQL_USER": settings.mysql_runtime_user,
        "ANNUAL_MYSQL_PASSWORD": settings.mysql_runtime_password,
    }
    seen: set[str] = set()
    output: list[str] = []
    for line in original.splitlines():
        match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        key = match.group(1) if match else ""
        if key in replacements:
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key in ("ANNUAL_MYSQL_USER", "ANNUAL_MYSQL_PASSWORD"):
        if key not in seen:
            output.append(f"{key}={replacements[key]}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(newline.join(output) + newline)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _require_separate_admin(settings: ServiceSettings) -> None:
    if settings.mysql_admin_user == settings.mysql_runtime_user:
        raise MigrationError(
            "ANNUAL_MYSQL_USER 已是运行账号；迁移阶段必须通过 "
            "MIGRATION_MYSQL_ADMIN_USER/PASSWORD 提供一次性管理员凭据"
        )


def _build_target_expected(
    platform: Mapping[str, Any],
    transformed: Mapping[str, Sequence[Mapping[str, Any]]],
    manifest: Mapping[str, Any],
    uploaded_files: Mapping[str, Mapping[str, Any]],
    settings: ServiceSettings,
) -> dict[str, Any]:
    business: dict[str, Any] = {}
    for scenario in SCENARIOS:
        source = manifest["source"]["business"][scenario.id]
        tables: dict[str, Any] = {}
        for table, item in source["tables"].items():
            plan = item["plan"]
            columns = [str(column["name"]) for column in plan["columns"]]
            order_columns = [
                str(column["name"])
                for column in plan["columns"]
                if column.get("primary_key")
            ]
            tables[table] = {
                "row_count": int(item["row_count"]),
                "row_sha256": str(item["target_row_sha256"]),
                "columns": columns,
                "column_types": _target_column_types(plan),
                "order_columns": order_columns,
                "indexes": copy.deepcopy(plan.get("indexes", [])),
            }
        views = {
            view: {
                "row_count": int(item["row_count"]),
                "row_sha256": str(item["target_row_sha256"]),
                "columns": list(item["columns"]),
                "column_types": dict(item["column_types"]),
                "target_ddl_sha256": str(item["target_ddl_sha256"]),
            }
            for view, item in source["views"].items()
        }
        business[scenario.id] = {"tables": tables, "views": views}

    files = []
    for file_id, item in sorted(uploaded_files.items()):
        files.append(
            {
                "file_id": file_id,
                "scenario_id": str(item["scenario_id"]),
                "bucket_name": str(item["bucket_name"]),
                "object_key": str(item["object_key"]),
                "object_url": str(item["object_url"]),
                "size": int(item["size"]),
                "sha256": str(item["sha256"]),
                "etag": str(item.get("etag") or ""),
                "object_version_id": str(item.get("object_version_id") or ""),
            }
        )
    return {
        "format_version": MANIFEST_FORMAT_VERSION,
        "plan_digest": str(manifest["plan_digest"]),
        "platform": platform_expected_manifest(platform, transformed),
        "business": business,
        "files": files,
        "minio": {
            "versioning": _normalize_minio_versioning_capability(
                manifest["target"]["minio"]["versioning"]
            ),
            "object_key_strategy": MINIO_OBJECT_KEY_STRATEGY,
            "prefix": str(manifest["target"]["minio"]["prefix"]),
        },
        "base_tables": sorted(
            {*PLATFORM_TABLES, *BOOKKEEPING_TABLES, *MEDICAL_TABLES, CONTROL_TABLE}
        ),
        "views": sorted({*BOOKKEEPING_VIEWS, *MEDICAL_VIEWS}),
        "readonly_accounts": {
            scenario.id: {
                "username": settings.readonly_accounts[scenario.id][0],
                "objects": list(_scenario_business_objects(scenario.id)),
            }
            for scenario in SCENARIOS
        },
        "runtime_account": {
            "username": settings.mysql_runtime_user,
            "account_host": settings.mysql_account_host,
            "database": settings.mysql_database,
            "privileges": sorted(RUNTIME_MYSQL_PRIVILEGES),
        },
    }


def _write_control_status(
    connection,
    *,
    manifest: Mapping[str, Any],
    status: str,
    expected: Mapping[str, Any],
) -> None:
    if status not in {"running", "executed", "verified"}:
        raise MigrationError(f"不能写入未知迁移状态：{status}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    result = connection.exec_driver_sql(
        f"UPDATE {_q_mysql(CONTROL_TABLE)} "
        "SET status=%s, expected_json=%s, updated_at=%s "
        "WHERE migration_name=%s AND plan_digest=%s",
        (
            status,
            _canonical_json(expected),
            now,
            MIGRATION_NAME,
            str(manifest["plan_digest"]),
        ),
    )
    if int(result.rowcount or 0) != 1:
        raise MigrationError("无法更新目标迁移控制状态")


def _mysql_rows(
    connection,
    table: str,
    columns: Sequence[str],
    order_columns: Sequence[str],
) -> Iterator[Mapping[str, Any]]:
    select_columns = ", ".join(_q_mysql(column) for column in columns)
    sql = f"SELECT {select_columns} FROM {_q_mysql(table)}"
    if order_columns:
        sql += " ORDER BY " + ", ".join(_q_mysql(column) for column in order_columns)
    result = connection.execution_options(stream_results=True).exec_driver_sql(sql).mappings()
    for row in result:
        yield row


def _verify_hash_group(
    connection,
    objects: Mapping[str, Mapping[str, Any]],
    *,
    unordered: bool = False,
) -> None:
    for table, expected in objects.items():
        columns = [str(item) for item in expected["columns"]]
        rows = _mysql_rows(
            connection,
            table,
            columns,
            [] if unordered else [str(item) for item in expected.get("order_columns", expected.get("pk_columns", []))],
        )
        hash_function = _hash_rows_unordered if unordered else _hash_rows
        count, digest = hash_function(rows, columns, expected["column_types"])
        if count != int(expected["row_count"]):
            raise MigrationError(
                f"目标 {table} 行数不匹配：{count} != {expected['row_count']}"
            )
        if digest != str(expected["row_sha256"]):
            raise MigrationError(f"目标 {table} 内容哈希不匹配")


def _business_view_target_results(
    connection,
    expected: Mapping[str, Any],
    *,
    require_expected_hash: bool,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Compare each materialized view with its reviewed, fixed MySQL SELECT."""
    results: dict[str, dict[str, dict[str, Any]]] = {}
    for scenario in SCENARIOS:
        try:
            views = expected["business"][scenario.id]["views"]
        except (KeyError, TypeError) as exc:
            raise MigrationError(f"目标校验清单缺少 {scenario.name} 视图契约") from exc
        scenario_results: dict[str, dict[str, Any]] = {}
        for view, item in views.items():
            fixed_ddl = _fixed_target_view_ddl(str(view))
            if str(item.get("target_ddl_sha256") or "") != hashlib.sha256(
                fixed_ddl.encode("utf-8")
            ).hexdigest():
                raise MigrationError(f"目标视图固定 DDL 契约不匹配：{view}")
            columns = [str(column) for column in item["columns"]]
            column_types = {
                str(column): str(type_name)
                for column, type_name in dict(item["column_types"]).items()
            }
            selected_columns = ", ".join(_q_mysql(column) for column in columns)
            target_sql = (
                f"SELECT {selected_columns} FROM ("
                f"{_fixed_target_view_select(str(view))}"
                ") AS `_migration_fixed_view_target`"
            )
            target_rows = (
                connection.execution_options(stream_results=True)
                .exec_driver_sql(target_sql)
                .mappings()
            )
            target_count, target_digest = _hash_rows_unordered(
                target_rows, columns, column_types
            )
            actual_count, actual_digest = _hash_rows_unordered(
                _mysql_rows(connection, str(view), columns, []),
                columns,
                column_types,
            )
            if (actual_count, actual_digest) != (target_count, target_digest):
                raise MigrationError(f"目标视图与固定 MySQL SELECT 结果不一致：{view}")
            if require_expected_hash and (
                target_count != int(item["row_count"])
                or target_digest != str(item["row_sha256"])
            ):
                raise MigrationError(f"目标视图固定 SELECT 哈希与 expected 不一致：{view}")
            scenario_results[str(view)] = {
                "row_count": target_count,
                "row_sha256": target_digest,
            }
        results[scenario.id] = scenario_results
    return results


def _refresh_business_view_expected(engine, expected: MutableMapping[str, Any]) -> None:
    """Replace SQLite preview hashes with the actual fixed-MySQL semantics."""
    with engine.connect() as connection:
        results = _business_view_target_results(
            connection, expected, require_expected_hash=False
        )
    for scenario_id, views in results.items():
        for view, result in views.items():
            expected["business"][scenario_id]["views"][view].update(result)


def _verify_target_object_contract(connection, expected: Mapping[str, Any]) -> None:
    objects = _target_objects(connection)
    allowed = set(expected["base_tables"]) | set(expected["views"])
    unexpected = set(objects) - allowed
    missing = allowed - set(objects)
    if unexpected or missing:
        raise MigrationError(
            "目标库对象集合不精确："
            f"缺少={sorted(missing)}，额外={sorted(unexpected)}"
        )
    for view in expected["views"]:
        if objects[view]["type"] != "VIEW":
            raise MigrationError(f"目标对象 {view} 不是视图")
    _verify_innodb_engines(connection, expected["base_tables"])


def _verify_business_indexes(connection, expected: Mapping[str, Any]) -> None:
    for scenario in SCENARIOS:
        for table, item in expected["business"][scenario.id]["tables"].items():
            rows = connection.exec_driver_sql(
                "SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
                "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
                (connection.engine.url.database, table),
            )
            actual: dict[str, list[str]] = defaultdict(list)
            for name, column, _sequence in rows:
                actual[str(name)].append(str(column))
            for index in item.get("indexes", []):
                if actual.get(str(index["name"])) != list(index["columns"]):
                    raise MigrationError(f"目标索引不匹配：{table}.{index['name']}")


def _verify_platform_column_contract(
    connection,
    expected: Mapping[str, Any],
    *,
    allow_missing_datetime_precision: bool = False,
) -> None:
    for table, item in expected["platform"].items():
        rows = connection.exec_driver_sql(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, "
            "CHARACTER_MAXIMUM_LENGTH, DATETIME_PRECISION "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION",
            (connection.engine.url.database, table),
        )
        actual = {
            str(name): {
                "data_type": str(data_type).lower(),
                "nullable": str(nullable).upper() == "YES",
                "length": int(length) if length is not None else None,
                "datetime_precision": (
                    int(datetime_precision)
                    if datetime_precision is not None
                    else None
                ),
            }
            for name, data_type, nullable, length, datetime_precision in rows
        }
        if list(actual) != list(item["columns"]):
            raise MigrationError(f"平台表 {table} 列集合/顺序不符合当前 ORM")
        for column in item["columns"]:
            expected_type = str(item["mysql_data_types"][column]).lower()
            if actual[column]["data_type"] != expected_type:
                raise MigrationError(
                    f"平台列类型不匹配：{table}.{column} "
                    f"{actual[column]['data_type']} != {expected_type}"
                )
            if actual[column]["nullable"] != bool(item["nullable"][column]):
                raise MigrationError(f"平台列 nullable 不匹配：{table}.{column}")
            expected_length = item["character_lengths"].get(column)
            if expected_length is not None and actual[column]["length"] != int(expected_length):
                raise MigrationError(
                    f"平台列宽度不匹配：{table}.{column} "
                    f"{actual[column]['length']} != {expected_length}"
                )
        expected_datetime = item.get("datetime_precisions")
        if expected_datetime is None and allow_missing_datetime_precision:
            continue
        if not isinstance(expected_datetime, Mapping):
            raise MigrationError(f"平台表 {table} 缺少 DATETIME 精度契约")
        for column, precision in expected_datetime.items():
            if actual.get(column, {}).get("datetime_precision") != int(precision):
                raise MigrationError(
                    f"平台时间列精度不匹配：{table}.{column} "
                    f"{actual.get(column, {}).get('datetime_precision')} != {precision}"
                )


def _verify_platform_foreign_keys(connection, expected: Mapping[str, Any]) -> None:
    for child_table, item in expected["platform"].items():
        grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
        for foreign_key in item.get("foreign_keys", []):
            grouped[int(foreign_key["id"])].append(foreign_key)
        for foreign_key_id, parts in grouped.items():
            parts.sort(key=lambda value: int(value["seq"]))
            parent_table = str(parts[0]["parent_table"])
            if parent_table not in expected["platform"]:
                continue
            present = " AND ".join(
                f"c.{_q_mysql(str(part['child_column']))} IS NOT NULL" for part in parts
            )
            match = " AND ".join(
                f"p.{_q_mysql(str(part['parent_column']))} = "
                f"c.{_q_mysql(str(part['child_column']))}"
                for part in parts
            )
            count = int(
                connection.exec_driver_sql(
                    f"SELECT COUNT(*) FROM {_q_mysql(child_table)} AS c "
                    f"WHERE {present} AND NOT EXISTS ("
                    f"SELECT 1 FROM {_q_mysql(parent_table)} AS p WHERE {match})"
                ).scalar_one()
                or 0
            )
            if count:
                raise MigrationError(
                    f"平台外键孤儿：{child_table} FK#{foreign_key_id} -> "
                    f"{parent_table}，共 {count} 行"
                )


def _verify_minio_files(client, expected: Mapping[str, Any]) -> None:
    minio_expected = expected.get("minio")
    if not isinstance(minio_expected, Mapping):
        raise MigrationError("目标校验清单缺少 MinIO 能力契约")
    capability = _normalize_minio_versioning_capability(
        minio_expected.get("versioning")
    )
    if minio_expected.get("object_key_strategy") != MINIO_OBJECT_KEY_STRATEGY:
        raise MigrationError("目标校验清单的 MinIO object key 策略不受支持")
    files = list(expected["files"])
    _assert_non_reusable_object_keys(files)
    expected_by_scenario: dict[str, set[tuple[str, str, bool]]] = defaultdict(set)
    bucket_by_scenario: dict[str, str] = {}
    buckets = {str(item["bucket_name"]) for item in files}
    for bucket in buckets:
        _assert_minio_versioning_capability(
            client,
            bucket,
            capability,
            prefix=str(minio_expected.get("prefix") or ""),
        )
    for item in files:
        version_id = _normalize_object_version_id(item.get("object_version_id"))
        if capability == MINIO_VERSIONING_ENABLED and not version_id:
            raise MigrationError(f"MinIO 对象缺少不可变 version_id：{item['file_id']}")
        if capability == MINIO_VERSIONING_UNSUPPORTED and version_id:
            raise MigrationError(
                f"不支持版本 API 的 MinIO 对象不能记录 version_id：{item['file_id']}"
            )
        etag = str(item.get("etag") or "").strip('"')
        if not etag:
            raise MigrationError(f"MinIO 对象缺少 ETag：{item['file_id']}")
        _assert_minio_object(
            client,
            bucket=str(item["bucket_name"]),
            object_key=str(item["object_key"]),
            expected_size=int(item["size"]),
            expected_sha256=str(item["sha256"]),
            expected_version_id=version_id,
            expected_etag=etag,
        )
        expected_url = build_object_url(
            endpoint="",
            secure=True,
            bucket=str(item["bucket_name"]),
            object_key=str(item["object_key"]),
        )
        if expected_url != str(item["object_url"]):
            raise MigrationError(f"MinIO 稳定 URL 不匹配：{item['file_id']}")
        scenario_id = str(item["scenario_id"])
        bucket_by_scenario[scenario_id] = str(item["bucket_name"])
        expected_by_scenario[scenario_id].add(
            (
                str(item["object_key"]),
                version_id,
                False,
            )
        )

    for scenario_id, versions in expected_by_scenario.items():
        marker = f"/scenarios/{scenario_id}/"
        sample = next(iter(versions))[0]
        if marker not in sample:
            raise MigrationError("MinIO object key 缺少场景边界")
        prefix = sample.split(marker, 1)[0] + marker
        try:
            if capability == MINIO_VERSIONING_UNSUPPORTED:
                actual_objects = {
                    str(item.object_name)
                    for item in client.list_objects(
                        bucket_by_scenario[scenario_id],
                        prefix=prefix,
                        recursive=True,
                    )
                    if not bool(getattr(item, "is_dir", False))
                }
                expected_objects = {item[0] for item in versions}
                if actual_objects != expected_objects:
                    raise MigrationError(
                        f"MinIO 场景对象集合不精确：{scenario_id}，"
                        f"缺少={sorted(expected_objects - actual_objects)}，"
                        f"额外={sorted(actual_objects - expected_objects)}"
                    )
            else:
                actual_versions = {
                    (
                        str(item.object_name),
                        _normalize_object_version_id(
                            getattr(item, "version_id", "")
                        ),
                        bool(getattr(item, "is_delete_marker", False)),
                    )
                    for item in client.list_objects(
                        bucket_by_scenario[scenario_id],
                        prefix=prefix,
                        recursive=True,
                        include_version=True,
                    )
                    if not bool(getattr(item, "is_dir", False))
                }
                if actual_versions != versions:
                    raise MigrationError(
                        f"MinIO 场景对象版本集合不精确：{scenario_id}，"
                        f"缺少={sorted(versions - actual_versions)}，"
                        f"额外={sorted(actual_versions - versions)}"
                    )
        except MigrationError:
            raise
        except Exception as exc:
            raise MigrationError(f"无法列举 MinIO 场景前缀：{scenario_id}") from exc


def _row_primary_key_identity(
    row: Mapping[str, Any],
    pk_columns: Sequence[str],
    column_types: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(
        _canonical_json(
            _canonical_value(row.get(column), column_types.get(column, ""))
        )
        for column in pk_columns
    )


def _mysql_datetime0_round_half_up(value: datetime) -> datetime:
    normalized = _coerce_datetime(value)
    if normalized is None:  # pragma: no cover - guarded by callers.
        raise MigrationError("不能量化空时间值")
    second = normalized.replace(microsecond=0)
    if normalized.microsecond >= 500_000:
        second += timedelta(seconds=1)
    return second


def _legacy_uploaded_files(old_expected: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    files = old_expected.get("files")
    if not isinstance(files, list) or len(files) != EXPECTED_BUCKET_FILE_COUNT:
        raise MigrationError("旧 expected 文件清单数量不符合迁移契约")
    uploaded: dict[str, dict[str, Any]] = {}
    required = {
        "file_id",
        "scenario_id",
        "bucket_name",
        "object_key",
        "object_url",
        "size",
        "sha256",
        "etag",
        "object_version_id",
    }
    for item in files:
        if not isinstance(item, Mapping) or not required <= set(item):
            raise MigrationError("旧 expected 文件条目结构不完整")
        file_id = str(item["file_id"])
        if not file_id or file_id in uploaded:
            raise MigrationError("旧 expected 文件 ID 为空或重复")
        uploaded[file_id] = dict(item)
    return uploaded


def _platform_rows_hash(
    rows: Sequence[Mapping[str, Any]], expected: Mapping[str, Any]
) -> tuple[int, str]:
    columns = [str(column) for column in expected["columns"]]
    pk_columns = [str(column) for column in expected.get("pk_columns", [])]
    column_types = {
        str(column): str(type_name)
        for column, type_name in dict(expected["column_types"]).items()
    }
    ordered = sorted(
        rows,
        key=lambda row: tuple(str(row.get(column) or "") for column in pk_columns),
    )
    return _hash_rows(ordered, columns, column_types)


def _legacy_migration_binding_fields(
    platform: Mapping[str, Any],
    transformed: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, tuple[str, ...]]:
    source_ids = {
        str(row.get("id") or "")
        for row in _parsed_platform_rows(platform)["connector_bindings"]
    }
    fields: dict[str, tuple[str, ...]] = {}
    for scenario in SCENARIOS:
        matches = [
            row
            for row in transformed["connector_bindings"]
            if str(row.get("scenario_id") or "") == scenario.id
            and str(row.get("connector_kind") or "") == "data_source"
            and str(row.get("connector_id") or "") == scenario.sql_source_id
        ]
        if len(matches) != 1:
            raise MigrationError(f"{scenario.name} 迁移绑定数量不唯一")
        binding_id = str(matches[0].get("id") or "")
        if not binding_id or binding_id in fields:
            raise MigrationError("迁移绑定 ID 为空或重复")
        dynamic = ("checked_at", "updated_at")
        if binding_id not in source_ids:
            dynamic = (*dynamic, "created_at")
        fields[binding_id] = dynamic
    return fields


def _legacy_executed_at_candidates(
    quantized_second: datetime,
    *,
    snapshot_time: datetime,
    control_updated_at: datetime,
) -> Iterator[datetime]:
    quantized = _coerce_datetime(quantized_second)
    if quantized is None or quantized.microsecond != 0:
        raise MigrationError("旧 connector_bindings 时间不是 DATETIME(0)")
    lower = _coerce_datetime(snapshot_time)
    upper = _coerce_datetime(control_updated_at)
    if lower is None or upper is None or lower > upper:
        raise MigrationError("旧迁移 snapshot/control 时间窗口无效")
    previous = quantized - timedelta(seconds=1)
    for microsecond in range(500_000, 1_000_000):
        candidate = previous.replace(microsecond=microsecond)
        if lower <= candidate <= upper:
            yield candidate
    for microsecond in range(0, 500_000):
        candidate = quantized.replace(microsecond=microsecond)
        if lower <= candidate <= upper:
            yield candidate


def _legacy_connector_hash_template(
    rows: Sequence[Mapping[str, Any]],
    expected: Mapping[str, Any],
    dynamic_fields: Mapping[str, Sequence[str]],
) -> tuple[int, tuple[bytes, ...]]:
    marker = "__ONTOLOGY_MIGRATION_LEGACY_EXECUTED_AT__"
    marker_bytes = _canonical_json(marker).encode("utf-8")
    columns = [str(column) for column in expected["columns"]]
    pk_columns = [str(column) for column in expected.get("pk_columns", [])]
    column_types = {
        str(column): str(type_name)
        for column, type_name in dict(expected["column_types"]).items()
    }
    ordered = sorted(
        rows,
        key=lambda row: tuple(str(row.get(column) or "") for column in pk_columns),
    )
    payload = bytearray()
    replacement_count = 0
    for row in ordered:
        binding_id = str(row.get("id") or "")
        binding_dynamic = set(dynamic_fields.get(binding_id, ()))
        normalized: list[Any] = []
        for column in columns:
            if column in binding_dynamic:
                normalized.append(marker)
                replacement_count += 1
            else:
                normalized.append(
                    _canonical_value(row.get(column), column_types.get(column, ""))
                )
        payload.extend(_canonical_json(normalized).encode("utf-8"))
        payload.extend(b"\n")
    parts = tuple(bytes(payload).split(marker_bytes))
    if len(parts) != replacement_count + 1 or replacement_count == 0:
        raise MigrationError("无法构造旧 connector_bindings 时间哈希模板")
    return len(ordered), parts


def _legacy_connector_candidate_digest(
    template_parts: Sequence[bytes], candidate: datetime
) -> bytes:
    normalized = _coerce_datetime(candidate)
    if normalized is None:  # pragma: no cover - candidate generator forbids it.
        raise MigrationError("旧 executed_at 候选为空")
    token = (
        b'"'
        + normalized.isoformat(sep=" ", timespec="microseconds").encode("ascii")
        + b'"'
    )
    return hashlib.sha256(token.join(template_parts)).digest()


def _recover_legacy_executed_at(
    connection,
    old_expected: Mapping[str, Any],
    platform: Mapping[str, Any],
    settings: ServiceSettings,
    uploaded: Mapping[str, Mapping[str, Any]],
    control_state: Mapping[str, Any],
) -> str:
    try:
        expected = old_expected["platform"]["connector_bindings"]
    except (KeyError, TypeError) as exc:
        raise MigrationError("旧 expected 缺少 connector_bindings 契约") from exc
    columns = [str(item) for item in expected.get("columns", [])]
    pk_columns = [str(item) for item in expected.get("pk_columns", [])]
    if not pk_columns:
        raise MigrationError("旧 connector_bindings 契约缺少主键")
    remote_rows = list(
        _mysql_rows(connection, "connector_bindings", columns, pk_columns)
    )
    if len(remote_rows) != int(expected.get("row_count") or 0):
        raise MigrationError("旧 connector_bindings 行数与 expected 不一致")

    probe = "1900-01-01T00:00:00+00:00"
    transformed = transform_platform_rows(
        platform, settings, uploaded, executed_at=probe
    )
    dynamic_fields = _legacy_migration_binding_fields(platform, transformed)
    remote_by_id = {str(row.get("id") or ""): row for row in remote_rows}
    if len(remote_by_id) != len(remote_rows):
        raise MigrationError("旧 connector_bindings 出现重复 ID")
    quantized_values: set[datetime] = set()
    for binding_id, fields in dynamic_fields.items():
        remote = remote_by_id.get(binding_id)
        if remote is None:
            raise MigrationError("旧 connector_bindings 缺少迁移绑定")
        for column in fields:
            value = _coerce_datetime(remote.get(column))
            if value is None or value.microsecond != 0:
                raise MigrationError(
                    f"旧迁移绑定时间不符合 DATETIME(0)：connector_bindings.{column}"
                )
            quantized_values.add(value)
    if len(quantized_values) != 1:
        raise MigrationError("旧迁移绑定时间没有量化到同一秒")
    quantized = next(iter(quantized_values))
    try:
        snapshot_time = _parse_utc_datetime(
            platform["snapshot_time"]
        ).replace(tzinfo=None)
        control_created_at = _parse_utc_datetime(
            control_state["created_at"]
        ).replace(tzinfo=None)
        control_updated_at = _parse_utc_datetime(
            control_state["updated_at"]
        ).replace(tzinfo=None)
    except (KeyError, TypeError) as exc:
        raise MigrationError("旧迁移缺少 snapshot/control 时间窗口") from exc
    if control_created_at > control_updated_at:
        raise MigrationError("旧迁移控制记录 created_at 晚于 updated_at")
    if control_updated_at < snapshot_time:
        raise MigrationError("旧迁移控制记录 updated_at 早于 snapshot")

    connector_rows = [dict(row) for row in transformed["connector_bindings"]]
    template_count, template_parts = _legacy_connector_hash_template(
        connector_rows, expected, dynamic_fields
    )
    expected_digest = bytes.fromhex(str(expected["row_sha256"]))
    matches: list[datetime] = []
    for candidate in _legacy_executed_at_candidates(
        quantized,
        snapshot_time=snapshot_time,
        control_updated_at=control_updated_at,
    ):
        if template_count == int(expected["row_count"]) and (
            _legacy_connector_candidate_digest(template_parts, candidate)
            == expected_digest
        ):
            matches.append(candidate)
            if len(matches) > 1:
                break
    if len(matches) != 1:
        raise MigrationError(
            "旧 connector_bindings expected 无法唯一恢复 executed_at："
            f"命中数={len(matches)}"
        )
    return matches[0].replace(tzinfo=timezone.utc).isoformat()


def _verify_legacy_platform_rows(
    connection,
    old_expected: Mapping[str, Any],
    transformed: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    platform_expected = old_expected.get("platform")
    if not isinstance(platform_expected, Mapping) or set(platform_expected) != set(
        PLATFORM_TABLES
    ):
        raise MigrationError("旧 expected 平台表集合不符合当前迁移契约")
    datetime_columns = set(_metadata_datetime_columns(_orm_platform_metadata()))
    for table in PLATFORM_TABLES:
        expected = platform_expected[table]
        local_rows = list(transformed[table])
        rebuilt_count, rebuilt_digest = _platform_rows_hash(local_rows, expected)
        if (
            rebuilt_count != int(expected["row_count"])
            or rebuilt_digest != str(expected["row_sha256"])
        ):
            raise MigrationError(f"本地不可变快照无法重建旧 expected：{table}")

        columns = [str(column) for column in expected["columns"]]
        pk_columns = [str(column) for column in expected.get("pk_columns", [])]
        column_types = {
            str(column): str(type_name)
            for column, type_name in dict(expected["column_types"]).items()
        }
        remote_rows = list(_mysql_rows(connection, table, columns, pk_columns))
        if len(remote_rows) != rebuilt_count:
            raise MigrationError(f"旧目标平台表行数不匹配：{table}")

        def index_rows(
            rows: Sequence[Mapping[str, Any]], label: str
        ) -> dict[tuple[str, ...], Mapping[str, Any]]:
            indexed: dict[tuple[str, ...], Mapping[str, Any]] = {}
            for row in rows:
                key = _row_primary_key_identity(row, pk_columns, column_types)
                if key in indexed:
                    raise MigrationError(f"旧目标平台表 {table} {label} 出现重复主键")
                indexed[key] = row
            return indexed

        local_by_pk = index_rows(local_rows, "本地重建")
        remote_by_pk = index_rows(remote_rows, "远端")
        if set(local_by_pk) != set(remote_by_pk):
            raise MigrationError(f"旧目标平台表主键集合不匹配：{table}")

        explained_datetime_differences = 0
        for key in sorted(local_by_pk):
            local = local_by_pk[key]
            remote = remote_by_pk[key]
            for column in columns:
                if (table, column) not in datetime_columns:
                    if _canonical_value(
                        local.get(column), column_types.get(column, "")
                    ) != _canonical_value(
                        remote.get(column), column_types.get(column, "")
                    ):
                        raise MigrationError(
                            f"旧目标平台非时间字段不匹配：{table}.{column}"
                        )
                    continue
                local_datetime = _coerce_datetime(local.get(column))
                remote_datetime = _coerce_datetime(remote.get(column))
                if local_datetime is None or remote_datetime is None:
                    if local_datetime != remote_datetime:
                        raise MigrationError(
                            f"旧目标平台时间空值状态不匹配：{table}.{column}"
                        )
                    continue
                if (
                    remote_datetime.microsecond != 0
                    or _mysql_datetime0_round_half_up(local_datetime)
                    != remote_datetime
                ):
                    raise MigrationError(
                        f"旧目标平台时间不符合 DATETIME(0) 量化：{table}.{column}"
                    )
                if local_datetime != remote_datetime:
                    explained_datetime_differences += 1

        remote_count, remote_digest = _platform_rows_hash(remote_rows, expected)
        if remote_count != rebuilt_count:
            raise MigrationError(f"旧目标平台表行数在验证期间变化：{table}")
        if (
            remote_digest != str(expected["row_sha256"])
            and explained_datetime_differences == 0
        ):
            raise MigrationError(f"旧目标平台哈希差异无法由时间量化解释：{table}")


def _verify_legacy_v2_database(
    connection,
    old_expected: Mapping[str, Any],
    platform: Mapping[str, Any],
    settings: ServiceSettings,
    control_state: Mapping[str, Any],
) -> None:
    legacy_settings = _recover_readonly_accounts(connection, settings)
    uploaded = _legacy_uploaded_files(old_expected)
    executed_at = _recover_legacy_executed_at(
        connection,
        old_expected,
        platform,
        legacy_settings,
        uploaded,
        control_state,
    )
    transformed = transform_platform_rows(
        platform,
        legacy_settings,
        uploaded,
        executed_at=executed_at,
    )
    _verify_target_object_contract(connection, old_expected)
    _verify_platform_column_contract(
        connection,
        old_expected,
        allow_missing_datetime_precision=True,
    )
    _verify_legacy_platform_rows(connection, old_expected, transformed)
    for scenario in SCENARIOS:
        try:
            scenario_expected = old_expected["business"][scenario.id]
        except (KeyError, TypeError) as exc:
            raise MigrationError(f"旧 expected 缺少 {scenario.name} 业务契约") from exc
        _verify_hash_group(connection, scenario_expected["tables"])
    _business_view_target_results(
        connection, old_expected, require_expected_hash=False
    )
    _verify_business_indexes(connection, old_expected)
    _verify_platform_foreign_keys(connection, old_expected)


def _preflight_and_adopt_v2_executed_target(
    engine,
    settings: ServiceSettings,
    manifest: Mapping[str, Any],
    platform: Mapping[str, Any],
) -> None:
    if _supersede_descriptor(manifest) is None:
        return
    with engine.connect() as connection:
        old_expected = _remote_supersede_expected(
            _read_control_state(connection), manifest
        )
    if old_expected is None:
        # A prior attempt completed the CAS.  The ordinary same-digest running
        # path below owns crash recovery from this point onward.
        return

    # Object verification is intentionally before the CAS and before any DDL.
    # It downloads all 41 exact versions/keys and rejects extra objects.
    _verify_minio_files(_minio_client(settings), old_expected)
    with engine.begin() as connection:
        current_state = _read_control_state(connection)
        current_expected = _remote_supersede_expected(current_state, manifest)
        if current_expected is None:
            return
        if _sha256_json(current_expected) != _sha256_json(old_expected):
            raise MigrationError("旧 expected 在 supersede preflight 期间发生变化")
        _verify_legacy_v2_database(
            connection,
            current_expected,
            platform,
            settings,
            current_state,
        )
        # Commit this ownership transition before the first auto-committing
        # DROP.  A crash after this CAS is a normal same-v3-digest retry.
        _cas_superseded_control_to_running(
            connection,
            manifest=manifest,
            old_expected=current_expected,
        )


def _persist_manifest_execution(
    paths: RuntimePaths,
    manifest: MutableMapping[str, Any],
    expected: Mapping[str, Any],
    *,
    verified: bool,
) -> None:
    state = manifest.setdefault("state", {})
    state["executed"] = True
    state["executed_at"] = state.get("executed_at") or _utc_now()
    state["target_expected"] = copy.deepcopy(expected)
    state["target_expected_sha256"] = _sha256_json(expected)
    if verified:
        state["verified"] = True
        state["verified_at"] = _utc_now()
    _write_json_atomic(paths.manifest_path, manifest)


def _assert_collected_platform_matches_manifest(
    platform: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    if _public_platform_snapshot(platform) != manifest["source"]["platform"]:
        raise MigrationError(
            "平台 SQLite 在初始检查与实际采集之间发生变化；拒绝生成未审核目标哈希"
        )


def execute_migration(
    paths: RuntimePaths,
    *,
    confirmation: str,
    batch_size: int = 1000,
) -> dict[str, Any]:
    local_lock = _acquire_local_phase_lock(paths.manifest_path)
    mysql_lock: _PhaseLockHandle | None = None
    engine = None
    try:
        manifest = load_manifest(paths.manifest_path)
        if confirmation != str(manifest.get("confirm_execute") or ""):
            raise MigrationError("execute 确认令牌不匹配")
        if manifest.get("state", {}).get("cleaned"):
            raise MigrationError("本地源已 cleanup，不能重新 execute")
        settings = load_service_settings(paths.env_file)
        _require_separate_admin(settings)
        _assert_target_settings(manifest, settings)
        engine = _mysql_engine(settings)
        with engine.connect() as connection:
            _verify_mysql_server(connection)
        mysql_lock = _acquire_mysql_phase_lock(engine, settings)
        # Hold both locks while rebuilding the immutable source snapshot.  No
        # second execute/verify/cleanup can interleave with this plan.
        assert_source_unchanged(paths, manifest)
        platform = collect_platform_snapshot(
            paths.platform_db,
            snapshot_time=manifest["source"]["platform"]["snapshot_time"],
        )
        _validate_scenario_contract(platform, paths)
        _assert_collected_platform_matches_manifest(platform, manifest)
        executed_at = _utc_now()
        width_preview = transform_platform_rows(
            platform,
            settings,
            _manifest_upload_preview(manifest),
            executed_at=executed_at,
        )
        # This gate runs before target schema takeover/DDL and before any PUT.
        # It reports metadata only, so historical credentials or content can
        # never leak into migration output.
        _assert_platform_target_widths(width_preview)
        _preflight_and_adopt_v2_executed_target(
            engine, settings, manifest, platform
        )
        metadata, existing_state, settings, existing_accounts = _prepare_target_schema(
            engine, paths.platform_db, manifest, settings
        )
        if existing_state is not None:
            expected = existing_state["expected"]
            if (
                not expected
                or str(expected.get("plan_digest") or "") != manifest["plan_digest"]
            ):
                raise MigrationError("目标 executed 状态缺少有效校验清单")
            account_set = set(existing_accounts)
            _configure_readonly_accounts(
                engine, settings, existing_accounts=account_set
            )
            _configure_runtime_account(
                engine, settings, existing_accounts=account_set
            )
            _persist_manifest_execution(
                paths,
                manifest,
                expected,
                verified=existing_state["status"] == "verified",
            )
            return copy.deepcopy(expected)

        assert metadata is not None
        minio = _minio_client(settings)
        uploaded = _upload_files_to_minio(minio, settings, manifest)
        transformed = transform_platform_rows(
            platform, settings, uploaded, executed_at=executed_at
        )
        # Re-check MinIO-generated ETag/version fields before the first target
        # row insert.  A failure leaves source data untouched and the uploaded
        # canonical objects remain safely reusable by an idempotent retry.
        _assert_platform_target_widths(transformed, metadata=metadata)
        expected = _build_target_expected(
            platform, transformed, manifest, uploaded, settings
        )

        _insert_platform_rows(engine, metadata, platform, transformed)
        _insert_business_tables(engine, paths, manifest, batch_size=batch_size)
        _create_business_indexes_and_views(engine, manifest)
        # SQLite preview hashes are useful source evidence, but MySQL collation
        # and CAST semantics define the deployed views.  Persist only the
        # independently executed fixed-SELECT result as the target truth.
        _refresh_business_view_expected(engine, expected)
        account_set = set(existing_accounts)
        _configure_readonly_accounts(
            engine, settings, existing_accounts=account_set
        )
        _configure_runtime_account(
            engine, settings, existing_accounts=account_set
        )
        with engine.begin() as connection:
            _verify_innodb_engines(connection, expected["base_tables"])
            _verify_business_indexes(connection, expected)
            _write_control_status(
                connection,
                manifest=manifest,
                status="executed",
                expected=expected,
            )
        _persist_manifest_execution(paths, manifest, expected, verified=False)
        return expected
    finally:
        _release_mysql_phase_lock(mysql_lock)
        if engine is not None:
            engine.dispose()
        _release_local_phase_lock(local_lock)


def _verify_database(
    engine,
    expected: Mapping[str, Any],
) -> None:
    with engine.connect() as connection:
        _verify_target_object_contract(connection, expected)
        _verify_platform_column_contract(connection, expected)
        _verify_hash_group(connection, expected["platform"])
        for scenario in SCENARIOS:
            scenario_expected = expected["business"][scenario.id]
            _verify_hash_group(connection, scenario_expected["tables"])
        _business_view_target_results(
            connection, expected, require_expected_hash=True
        )
        _verify_business_indexes(connection, expected)
        _verify_platform_foreign_keys(connection, expected)


def _verify_migration_locked(
    paths: RuntimePaths,
    *,
    manifest: MutableMapping[str, Any],
    settings: ServiceSettings,
    engine,
) -> dict[str, Any]:
    """Run the full verification while the caller owns both phase locks."""
    with engine.connect() as connection:
        state = _read_control_state(connection)
        if not state or state["plan_digest"] != manifest["plan_digest"]:
            raise MigrationError("目标库没有与 manifest 匹配的迁移状态")
        if state["status"] not in {"executed", "verified"}:
            raise MigrationError(f"目标迁移尚未完成：{state['status']}")
        expected = state["expected"]
        if str(expected.get("plan_digest") or "") != manifest["plan_digest"]:
            raise MigrationError("目标校验清单与 manifest 不匹配")
        settings = _recover_readonly_accounts(connection, settings)
    for scenario in SCENARIOS:
        expected_user = str(expected["readonly_accounts"][scenario.id]["username"])
        if settings.readonly_accounts[scenario.id][0] != expected_user:
            raise MigrationError(f"{scenario.name} 的只读账号身份不匹配")
    runtime_expected = expected.get("runtime_account", {})
    if (
        runtime_expected.get("username") != settings.mysql_runtime_user
        or runtime_expected.get("account_host") != settings.mysql_account_host
    ):
        raise MigrationError("MySQL 运行账号身份与目标校验清单不匹配")
    # The random runtime password is intentionally not persisted before a
    # successful verify.  Rotate/recover it under the owned target state,
    # then atomically publish it to .env only after every check passes.
    with engine.connect() as connection:
        runtime_accounts = _mysql_accounts_present(
            connection,
            [settings.mysql_runtime_user],
            account_host=settings.mysql_account_host,
        )
    _configure_runtime_account(engine, settings, existing_accounts=runtime_accounts)

    _verify_database(engine, expected)
    _verify_readonly_accounts(engine, settings)
    _verify_runtime_account(engine, settings)
    _verify_minio_files(_minio_client(settings), expected)
    with engine.begin() as connection:
        # Re-read under the final update transaction so a concurrent plan
        # cannot be certified using stale state.
        state = _read_control_state(connection)
        if not state or state["plan_digest"] != manifest["plan_digest"]:
            raise MigrationError("验证期间目标迁移状态发生变化")
        _write_control_status(
            connection,
            manifest=manifest,
            status="verified",
            expected=expected,
        )
    _persist_manifest_execution(paths, manifest, expected, verified=True)
    _atomic_update_runtime_env(paths.env_file, settings)
    return copy.deepcopy(expected)


def verify_migration(paths: RuntimePaths) -> dict[str, Any]:
    local_lock = _acquire_local_phase_lock(paths.manifest_path)
    mysql_lock: _PhaseLockHandle | None = None
    engine = None
    try:
        manifest = load_manifest(paths.manifest_path)
        settings = load_service_settings(paths.env_file)
        _require_separate_admin(settings)
        _assert_target_settings(manifest, settings)
        engine = _mysql_engine(settings)
        with engine.connect() as connection:
            _verify_mysql_server(connection)
        mysql_lock = _acquire_mysql_phase_lock(engine, settings)
        return _verify_migration_locked(
            paths,
            manifest=manifest,
            settings=settings,
            engine=engine,
        )
    finally:
        _release_mysql_phase_lock(mysql_lock)
        if engine is not None:
            engine.dispose()
        _release_local_phase_lock(local_lock)


def assert_cleanup_allowed(
    manifest: Mapping[str, Any],
    *,
    confirmation: str,
    verified_expected_sha256: str,
) -> None:
    validate_manifest(manifest)
    state = manifest.get("state", {})
    if confirmation != str(manifest.get("confirm_cleanup") or ""):
        raise MigrationError("cleanup 确认令牌不匹配")
    if not state.get("executed") or not state.get("verified"):
        raise MigrationError("cleanup 前必须成功 execute 并完成端到端 verify")
    expected = state.get("target_expected")
    if not isinstance(expected, Mapping) or not expected:
        raise MigrationError("cleanup 缺少目标校验清单")
    manifest_digest = str(state.get("target_expected_sha256") or "")
    if manifest_digest != _sha256_json(expected):
        raise MigrationError("cleanup 的目标校验清单已被修改")
    if manifest_digest != verified_expected_sha256:
        raise MigrationError("当前 verify 结果与 cleanup 校验清单不一致")


def _inventory_path(data_root: Path, relative_path: str) -> Path:
    relative = Path(str(relative_path))
    if relative.is_absolute() or ".." in relative.parts:
        raise MigrationError(f"cleanup 清单路径不安全：{relative_path}")
    root = data_root.resolve(strict=True)
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise MigrationError(f"cleanup 清单路径越界：{relative_path}") from exc
    return candidate


def _assert_inventory_file(path: Path, item: Mapping[str, Any]) -> None:
    if not path.is_file() or path.is_symlink():
        raise MigrationError(f"cleanup 文件不存在或不是普通文件：{path}")
    if path.stat().st_size != int(item["size"]):
        raise MigrationError(f"cleanup 文件大小已变化：{path}")
    if _sha256_file(path) != str(item["sha256"]):
        raise MigrationError(f"cleanup 文件哈希已变化：{path}")


WINDOWS_UNLINK_RETRY_DELAYS = (0.05, 0.15, 0.30)


def _is_windows_sharing_violation(error: BaseException) -> bool:
    return isinstance(error, PermissionError) and int(
        getattr(error, "winerror", 0) or 0
    ) == 32


def _unlink_inventory_file(
    path: Path,
    item: Mapping[str, Any],
    *,
    retry_delays: Sequence[float] = WINDOWS_UNLINK_RETRY_DELAYS,
) -> None:
    """Delete one verified file, retrying only Windows sharing violations."""
    for attempt in range(len(retry_delays) + 1):
        # Re-open and hash on every attempt.  A sharing violation must never
        # turn the retry into an unchecked deletion window.
        _assert_inventory_file(path, item)
        try:
            path.unlink()
            return
        except PermissionError as exc:
            if not _is_windows_sharing_violation(exc) or attempt >= len(
                retry_delays
            ):
                raise
            gc.collect()
            time.sleep(float(retry_delays[attempt]))


def _cleanup_local_data_locked(
    paths: RuntimePaths,
    *,
    confirmation: str,
    manifest: MutableMapping[str, Any],
    settings: ServiceSettings,
    engine,
) -> int:
    # Fresh end-to-end verification is mandatory on every cleanup attempt.
    expected = _verify_migration_locked(
        paths,
        manifest=manifest,
        settings=settings,
        engine=engine,
    )
    manifest = load_manifest(paths.manifest_path)
    if manifest.get("state", {}).get("cleaned"):
        return 0
    verified_digest = _sha256_json(expected)
    assert_cleanup_allowed(
        manifest,
        confirmation=confirmation,
        verified_expected_sha256=verified_digest,
    )
    state = manifest.setdefault("state", {})
    inventory = list(manifest["source"]["cleanup_inventory"])
    if not inventory:
        raise MigrationError("cleanup 清单为空，拒绝推断删除范围")
    already_deleted = set(str(item) for item in state.get("cleanup_deleted", []))
    pending = str(state.get("cleanup_pending") or "")

    if not state.get("cleanup_started"):
        assert_source_unchanged(paths, manifest)
    # Validate every remaining file before deleting any new file.  Missing
    # files are accepted only when an earlier journal write proves ownership.
    for item in inventory:
        relative = str(item["relative_path"])
        path = _inventory_path(paths.data_root, relative)
        if relative in already_deleted or relative == pending:
            if path.exists():
                _assert_inventory_file(path, item)
            continue
        _assert_inventory_file(path, item)

    state["cleanup_started"] = True
    state.setdefault("cleanup_deleted", sorted(already_deleted))
    _write_json_atomic(paths.manifest_path, manifest)
    deleted_count = 0
    # Databases are intentionally last so an interrupted cleanup retains the
    # most useful local recovery material for as long as possible.
    ordered = sorted(
        inventory,
        key=lambda item: (
            str(item["relative_path"]).lower().endswith((".db", ".sqlite", ".sqlite3")),
            str(item["relative_path"]),
        ),
    )
    by_relative = {str(item["relative_path"]): item for item in inventory}
    if pending:
        ordered = [by_relative[pending]] + [
            item for item in ordered if str(item["relative_path"]) != pending
        ]
    for item in ordered:
        relative = str(item["relative_path"])
        if relative in already_deleted:
            continue
        path = _inventory_path(paths.data_root, relative)
        state["cleanup_pending"] = relative
        _write_json_atomic(paths.manifest_path, manifest)
        if path.exists():
            _unlink_inventory_file(path, item)
            deleted_count += 1
        already_deleted.add(relative)
        state["cleanup_deleted"] = sorted(already_deleted)
        state["cleanup_pending"] = None
        _write_json_atomic(paths.manifest_path, manifest)

    root = paths.data_root.resolve(strict=True)
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            # An unlisted or concurrently created file is never removed.
            continue
    state["cleaned"] = True
    state["cleaned_at"] = _utc_now()
    state["cleanup_pending"] = None
    _write_json_atomic(paths.manifest_path, manifest)
    return deleted_count


def cleanup_local_data(paths: RuntimePaths, *, confirmation: str) -> int:
    local_lock = _acquire_local_phase_lock(paths.manifest_path)
    mysql_lock: _PhaseLockHandle | None = None
    engine = None
    try:
        manifest = load_manifest(paths.manifest_path)
        settings = load_service_settings(paths.env_file)
        _require_separate_admin(settings)
        _assert_target_settings(manifest, settings)
        engine = _mysql_engine(settings)
        with engine.connect() as connection:
            _verify_mysql_server(connection)
        mysql_lock = _acquire_mysql_phase_lock(engine, settings)
        return _cleanup_local_data_locked(
            paths,
            confirmation=confirmation,
            manifest=manifest,
            settings=settings,
            engine=engine,
        )
    finally:
        _release_mysql_phase_lock(mysql_lock)
        if engine is not None:
            engine.dispose()
        _release_local_phase_lock(local_lock)


def _default_paths(arguments: argparse.Namespace) -> RuntimePaths:
    backend_root = Path(arguments.backend_root).resolve()
    data_root = Path(arguments.data_root).resolve() if arguments.data_root else backend_root / "data"
    return RuntimePaths(
        backend_root=backend_root,
        data_root=data_root,
        platform_db=(
            Path(arguments.platform_db).resolve()
            if arguments.platform_db
            else data_root / "platform.db"
        ),
        buckets_root=(
            Path(arguments.buckets_root).resolve()
            if arguments.buckets_root
            else data_root / "buckets"
        ),
        manifest_path=(
            Path(arguments.manifest).resolve()
            if arguments.manifest
            else backend_root / "migration-manifests" / "local-to-services.json"
        ),
        env_file=(
            Path(arguments.env_file).resolve()
            if arguments.env_file
            else backend_root / ".env"
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将两个保留业务场景从本地 SQLite/文件迁移到 MySQL/MinIO"
    )
    parser.add_argument(
        "phase", choices=("dry-run", "execute", "verify", "cleanup")
    )
    parser.add_argument(
        "--backend-root", default=str(Path(__file__).resolve().parents[1])
    )
    parser.add_argument("--data-root")
    parser.add_argument("--platform-db")
    parser.add_argument("--buckets-root")
    parser.add_argument("--manifest")
    parser.add_argument(
        "--supersede-manifest",
        help="dry-run only: 已执行但未验证的旧 v2 manifest 路径",
    )
    parser.add_argument("--env-file")
    parser.add_argument("--confirm-execute", default="")
    parser.add_argument("--confirm-cleanup", default="")
    parser.add_argument("--batch-size", type=int, default=1000)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if arguments.batch_size < 100 or arguments.batch_size > 10000:
        parser.error("--batch-size 必须在 100 到 10000 之间")
    if arguments.supersede_manifest and arguments.phase != "dry-run":
        parser.error("--supersede-manifest 只能用于 dry-run")
    paths = _default_paths(arguments)
    try:
        if arguments.phase == "dry-run":
            dry_run_lock = _acquire_local_phase_lock(paths.manifest_path)
            try:
                if arguments.supersede_manifest:
                    manifest = build_superseding_dry_run_manifest(
                        paths, Path(arguments.supersede_manifest).resolve()
                    )
                else:
                    manifest = build_dry_run_manifest(paths)
                _write_json_atomic(paths.manifest_path, manifest)
            finally:
                _release_local_phase_lock(dry_run_lock)
            print(f"manifest={paths.manifest_path}")
            print(f"plan_digest={manifest['plan_digest']}")
            print(f"confirm_execute={manifest['confirm_execute']}")
            print(f"confirm_cleanup={manifest['confirm_cleanup']}")
            print("remote_writes=0")
        elif arguments.phase == "execute":
            expected = execute_migration(
                paths,
                confirmation=arguments.confirm_execute,
                batch_size=arguments.batch_size,
            )
            print(f"executed_plan={expected['plan_digest']}")
            print(f"platform_tables={len(expected['platform'])}")
            print(f"minio_files={len(expected['files'])}")
        elif arguments.phase == "verify":
            expected = verify_migration(paths)
            print(f"verified_plan={expected['plan_digest']}")
            print(f"target_expected_sha256={_sha256_json(expected)}")
        else:
            deleted = cleanup_local_data(
                paths, confirmation=arguments.confirm_cleanup
            )
            print(f"local_files_deleted={deleted}")
        return 0
    except (MigrationError, OSError, sqlite3.Error) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
