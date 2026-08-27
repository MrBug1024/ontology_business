"""Recoverable MySQL -> PostgreSQL/MinIO platform migration.

This command migrates *exactly* the two retained business scenarios.  It does
not turn their industry tables into PostgreSQL application tables.  Platform
control-plane rows are copied to PostgreSQL while the nineteen retained
business relations are reconstructed as immutable Parquet objects in MinIO
and registered in the generic data catalog.

The command is intentionally fail-closed and phase based::

    python -m scripts.migrate_mysql_to_postgresql                 # plan
    python -m scripts.migrate_mysql_to_postgresql bootstrap --confirm ...
    python -m scripts.migrate_mysql_to_postgresql archive --confirm ...
    python -m scripts.migrate_mysql_to_postgresql import --confirm ...
    python -m scripts.migrate_mysql_to_postgresql verify
    python -m scripts.migrate_mysql_to_postgresql cutover --confirm ...

``plan`` is the default and performs no remote writes.  Each mutating phase
requires the phase-specific token recorded in the credential-free local
manifest.  MySQL is always a read-only migration source and is never dropped,
truncated, renamed, or otherwise modified by this module.

Important provenance contract: the Parquet objects are a reconstruction from
the retained MySQL relations (``legacy_mysql_reconstruction``), not the lost
original SQLite/XLSX source files.  The distinction is persisted in every
asset and dataset version record.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, MutableMapping, Sequence


SCRIPT_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_BACKEND_ROOT))


MANIFEST_FORMAT_VERSION = 1
MIGRATION_NAME = "mysql-to-postgresql-catalog-v1"
TARGET_DATABASE_DEFAULT = "ontology_platform"
TARGET_OWNER_ROLE_DEFAULT = "ontology_owner"
TARGET_RUNTIME_ROLE_DEFAULT = "ontology_app"
TARGET_READONLY_ROLE_DEFAULT = "ontology_readonly"
PROVENANCE_KIND = "legacy_mysql_reconstruction"

BOOKKEEPING_SCENARIO_ID = "56e2006148e8499e8599f5c7c8145e60"
MEDICAL_SCENARIO_ID = "cc5d3ff36d2a468596dfa9f8ef2995da"
TARGET_SCENARIO_IDS = (BOOKKEEPING_SCENARIO_ID, MEDICAL_SCENARIO_ID)

BOOKKEEPING_SQL_SOURCE_ID = "68fcb44b941a40d48c7aba1efb14e7f6"
BOOKKEEPING_BUCKET_SOURCE_ID = "7296fec756624e939e813c2253c83482"
MEDICAL_SQL_SOURCE_ID = "a2d20a398ed744e7839acb910f377d6a"
MEDICAL_BUCKET_SOURCE_ID = "76de17773bf24d86891c627dc7981c9b"

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
MEDICAL_TABLES = ("就诊表", "结算表", "规则表", "项目明细表")

# These are governed, rebuildable logical views over the nineteen canonical
# base relations.  They get catalog relation identities but no MinIO fragment,
# so industry-specific storage never leaks into PostgreSQL physical tables.
BOOKKEEPING_DERIVED_RELATIONS: Mapping[str, str] = {
    "audit_project_view": """
SELECT p.*, CAST(c."company_name" AS VARCHAR) AS "company_name"
FROM "audit_projects" AS p
LEFT JOIN "customers" AS c ON c."customer_id" = p."customer_id"
""".strip(),
}
MEDICAL_DERIVED_RELATIONS: Mapping[str, str] = {
    "医疗机构视图": """
SELECT
    CAST("定点医药机构编号" AS VARCHAR) AS "定点医药机构编号",
    MAX(CAST("定点医药机构名称" AS VARCHAR)) AS "定点医药机构名称",
    MAX(CAST("医院等级" AS VARCHAR)) AS "医院等级",
    MAX(CAST("定点归属医保区划" AS VARCHAR)) AS "定点归属医保区划"
FROM "就诊表"
WHERE "定点医药机构编号" IS NOT NULL
  AND TRIM(CAST("定点医药机构编号" AS VARCHAR)) <> ''
GROUP BY CAST("定点医药机构编号" AS VARCHAR)
""".strip(),
    "医保服务项目视图": """
SELECT
    CAST("医保目录编码" AS VARCHAR) AS "医保目录编码",
    MAX(CAST("医保目录名称" AS VARCHAR)) AS "医保目录名称",
    MAX(CAST("目录类别" AS VARCHAR)) AS "目录类别",
    MAX(CAST("医疗收费项目类别" AS VARCHAR)) AS "医疗收费项目类别",
    MAX(CAST("规格" AS VARCHAR)) AS "规格",
    MAX(TRY_CAST(NULLIF(TRIM(CAST("单价" AS VARCHAR)), '') AS DECIMAL(30,8))) AS "参考单价"
FROM "项目明细表"
WHERE "医保目录编码" IS NOT NULL
  AND TRIM(CAST("医保目录编码" AS VARCHAR)) <> ''
GROUP BY CAST("医保目录编码" AS VARCHAR)
""".strip(),
}

if len(BOOKKEEPING_TABLES) + len(MEDICAL_TABLES) != 19:  # pragma: no cover
    raise RuntimeError("业务关系白名单必须恰好包含 19 张基础表")

CATALOG_TABLES = frozenset(
    {
        "data_assets",
        "data_asset_versions",
        "logical_datasets",
        "dataset_schemas",
        "dataset_relations",
        "dataset_fields",
        "dataset_versions",
        "dataset_fragments",
        "dataset_heads",
        "scenario_dataset_bindings",
        "platform_migration_runs",
        "platform_migration_checkpoints",
    }
)

# Content-addressed rows are inserted once and superseded by new identities.
# Workflow/run state lives in separate mutable tables and retains UPDATE.
RUNTIME_IMMUTABLE_TABLES = (
    "data_asset_versions",
    "dataset_schemas",
    "dataset_relations",
    "dataset_fields",
    "dataset_versions",
    "dataset_version_assets",
    "dataset_fragments",
    "ingestion_run_inputs",
    "dataset_lineage_edges",
    "reasoning_terms",
    "derivation_run_inputs",
    "assertions",
    "derivation_evidence",
)
RUNTIME_MIGRATION_LEDGER_TABLES = (
    "alembic_version",
    "platform_migration_runs",
    "platform_migration_checkpoints",
)
RUNTIME_REQUIRED_UPDATE_TABLES = (
    "dataset_heads",
    "ingestion_runs",
    "derivation_runs",
)

PHASES = ("plan", "bootstrap", "archive", "import", "verify", "cutover")
MUTATING_PHASES = ("bootstrap", "archive", "import", "cutover")
PHASE_PREREQUISITES: Mapping[str, tuple[str, ...]] = {
    "bootstrap": ("plan",),
    "archive": ("plan", "bootstrap"),
    "import": ("plan", "bootstrap", "archive"),
    "verify": ("plan", "bootstrap", "archive", "import"),
    "cutover": ("plan", "bootstrap", "archive", "import", "verify"),
}

# Versioned target checkpoints let a repaired migrator coexist with checkpoints
# written by the first live run.  Never reuse a key when its canonical payload
# contract changes: PostgreSQL deliberately rejects a different hash for the
# same (run, stage, item) tuple.
BOOTSTRAP_SCHEMA_CHECKPOINT_PREFIX = "schema-v2"
IMPORT_TARGET_CHECKPOINT = "target-v2"
LEGACY_IMPORT_TARGET_CHECKPOINT = "target"
VERIFY_DEEP_CHECKPOINT = "end-to-end-deep-v2"
VERIFY_SHALLOW_CHECKPOINT = "end-to-end-shallow-v2"
LEGACY_VERIFY_CHECKPOINT = "end-to-end"
CUTOVER_PREPARED_CHECKPOINT = "environment-prepared-v2"
CUTOVER_FINALIZED_CHECKPOINT = "environment-finalized-v2"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
SECRET_KEY_RE = re.compile(
    r"(?:password|passwd|secret|access[_-]?key|token|credential|private[_-]?key)",
    re.IGNORECASE,
)
SQL_SOURCE_TYPES = frozenset({"mysql", "postgres", "postgresql", "sqlite"})


class MigrationError(RuntimeError):
    """A migration contract, safety, or verification failure."""


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    key: str
    display_name: str
    sql_source_id: str
    bucket_source_id: str
    relations: tuple[str, ...]
    derived_relations: Mapping[str, str]


SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        id=BOOKKEEPING_SCENARIO_ID,
        key="bookkeeping",
        display_name="代理记账业务",
        sql_source_id=BOOKKEEPING_SQL_SOURCE_ID,
        bucket_source_id=BOOKKEEPING_BUCKET_SOURCE_ID,
        relations=BOOKKEEPING_TABLES,
        derived_relations=BOOKKEEPING_DERIVED_RELATIONS,
    ),
    ScenarioSpec(
        id=MEDICAL_SCENARIO_ID,
        key="medical-insurance-audit",
        display_name="医保违规审计",
        sql_source_id=MEDICAL_SQL_SOURCE_ID,
        bucket_source_id=MEDICAL_BUCKET_SOURCE_ID,
        relations=MEDICAL_TABLES,
        derived_relations=MEDICAL_DERIVED_RELATIONS,
    ),
)


@dataclass(frozen=True)
class MigrationSettings:
    env_file: Path
    manifest_path: Path
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_user: str
    mysql_password: str
    postgresql_host: str
    postgresql_port: int
    postgresql_admin_database: str
    postgresql_target_database: str
    postgresql_admin_user: str
    postgresql_admin_password: str
    postgresql_owner_role: str
    postgresql_runtime_role: str
    postgresql_readonly_role: str
    postgresql_runtime_password: str
    minio_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_prefix: str
    minio_secure: bool

    def public_summary(self) -> dict[str, Any]:
        """Return manifest-safe connection identity without credentials."""

        return {
            "mysql": {
                "host": self.mysql_host,
                "port": self.mysql_port,
                "database": self.mysql_database,
                "user_fingerprint": _short_hash(self.mysql_user),
            },
            "postgresql": {
                "host": self.postgresql_host,
                "port": self.postgresql_port,
                "database": self.postgresql_target_database,
                "owner_role": self.postgresql_owner_role,
                "runtime_role": self.postgresql_runtime_role,
                "readonly_role": self.postgresql_readonly_role,
            },
            "minio": {
                "endpoint": self.minio_endpoint,
                "bucket": self.minio_bucket,
                "prefix": self.minio_prefix,
                "secure": self.minio_secure,
                "access_key_fingerprint": _short_hash(self.minio_access_key),
            },
        }


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16] if value else ""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    normalized = value or _utc_now()
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _read_dotenv(path: Path) -> dict[str, str]:
    """Read a conservative dotenv subset without evaluating shell syntax."""

    values: dict[str, str] = {}
    if not path.exists():
        raise MigrationError(f"环境文件不存在：{path}")
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise MigrationError(f"{path}:{line_number} 不是有效的 KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise MigrationError(f"{path}:{line_number} 包含非法环境变量名")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _merged_environment(env_file: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    values = _read_dotenv(env_file)
    for key, value in (environ or os.environ).items():
        if value is not None:
            values[key] = str(value)
    return values


def _required(values: Mapping[str, str], key: str) -> str:
    value = str(values.get(key, "")).strip()
    if not value:
        raise MigrationError(f"缺少必需配置 {key}")
    return value


def _port(values: Mapping[str, str], key: str, default: int) -> int:
    raw = str(values.get(key, default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise MigrationError(f"{key} 必须是端口号") from exc
    if not 1 <= value <= 65535:
        raise MigrationError(f"{key} 超出 1..65535")
    return value


def _boolean(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = str(values.get(key, "true" if default else "false")).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise MigrationError(f"{key} 必须是布尔值")


def _validated_identifier(value: str, *, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise MigrationError(f"{label} 不是安全的 PostgreSQL 标识符：{value!r}")
    return value


def load_settings(
    *,
    backend_root: Path,
    env_file: Path | None = None,
    manifest_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> MigrationSettings:
    env_path = (env_file or backend_root / ".env").resolve()
    values = _merged_environment(env_path, environ)
    endpoint = _required(values, "MINIO_ALIYUN_ENDPOINT")
    # Match the runtime object-storage contract: a bare endpoint is HTTPS.
    # Plain HTTP must be an explicit deployment choice.
    inferred_secure = not endpoint.lower().startswith("http://")
    endpoint = re.sub(r"^https?://", "", endpoint, flags=re.IGNORECASE).rstrip("/")
    prefix = str(values.get("MINIO_ALIYUN_FILE_PATH", "")).strip().strip("/")
    target_database = str(values.get("POSTGRESQL_DATABASE", "")).strip()
    target_database = target_database or TARGET_DATABASE_DEFAULT
    owner_role = str(
        values.get("POSTGRESQL_OWNER_ROLE", TARGET_OWNER_ROLE_DEFAULT)
    ).strip()
    runtime_role = str(
        values.get("POSTGRESQL_RUNTIME_ROLE", TARGET_RUNTIME_ROLE_DEFAULT)
    ).strip()
    readonly_role = str(
        values.get("POSTGRESQL_READONLY_ROLE", TARGET_READONLY_ROLE_DEFAULT)
    ).strip()
    for identifier, label in (
        (target_database, "目标数据库名"),
        (owner_role, "owner 角色名"),
        (runtime_role, "runtime 角色名"),
        (readonly_role, "readonly 角色名"),
    ):
        _validated_identifier(identifier, label=label)
    runtime_password = str(values.get("POSTGRESQL_PASSWORD", ""))
    runtime_password = str(
        values.get("POSTGRESQL_RUNTIME_PASSWORD", runtime_password)
    )
    if not runtime_password:
        raise MigrationError("POSTGRESQL_PASSWORD/POSTGRESQL_RUNTIME_PASSWORD 不能为空")
    admin_user = str(values.get("POSTGRESQL_ADMIN_USER", "")).strip() or "postgres"
    admin_password = str(values.get("POSTGRESQL_ADMIN_PASSWORD", ""))
    admin_password = admin_password or str(values.get("POSTGRESQL_PASSWORD", ""))
    if not admin_password:
        raise MigrationError("POSTGRESQL_ADMIN_PASSWORD/POSTGRESQL_PASSWORD 不能为空")
    settings = MigrationSettings(
        env_file=env_path,
        manifest_path=(
            manifest_path
            or backend_root / "migration-manifests" / "mysql-to-postgresql.json"
        ).resolve(),
        mysql_host=_required(values, "ANNUAL_MYSQL_HOST"),
        mysql_port=_port(values, "ANNUAL_MYSQL_PORT", 3306),
        mysql_database=_required(values, "ANNUAL_MYSQL_DATABASE"),
        mysql_user=_required(values, "ANNUAL_MYSQL_USER"),
        mysql_password=str(values.get("ANNUAL_MYSQL_PASSWORD", "")),
        postgresql_host=_required(values, "POSTGRESQL_HOST"),
        postgresql_port=_port(values, "POSTGRESQL_PORT", 5432),
        postgresql_admin_database=str(
            values.get("POSTGRESQL_ADMIN_DATABASE", "postgres")
        ).strip()
        or "postgres",
        postgresql_target_database=target_database,
        postgresql_admin_user=admin_user,
        postgresql_admin_password=admin_password,
        postgresql_owner_role=owner_role,
        postgresql_runtime_role=runtime_role,
        postgresql_readonly_role=readonly_role,
        postgresql_runtime_password=runtime_password,
        minio_endpoint=endpoint,
        minio_access_key=_required(values, "MINIO_ALIYUN_ACCESS_KEY_ID"),
        minio_secret_key=_required(values, "MINIO_ALIYUN_ACCESS_KEY_SECRET"),
        minio_bucket=_required(values, "MINIO_BUCKETNAME"),
        minio_prefix=prefix,
        minio_secure=_boolean(values, "MINIO_SECURE", inferred_secure),
    )
    if settings.postgresql_admin_database == settings.postgresql_target_database:
        # Once cut over this is valid for read-only verify, but bootstrap must
        # still override the admin database to postgres if it needs to create.
        pass
    return settings


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise MigrationError("数据中包含不可规范化的 NaN/Infinity")
        return format(value, ".17g")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is None:
            normalized = normalized.replace(tzinfo=timezone.utc)
        return normalized.astimezone(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


class RowHasher:
    """Streaming, boundary-safe canonical row-set digest."""

    def __init__(self, columns: Sequence[str]) -> None:
        self.columns = tuple(columns)
        self.count = 0
        self._digest = hashlib.sha256()
        header = _canonical_json({"columns": self.columns}).encode("utf-8")
        self._digest.update(len(header).to_bytes(8, "big"))
        self._digest.update(header)

    def update(self, row: Mapping[str, Any]) -> None:
        encoded = _canonical_json([row.get(column) for column in self.columns]).encode(
            "utf-8"
        )
        self._digest.update(len(encoded).to_bytes(8, "big"))
        self._digest.update(encoded)
        self.count += 1

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _sanitize_mapping(value: Any) -> Any:
    """Recursively remove credentials before a config reaches PostgreSQL."""

    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_mapping(item)
            for key, item in value.items()
            if not SECRET_KEY_RE.search(str(key))
        }
    if isinstance(value, list):
        return [_sanitize_mapping(item) for item in value]
    return value


def dataset_connector_config(
    *, dataset_id: str, dataset_version_id: str, binding_id: str
) -> dict[str, Any]:
    return {
        "adapter": "dataset",
        "backend": "duckdb_parquet",
        "dataset_id": dataset_id,
        "dataset_version_id": dataset_version_id,
        "scenario_dataset_binding_id": binding_id,
    }


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MigrationError(f"迁移清单不存在，请先执行 plan：{path}") from exc
    except json.JSONDecodeError as exc:
        raise MigrationError(f"迁移清单不是有效 JSON：{path}") from exc
    if not isinstance(manifest, dict):
        raise MigrationError("迁移清单根节点必须是对象")
    if manifest.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise MigrationError("迁移清单版本不受支持")
    if manifest.get("migration_name") != MIGRATION_NAME:
        raise MigrationError("迁移清单名称不匹配")
    _verify_manifest_digest(manifest)
    return manifest


def _manifest_digest_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload.pop("manifest_digest", None)
    payload.pop("phase_state", None)
    payload.pop("checkpoints", None)
    payload.pop("archive", None)
    payload.pop("verification", None)
    return payload


def _set_manifest_digest(manifest: MutableMapping[str, Any]) -> None:
    manifest["manifest_digest"] = _sha256_json(_manifest_digest_payload(manifest))


def _verify_manifest_digest(manifest: Mapping[str, Any]) -> None:
    expected = _sha256_json(_manifest_digest_payload(manifest))
    if not secrets.compare_digest(str(manifest.get("manifest_digest", "")), expected):
        raise MigrationError("迁移清单 plan 部分已被篡改或损坏")


def _phase_token(manifest: Mapping[str, Any], phase: str) -> str:
    return str((manifest.get("confirmations") or {}).get(phase, ""))


def _require_confirmation(
    manifest: Mapping[str, Any], phase: str, supplied: str
) -> None:
    expected = _phase_token(manifest, phase)
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise MigrationError(
            f"{phase} 需要清单中的精确确认令牌；未执行任何远程写入"
        )


def _completed_phases(manifest: Mapping[str, Any]) -> set[str]:
    state = manifest.get("phase_state") or {}
    return {
        phase
        for phase, details in state.items()
        if isinstance(details, Mapping) and details.get("status") == "complete"
    }


def _require_prerequisites(manifest: Mapping[str, Any], phase: str) -> None:
    missing = [
        prerequisite
        for prerequisite in PHASE_PREREQUISITES.get(phase, ())
        if prerequisite not in _completed_phases(manifest)
    ]
    if missing:
        raise MigrationError(f"{phase} 前置阶段未完成：{', '.join(missing)}")


def _mark_phase(
    manifest: MutableMapping[str, Any],
    phase: str,
    status: str,
    *,
    error: str = "",
) -> None:
    state = manifest.setdefault("phase_state", {})
    previous = state.get(phase) if isinstance(state.get(phase), Mapping) else {}
    now = _utc_iso()
    state[phase] = {
        "status": status,
        "started_at": previous.get("started_at") or now,
        "updated_at": now,
        "completed_at": now if status == "complete" else "",
        "error": error[:2000],
    }


def _checkpoint_key(stage: str, item_key: str) -> str:
    return f"{stage}:{item_key}"


def _put_checkpoint(
    manifest: MutableMapping[str, Any],
    *,
    stage: str,
    item_key: str,
    payload: Mapping[str, Any],
) -> None:
    checkpoints = manifest.setdefault("checkpoints", {})
    key = _checkpoint_key(stage, item_key)
    digest = _sha256_json(payload)
    existing = checkpoints.get(key)
    if existing:
        if existing.get("payload_sha256") != digest:
            raise MigrationError(f"检查点 {key} 已存在但内容不同，拒绝覆盖")
        return
    checkpoints[key] = {
        "stage": stage,
        "item_key": item_key,
        "status": "complete",
        "payload_sha256": digest,
        "payload": dict(payload),
        "completed_at": _utc_iso(),
    }


def _deterministic_id(*parts: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_URL, ":".join((MIGRATION_NAME, *parts))).hex


def _validate_exact_scenarios(rows: Sequence[Mapping[str, Any]]) -> None:
    found = {str(row.get("id") or "") for row in rows}
    expected = set(TARGET_SCENARIO_IDS)
    if found != expected:
        unexpected = sorted(found - expected)
        missing = sorted(expected - found)
        raise MigrationError(
            "MySQL 场景闭包不等于精确白名单；"
            f"unexpected={unexpected}, missing={missing}"
        )


def _sqlalchemy_url(
    settings: MigrationSettings, *, backend: str, database: str | None = None
) -> Any:
    from sqlalchemy.engine import URL

    if backend == "mysql":
        return URL.create(
            "mysql+pymysql",
            username=settings.mysql_user,
            password=settings.mysql_password,
            host=settings.mysql_host,
            port=settings.mysql_port,
            database=database or settings.mysql_database,
            query={"charset": "utf8mb4"},
        )
    if backend == "postgresql":
        return URL.create(
            "postgresql+psycopg",
            username=settings.postgresql_admin_user,
            password=settings.postgresql_admin_password,
            host=settings.postgresql_host,
            port=settings.postgresql_port,
            database=database or settings.postgresql_target_database,
            query={"sslmode": "disable"},
        )
    raise AssertionError(f"unsupported backend: {backend}")


def _mysql_engine(settings: MigrationSettings) -> Any:
    from sqlalchemy import create_engine

    # This account is used solely inside READ ONLY transactions.  The script
    # never emits source-side DDL or DML even if the configured account happens
    # to be over-privileged.
    return create_engine(
        _sqlalchemy_url(settings, backend="mysql"),
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def _postgres_engine(settings: MigrationSettings, *, database: str | None = None) -> Any:
    from sqlalchemy import create_engine

    return create_engine(
        _sqlalchemy_url(settings, backend="postgresql", database=database),
        pool_pre_ping=True,
    )


@contextmanager
def _mysql_readonly_snapshot(engine: Any) -> Iterator[Any]:
    connection = engine.connect()
    started = False
    try:
        connection.exec_driver_sql(
            "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
        read_only_variables = {
            str(row[0]).lower(): str(row[1]).strip().lower()
            for row in connection.exec_driver_sql(
                "SHOW SESSION VARIABLES WHERE Variable_name IN "
                "('transaction_read_only', 'tx_read_only')"
            ).all()
        }
        read_only_value = read_only_variables.get(
            "transaction_read_only",
            read_only_variables.get("tx_read_only", ""),
        )
        if read_only_value not in {"1", "on", "true"}:
            raise MigrationError(
                "MySQL session 未确认 TRANSACTION READ ONLY，拒绝反射或扫描"
            )
        # SQLAlchemy opened an implicit transaction for the session settings.
        # Commit those settings before starting the explicit consistent snapshot.
        connection.commit()
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
        started = True
        yield connection
    finally:
        if started:
            try:
                connection.rollback()
            except Exception:
                pass
        connection.close()


def _load_orm_metadata() -> Any:
    # Importing app.models registers every ORM table with the shared Base.
    from app import external_api_models  # noqa: F401
    from app.models import Base

    return Base.metadata


def _reflect_source_metadata(connection: Any) -> Any:
    from sqlalchemy import MetaData

    metadata = MetaData()
    metadata.reflect(bind=connection, views=False)
    return metadata


def _primary_key_names(table: Any) -> tuple[str, ...]:
    return tuple(column.name for column in table.primary_key.columns)


def _ordered_select(table: Any, columns: Sequence[Any] | None = None) -> Any:
    from sqlalchemy import select

    selected = tuple(columns or table.columns)
    primary_keys = tuple(table.primary_key.columns)
    if not primary_keys:
        raise MigrationError(f"表 {table.name} 没有主键，无法生成确定性快照")
    return select(*selected).order_by(*primary_keys)


def _stream_rows(
    connection: Any,
    table: Any,
    *,
    columns: Sequence[Any] | None = None,
    batch_size: int,
) -> Iterator[dict[str, Any]]:
    # In SQLAlchemy 2, Connection.execution_options mutates the Connection in
    # place. Scope streaming to this SELECT so a later PostgreSQL executemany
    # does not inherit a server-side cursor.
    statement = _ordered_select(table, columns).execution_options(
        stream_results=True, yield_per=batch_size
    )
    result = connection.execute(statement)
    try:
        for row in result.mappings():
            yield dict(row)
    finally:
        result.close()


def _table_digest(
    connection: Any,
    table: Any,
    *,
    batch_size: int,
    columns: Sequence[Any] | None = None,
    target_columns: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    selected = tuple(columns or table.columns)
    hasher = RowHasher([column.name for column in selected])
    for row in _stream_rows(
        connection, table, columns=selected, batch_size=batch_size
    ):
        if target_columns:
            row = {
                name: _normalize_target_value(value, target_columns[name])
                for name, value in row.items()
            }
        hasher.update(row)
    return {
        "row_count": hasher.count,
        "row_hash": hasher.hexdigest,
        "columns": [column.name for column in selected],
        "primary_key": list(_primary_key_names(table)),
    }


def _column_contract(table: Any) -> list[dict[str, Any]]:
    pk_positions = {
        name: index for index, name in enumerate(_primary_key_names(table))
    }
    return [
        {
            "name": column.name,
            "physical_type": str(column.type),
            "nullable": bool(column.nullable),
            "key_ordinal": pk_positions.get(column.name),
            "ordinal": index,
        }
        for index, column in enumerate(table.columns)
    ]


def _derived_relation_inventory(
    scenario: ScenarioSpec,
    base_relations: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    def fields(names_and_types: Sequence[tuple[str, str]]) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "physical_type": physical_type,
                "nullable": True,
                "key_ordinal": 0 if index == 0 else None,
                "ordinal": index,
            }
            for index, (name, physical_type) in enumerate(names_and_types)
        ]

    schemas: dict[str, list[dict[str, Any]]] = {}
    if scenario.key == "bookkeeping":
        project_fields = [
            dict(field) for field in base_relations["audit_projects"]["schema"]
        ]
        if not any(field["name"] == "company_name" for field in project_fields):
            customer_company = next(
                (
                    field
                    for field in base_relations["customers"]["schema"]
                    if field["name"] == "company_name"
                ),
                {"physical_type": "VARCHAR(255)"},
            )
            project_fields.append(
                {
                    "name": "company_name",
                    "physical_type": customer_company["physical_type"],
                    "nullable": True,
                    "key_ordinal": None,
                    "ordinal": len(project_fields),
                }
            )
        schemas["audit_project_view"] = project_fields
    elif scenario.key == "medical-insurance-audit":
        schemas["医疗机构视图"] = fields(
            (
                ("定点医药机构编号", "VARCHAR"),
                ("定点医药机构名称", "VARCHAR"),
                ("医院等级", "VARCHAR"),
                ("定点归属医保区划", "VARCHAR"),
            )
        )
        schemas["医保服务项目视图"] = fields(
            (
                ("医保目录编码", "VARCHAR"),
                ("医保目录名称", "VARCHAR"),
                ("目录类别", "VARCHAR"),
                ("医疗收费项目类别", "VARCHAR"),
                ("规格", "VARCHAR"),
                ("参考单价", "DECIMAL(30,8)"),
            )
        )
    result: dict[str, dict[str, Any]] = {}
    for name, view_sql in scenario.derived_relations.items():
        schema = schemas.get(name)
        if schema is None:
            raise MigrationError(f"没有派生关系 Schema：{scenario.key}/{name}")
        result[name] = {
            "kind": "view",
            "view_sql": view_sql,
            "schema": schema,
            "schema_hash": _sha256_json(schema),
            "materialized": False,
        }
    return result


def _normalize_target_value(value: Any, target_column: Any) -> Any:
    from sqlalchemy import types as sqltypes

    if value is None:
        return None
    target_type = target_column.type
    if isinstance(target_type, sqltypes.Boolean):
        return bool(value)
    if isinstance(target_type, sqltypes.DateTime):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            raise MigrationError(
                f"列 {target_column.name} 期望 datetime，实际为 {type(value).__name__}"
            )
        if target_type.timezone:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        if value.tzinfo is not None:
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value
    if isinstance(target_type, sqltypes.JSON):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped[0] in "[{":
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    pass
        return json.loads(_canonical_json(value))
    return value


def _scenario_rows(connection: Any, source_metadata: Any) -> list[dict[str, Any]]:
    from sqlalchemy import select

    table = source_metadata.tables.get("business_scenarios")
    if table is None:
        raise MigrationError("MySQL 缺少 business_scenarios")
    wanted = [column for column in table.columns if column.name in {"id", "name", "tenant_id"}]
    rows = [dict(row) for row in connection.execute(select(*wanted)).mappings()]
    _validate_exact_scenarios(rows)
    return sorted(rows, key=lambda row: str(row.get("id")))


def _validate_data_source_contract(connection: Any, source_metadata: Any) -> None:
    from sqlalchemy import select

    table = source_metadata.tables.get("data_sources")
    if table is None:
        raise MigrationError("MySQL 缺少 data_sources")
    wanted_ids = {
        source_id
        for scenario in SCENARIOS
        for source_id in (scenario.sql_source_id, scenario.bucket_source_id)
    }
    rows = [
        dict(row)
        for row in connection.execute(
            select(table.c.id, table.c.scenario_id, table.c.type).where(
                table.c.id.in_(sorted(wanted_ids))
            )
        ).mappings()
    ]
    if {str(row["id"]) for row in rows} != wanted_ids:
        raise MigrationError("MySQL 缺少白名单内的 SQL 或文件桶数据源")
    by_id = {str(row["id"]): row for row in rows}
    for scenario in SCENARIOS:
        sql_source = by_id[scenario.sql_source_id]
        bucket_source = by_id[scenario.bucket_source_id]
        if str(sql_source.get("scenario_id")) != scenario.id:
            raise MigrationError(f"{scenario.display_name} SQL 数据源场景归属不匹配")
        if str(sql_source.get("type", "")).lower() not in SQL_SOURCE_TYPES:
            raise MigrationError(f"{scenario.display_name} 白名单 SQL 数据源类型异常")
        if str(bucket_source.get("scenario_id")) != scenario.id:
            raise MigrationError(f"{scenario.display_name} 文件桶场景归属不匹配")
        if str(bucket_source.get("type", "")).lower() != "file_bucket":
            raise MigrationError(f"{scenario.display_name} 白名单文件桶类型异常")


def _platform_table_names(source_metadata: Any, orm_metadata: Any) -> tuple[str, ...]:
    names = sorted(
        set(source_metadata.tables)
        & set(orm_metadata.tables)
        - CATALOG_TABLES
        - set(BOOKKEEPING_TABLES)
        - set(MEDICAL_TABLES)
    )
    required = {"business_scenarios", "data_sources", "bucket_files"}
    if not required.issubset(names):
        raise MigrationError(
            "平台 ORM/source 交集缺少核心表：" + ", ".join(sorted(required - set(names)))
        )
    return tuple(names)


def _source_inventory(
    settings: MigrationSettings, *, batch_size: int
) -> dict[str, Any]:
    engine = _mysql_engine(settings)
    try:
        orm_metadata = _load_orm_metadata()
        with _mysql_readonly_snapshot(engine) as connection:
            source_metadata = _reflect_source_metadata(connection)
            platform_names = _platform_table_names(source_metadata, orm_metadata)
            expected_business = {
                relation for scenario in SCENARIOS for relation in scenario.relations
            }
            missing_business = expected_business - set(source_metadata.tables)
            if missing_business:
                raise MigrationError(
                    "MySQL 缺少白名单业务表：" + ", ".join(sorted(missing_business))
                )
            scenarios = _scenario_rows(connection, source_metadata)
            _validate_data_source_contract(connection, source_metadata)
            platform: dict[str, Any] = {}
            for table_name in platform_names:
                source_table = source_metadata.tables[table_name]
                target_table = orm_metadata.tables[table_name]
                common_columns = [
                    source_table.c[name]
                    for name in source_table.c.keys()
                    if name in target_table.c
                ]
                platform[table_name] = _table_digest(
                    connection,
                    source_table,
                    batch_size=batch_size,
                    columns=common_columns,
                    target_columns={name: target_table.c[name] for name in target_table.c.keys()},
                )
            datasets: dict[str, Any] = {}
            for scenario in SCENARIOS:
                relation_inventory: dict[str, Any] = {}
                for relation_name in scenario.relations:
                    table = source_metadata.tables[relation_name]
                    details = _table_digest(
                        connection, table, batch_size=batch_size
                    )
                    details["schema"] = _column_contract(table)
                    details["schema_hash"] = _sha256_json(details["schema"])
                    relation_inventory[relation_name] = details
                scenario_row = next(row for row in scenarios if row["id"] == scenario.id)
                datasets[scenario.key] = {
                    "scenario_id": scenario.id,
                    "tenant_id": scenario_row.get("tenant_id"),
                    "display_name": scenario.display_name,
                    "sql_source_id": scenario.sql_source_id,
                    "bucket_source_id": scenario.bucket_source_id,
                    "provenance_kind": PROVENANCE_KIND,
                    "relations": relation_inventory,
                    "derived_relations": _derived_relation_inventory(
                        scenario, relation_inventory
                    ),
                }
            inventory = {
                "scenarios": scenarios,
                "platform": platform,
                "datasets": datasets,
            }
            inventory["source_fingerprint"] = _sha256_json(inventory)
            return inventory
    finally:
        engine.dispose()


def _probe_postgresql(settings: MigrationSettings) -> dict[str, Any]:
    engine = _postgres_engine(
        settings, database=settings.postgresql_admin_database
    )
    try:
        with engine.connect() as connection:
            version = str(connection.exec_driver_sql("SELECT version()").scalar_one())
            exists = bool(
                connection.exec_driver_sql(
                    "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = %s)",
                    (settings.postgresql_target_database,),
                ).scalar_one()
            )
        return {
            "reachable": True,
            "server_version_fingerprint": _short_hash(version),
            "target_database_exists": exists,
        }
    finally:
        engine.dispose()


def _minio_client(settings: MigrationSettings) -> Any:
    try:
        from minio import Minio
    except ImportError as exc:  # pragma: no cover - dependency validation
        raise MigrationError("缺少 minio 依赖") from exc
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def _probe_minio(settings: MigrationSettings) -> dict[str, Any]:
    client = _minio_client(settings)
    if not client.bucket_exists(settings.minio_bucket):
        raise MigrationError(f"MinIO bucket 不存在：{settings.minio_bucket}")
    return {"reachable": True, "bucket_exists": True}


def build_plan(settings: MigrationSettings, *, batch_size: int) -> dict[str, Any]:
    target_probe = {
        "postgresql": _probe_postgresql(settings),
        "minio": _probe_minio(settings),
    }
    inventory = _source_inventory(settings, batch_size=batch_size)
    plan_core = {
        "contract": {
            "scenario_ids": list(TARGET_SCENARIO_IDS),
            "business_relation_count": 19,
            "derived_relation_count": 3,
            "source_mutation_allowed": False,
            "source_deletion_allowed": False,
            "canonical_business_storage": "minio-parquet",
            "control_plane_storage": "postgresql",
            "provenance_kind": PROVENANCE_KIND,
        },
        "connections": settings.public_summary(),
        "source": inventory,
        "target_probe": target_probe,
    }
    plan_digest = _sha256_json(plan_core)
    run_id = uuid.uuid4().hex
    confirmations = {
        phase: f"{phase}:{plan_digest[:20]}:{secrets.token_urlsafe(12)}"
        for phase in MUTATING_PHASES
    }
    manifest: dict[str, Any] = {
        "format_version": MANIFEST_FORMAT_VERSION,
        "migration_name": MIGRATION_NAME,
        "run_id": run_id,
        "created_at": _utc_iso(),
        "plan_digest": plan_digest,
        **plan_core,
        "confirmations": confirmations,
        "phase_state": {},
        "checkpoints": {},
        "archive": {"datasets": {}},
        "verification": {},
    }
    _mark_phase(manifest, "plan", "complete")
    _set_manifest_digest(manifest)
    return manifest


def _safe_error(settings: MigrationSettings, exc: BaseException) -> str:
    message = str(exc)
    for secret in (
        settings.mysql_password,
        settings.postgresql_admin_password,
        settings.postgresql_runtime_password,
        settings.minio_secret_key,
        settings.minio_access_key,
    ):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _psycopg_connection(
    settings: MigrationSettings, *, database: str, autocommit: bool
) -> Any:
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - dependency validation
        raise MigrationError("缺少 psycopg 3 驱动") from exc
    return psycopg.connect(
        host=settings.postgresql_host,
        port=settings.postgresql_port,
        dbname=database,
        user=settings.postgresql_admin_user,
        password=settings.postgresql_admin_password,
        sslmode="disable",
        autocommit=autocommit,
        connect_timeout=10,
    )


def _role_row(connection: Any, role_name: str) -> Mapping[str, Any] | None:
    row = connection.execute(
        """
        SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolcanlogin,
               rolinherit, rolbypassrls, rolreplication
          FROM pg_roles
         WHERE rolname = %s
        """,
        (role_name,),
    ).fetchone()
    if row is None:
        return None
    return {
        "rolname": row[0],
        "rolsuper": row[1],
        "rolcreaterole": row[2],
        "rolcreatedb": row[3],
        "rolcanlogin": row[4],
        "rolinherit": row[5],
        "rolbypassrls": row[6],
        "rolreplication": row[7],
    }


def _validate_role_policy(
    role: Mapping[str, Any] | None,
    *,
    label: str,
    can_login: bool,
) -> None:
    if role is None:
        raise MigrationError(f"{label} 角色创建后不可见")
    expected_keys = {
        "rolsuper",
        "rolcreaterole",
        "rolcreatedb",
        "rolcanlogin",
        "rolinherit",
        "rolbypassrls",
        "rolreplication",
    }
    missing = expected_keys - set(role)
    if missing:
        raise MigrationError(f"{label} 角色治理信息不完整：{sorted(missing)}")
    unsafe = [
        name
        for name in (
            "rolsuper",
            "rolcreaterole",
            "rolcreatedb",
            "rolinherit",
            "rolbypassrls",
            "rolreplication",
        )
        if bool(role[name])
    ]
    if bool(role["rolcanlogin"]) != can_login:
        unsafe.append("rolcanlogin")
    if unsafe:
        raise MigrationError(
            f"既有 {label} 角色违反最小权限契约：{', '.join(sorted(unsafe))}"
        )


def _runtime_role_memberships(connection: Any, role_name: str) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT granted.rolname
          FROM pg_auth_members AS membership
          JOIN pg_roles AS member ON member.oid = membership.member
          JOIN pg_roles AS granted ON granted.oid = membership.roleid
         WHERE member.rolname = %s
         ORDER BY granted.rolname
        """,
        (role_name,),
    ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _reject_runtime_memberships(connection: Any, role_name: str) -> None:
    memberships = _runtime_role_memberships(connection, role_name)
    if memberships:
        raise MigrationError(
            "runtime 角色不得继承或 SET ROLE 到任何其他角色："
            + ", ".join(memberships)
        )


def _ensure_postgresql_roles_and_database(settings: MigrationSettings) -> None:
    from psycopg import sql

    with _psycopg_connection(
        settings,
        database=settings.postgresql_admin_database,
        autocommit=True,
    ) as connection:
        owner = _role_row(connection, settings.postgresql_owner_role)
        if owner is None:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOBYPASSRLS NOREPLICATION"
                ).format(sql.Identifier(settings.postgresql_owner_role))
            )
        _validate_role_policy(
            _role_row(connection, settings.postgresql_owner_role),
            label="owner",
            can_login=False,
        )

        runtime = _role_row(connection, settings.postgresql_runtime_role)
        runtime_statement = sql.SQL(
            "ALTER ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOINHERIT NOBYPASSRLS NOREPLICATION"
        ).format(
            sql.Identifier(settings.postgresql_runtime_role),
            sql.Literal(settings.postgresql_runtime_password),
        )
        if runtime is None:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOINHERIT NOBYPASSRLS NOREPLICATION"
                ).format(
                    sql.Identifier(settings.postgresql_runtime_role),
                    sql.Literal(settings.postgresql_runtime_password),
                )
            )
        else:
            # Membership is checked before ALTER so an inherited/admin role is
            # never briefly accepted merely because NOINHERIT is being set.
            _reject_runtime_memberships(
                connection, settings.postgresql_runtime_role
            )
            if any(
                bool(runtime[name])
                for name in (
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolbypassrls",
                    "rolreplication",
                )
            ):
                raise MigrationError("既有 runtime 角色拥有危险管理权限")
            connection.execute(runtime_statement)
        _reject_runtime_memberships(connection, settings.postgresql_runtime_role)
        _validate_role_policy(
            _role_row(connection, settings.postgresql_runtime_role),
            label="runtime",
            can_login=True,
        )

        readonly = _role_row(connection, settings.postgresql_readonly_role)
        if readonly is None:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOINHERIT NOBYPASSRLS NOREPLICATION"
                ).format(sql.Identifier(settings.postgresql_readonly_role))
            )
        _validate_role_policy(
            _role_row(connection, settings.postgresql_readonly_role),
            label="readonly",
            can_login=False,
        )

        database_row = connection.execute(
            "SELECT datname, pg_encoding_to_char(encoding) FROM pg_database WHERE datname = %s",
            (settings.postgresql_target_database,),
        ).fetchone()
        if database_row is None:
            connection.execute(
                sql.SQL(
                    "CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0"
                ).format(
                    sql.Identifier(settings.postgresql_target_database),
                    sql.Identifier(settings.postgresql_owner_role),
                )
            )
        elif str(database_row[1]).upper() != "UTF8":
            raise MigrationError("既有目标 PostgreSQL 数据库不是 UTF8 编码")
        connection.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(settings.postgresql_target_database),
                sql.Identifier(settings.postgresql_owner_role),
            )
        )
        connection.execute(
            sql.SQL("ALTER DATABASE {} SET timezone TO 'UTC'").format(
                sql.Identifier(settings.postgresql_target_database)
            )
        )
        connection.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(settings.postgresql_target_database)
            )
        )
        connection.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                sql.Identifier(settings.postgresql_target_database),
                sql.Identifier(settings.postgresql_runtime_role),
                sql.Identifier(settings.postgresql_readonly_role),
            )
        )


def _initialize_target_schema(settings: MigrationSettings) -> dict[str, Any]:
    """Upgrade the target exclusively through Alembic and verify the head.

    Bypassing revision history with direct ORM DDL would let a fresh install
    appear healthy while lacking the governed baseline.  The NOLOGIN
    owner role owns every object; the runtime role receives DML only later.
    """

    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect

    backend_root = Path(__file__).resolve().parents[1]
    alembic_ini = backend_root / "alembic.ini"
    if not alembic_ini.exists():
        raise MigrationError(f"缺少 Alembic 配置：{alembic_ini}")
    config = Config(str(alembic_ini))
    script = ScriptDirectory.from_config(config)
    expected_heads = set(script.get_heads())
    if not expected_heads:
        raise MigrationError("Alembic 没有 head revision")
    target_url = _sqlalchemy_url(
        settings, backend="postgresql", database=settings.postgresql_target_database
    ).render_as_string(hide_password=False)
    previous_url = os.environ.get("ALEMBIC_DATABASE_URL")
    previous_role = os.environ.get("ALEMBIC_ROLE")
    previous_admin = os.environ.get("ALEMBIC_USE_ADMIN")
    os.environ["ALEMBIC_DATABASE_URL"] = target_url
    os.environ["ALEMBIC_ROLE"] = settings.postgresql_owner_role
    os.environ["ALEMBIC_USE_ADMIN"] = "1"
    try:
        command.upgrade(config, "head")
    finally:
        for key, previous in (
            ("ALEMBIC_DATABASE_URL", previous_url),
            ("ALEMBIC_ROLE", previous_role),
            ("ALEMBIC_USE_ADMIN", previous_admin),
        ):
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous

    metadata = _load_orm_metadata()
    engine = _postgres_engine(settings)
    try:
        with engine.connect() as connection:
            actual_heads = {
                str(row[0])
                for row in connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).all()
            }
            if actual_heads != expected_heads:
                raise MigrationError(
                    f"Alembic revision 不在 head：actual={sorted(actual_heads)}, "
                    f"expected={sorted(expected_heads)}"
                )
            database_tables = set(inspect(connection).get_table_names())
            missing_tables = set(metadata.tables) - database_tables
            if missing_tables:
                raise MigrationError(
                    "Alembic head 缺少 ORM 表：" + ", ".join(sorted(missing_tables))
                )
        table_contract = {
            table.name: [
                {
                    "name": column.name,
                    "type": str(column.type),
                    "nullable": bool(column.nullable),
                }
                for column in table.columns
            ]
            for table in sorted(metadata.tables.values(), key=lambda item: item.name)
        }
        return {
            "orm_table_count": len(table_contract),
            "schema_fingerprint": _sha256_json(table_contract),
            "alembic_heads": sorted(actual_heads),
        }
    finally:
        engine.dispose()


def _grant_runtime_privileges(settings: MigrationSettings) -> None:
    from psycopg import sql

    with _psycopg_connection(
        settings,
        database=settings.postgresql_target_database,
        autocommit=True,
    ) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}, {}").format(
                sql.Identifier(settings.postgresql_runtime_role),
                sql.Identifier(settings.postgresql_readonly_role),
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(sql.Identifier(settings.postgresql_runtime_role))
        )
        immutable_tables = sql.SQL(", ").join(
            sql.Identifier(name) for name in RUNTIME_IMMUTABLE_TABLES
        )
        ledger_tables = sql.SQL(", ").join(
            sql.Identifier(name) for name in RUNTIME_MIGRATION_LEDGER_TABLES
        )
        connection.execute(
            sql.SQL("REVOKE UPDATE, DELETE ON TABLE {} FROM {}").format(
                immutable_tables,
                sql.Identifier(settings.postgresql_runtime_role),
            )
        )
        connection.execute(
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE {} FROM {}").format(
                ledger_tables,
                sql.Identifier(settings.postgresql_runtime_role),
            )
        )
        connection.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(
                sql.Identifier(settings.postgresql_readonly_role)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(settings.postgresql_runtime_role))
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "REVOKE UPDATE, DELETE ON TABLES FROM {}"
            ).format(
                sql.Identifier(settings.postgresql_owner_role),
                sql.Identifier(settings.postgresql_runtime_role),
            )
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT SELECT, INSERT ON TABLES TO {}"
            ).format(
                sql.Identifier(settings.postgresql_owner_role),
                sql.Identifier(settings.postgresql_runtime_role),
            )
        )
        connection.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                "GRANT SELECT ON TABLES TO {}"
            ).format(
                sql.Identifier(settings.postgresql_owner_role),
                sql.Identifier(settings.postgresql_readonly_role),
            )
        )


def _remote_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    safe = dict(manifest)
    safe.pop("confirmations", None)
    safe.pop("checkpoints", None)
    return _sanitize_mapping(safe)


def _sync_migration_run(
    settings: MigrationSettings,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    status: str,
    error: str = "",
) -> None:
    from sqlalchemy import text

    engine = _postgres_engine(settings)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO platform_migration_runs
                      (id, migration_name, plan_digest, source_fingerprint,
                       status, current_phase, manifest, started_at, updated_at,
                       completed_at, last_error)
                    VALUES
                      (:id, :migration_name, :plan_digest, :source_fingerprint,
                       :status, :phase, CAST(:manifest AS JSONB), :started_at,
                       :updated_at, :completed_at, :last_error)
                    ON CONFLICT (id) DO UPDATE SET
                      status = EXCLUDED.status,
                      current_phase = EXCLUDED.current_phase,
                      manifest = EXCLUDED.manifest,
                      updated_at = EXCLUDED.updated_at,
                      completed_at = EXCLUDED.completed_at,
                      last_error = EXCLUDED.last_error
                    """
                ),
                {
                    "id": manifest["run_id"],
                    "migration_name": MIGRATION_NAME,
                    "plan_digest": manifest["plan_digest"],
                    "source_fingerprint": manifest["source"]["source_fingerprint"],
                    "status": status,
                    "phase": phase,
                    "manifest": _canonical_json(_remote_manifest(manifest)),
                    "started_at": datetime.fromisoformat(
                        str(manifest["created_at"]).replace("Z", "+00:00")
                    ),
                    "updated_at": _utc_now(),
                    "completed_at": (
                        _utc_now() if status in {"verified", "cutover"} else None
                    ),
                    "last_error": error[:2000],
                },
            )
    finally:
        engine.dispose()


def bootstrap_target(
    settings: MigrationSettings,
    manifest: MutableMapping[str, Any],
    *,
    confirmation: str,
) -> dict[str, Any]:
    _require_prerequisites(manifest, "bootstrap")
    _require_confirmation(manifest, "bootstrap", confirmation)
    # An existing live run can outlive the Alembic head recorded in its local
    # v1 checkpoint.  Explicit bootstrap is therefore a convergence command,
    # not a local-manifest short circuit: Alembic and the grants are idempotent.
    _mark_phase(manifest, "bootstrap", "running")
    _write_json_atomic(settings.manifest_path, manifest)
    try:
        _ensure_postgresql_roles_and_database(settings)
        result = _initialize_target_schema(settings)
        _grant_runtime_privileges(settings)
        checkpoint_item = (
            f"{BOOTSTRAP_SCHEMA_CHECKPOINT_PREFIX}-"
            f"{_sha256_json(result)[:16]}"
        )
        _put_checkpoint(
            manifest,
            stage="bootstrap",
            item_key=checkpoint_item,
            payload=result,
        )
        _mark_phase(manifest, "bootstrap", "complete")
        _sync_migration_run(
            settings, manifest, phase="bootstrap", status="running"
        )
        _write_json_atomic(settings.manifest_path, manifest)
        return result
    except Exception as exc:
        error = _safe_error(settings, exc)
        _mark_phase(manifest, "bootstrap", "failed", error=error)
        _write_json_atomic(settings.manifest_path, manifest)
        raise MigrationError(f"bootstrap 失败：{error}") from exc


def _file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _arrow_type(column: Any) -> Any:
    try:
        import pyarrow as pa
        from sqlalchemy import types as sqltypes
    except ImportError as exc:  # pragma: no cover - dependency validation
        raise MigrationError("归档需要 pyarrow") from exc

    data_type = column.type
    if isinstance(data_type, sqltypes.Boolean):
        return pa.bool_()
    if isinstance(data_type, sqltypes.Integer):
        return pa.int64()
    # SQLAlchemy Float/REAL inherit from Numeric, so this must precede the
    # exact-decimal branch or a DOUBLE with fractional values becomes scale 0.
    if isinstance(data_type, (sqltypes.Float, sqltypes.REAL)):
        return pa.float64()
    if isinstance(data_type, sqltypes.Numeric):
        precision = int(data_type.precision or 38)
        scale = int(data_type.scale or 0)
        if precision <= 38:
            return pa.decimal128(max(precision, scale + 1), scale)
        if precision <= 76:
            return pa.decimal256(max(precision, scale + 1), scale)
        return pa.string()
    if isinstance(data_type, sqltypes.DateTime):
        return pa.timestamp("us", tz="UTC")
    if isinstance(data_type, sqltypes.Date):
        return pa.date32()
    if isinstance(data_type, sqltypes.Time):
        return pa.time64("us")
    if isinstance(data_type, sqltypes.LargeBinary):
        return pa.binary()
    if isinstance(data_type, sqltypes.JSON):
        return pa.string()
    return pa.string()


def _arrow_value(value: Any, arrow_type: Any) -> Any:
    import pyarrow as pa

    if value is None:
        return None
    if pa.types.is_timestamp(arrow_type):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if not isinstance(value, datetime):
            raise MigrationError(f"时间戳列出现非 datetime 值：{value!r}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if pa.types.is_date(arrow_type) and isinstance(value, datetime):
        return value.date()
    if pa.types.is_string(arrow_type):
        if isinstance(value, (dict, list, tuple)):
            return _canonical_json(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return base64.b64encode(bytes(value)).decode("ascii")
        return str(value)
    if pa.types.is_binary(arrow_type):
        return bytes(value)
    if pa.types.is_boolean(arrow_type):
        return bool(value)
    if pa.types.is_integer(arrow_type):
        return int(value)
    if pa.types.is_floating(arrow_type):
        return float(value)
    if pa.types.is_decimal(arrow_type):
        return value if isinstance(value, Decimal) else Decimal(str(value))
    return value


def _arrow_schema(table: Any, *, dataset_key: str, relation_name: str) -> Any:
    import pyarrow as pa

    fields = [
        pa.field(column.name, _arrow_type(column), nullable=bool(column.nullable))
        for column in table.columns
    ]
    metadata = {
        b"ontology.migration": MIGRATION_NAME.encode("utf-8"),
        b"ontology.provenance": PROVENANCE_KIND.encode("utf-8"),
        b"ontology.dataset": dataset_key.encode("utf-8"),
        b"ontology.relation": relation_name.encode("utf-8"),
    }
    return pa.schema(fields, metadata=metadata)


def _write_relation_parquet(
    connection: Any,
    table: Any,
    *,
    dataset_key: str,
    relation_name: str,
    output_path: Path,
    batch_size: int,
) -> dict[str, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - dependency validation
        raise MigrationError("归档需要 pyarrow") from exc

    columns = tuple(table.columns)
    schema = _arrow_schema(
        table, dataset_key=dataset_key, relation_name=relation_name
    )
    row_hasher = RowHasher([column.name for column in columns])
    writer = pq.ParquetWriter(
        str(output_path),
        schema,
        compression="zstd",
        use_dictionary=True,
        write_statistics=True,
        version="2.6",
    )
    try:
        batch: list[dict[str, Any]] = []
        for row in _stream_rows(
            connection, table, columns=columns, batch_size=batch_size
        ):
            row_hasher.update(row)
            batch.append(row)
            if len(batch) >= batch_size:
                arrays = [
                    pa.array(
                        [_arrow_value(item.get(field.name), field.type) for item in batch],
                        type=field.type,
                    )
                    for field in schema
                ]
                writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
                batch.clear()
        if batch:
            arrays = [
                pa.array(
                    [_arrow_value(item.get(field.name), field.type) for item in batch],
                    type=field.type,
                )
                for field in schema
            ]
            writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
    finally:
        writer.close()
    return {
        "row_count": row_hasher.count,
        "row_hash": row_hasher.hexdigest,
        "content_sha256": _file_sha256(output_path),
        "byte_size": output_path.stat().st_size,
        "format": "parquet",
        "compression": "zstd",
    }


def _object_key(
    settings: MigrationSettings,
    *,
    run_id: str,
    dataset_key: str,
    relation_key: str,
    content_sha256: str,
    suffix: str,
) -> str:
    safe_dataset = re.sub(r"[^a-z0-9._-]+", "-", dataset_key.lower()).strip("-")
    safe_relation = re.sub(r"[^a-z0-9._-]+", "-", relation_key.lower()).strip("-")
    if not safe_relation:
        safe_relation = "relation-" + _short_hash(relation_key)
    parts = [
        part
        for part in (
            settings.minio_prefix,
            "migrations",
            MIGRATION_NAME,
            run_id,
            "datasets",
            safe_dataset,
            safe_relation,
            f"{content_sha256}.{suffix}",
        )
        if part
    ]
    return "/".join(part.strip("/") for part in parts)


def _stat_metadata(stat: Any) -> dict[str, str]:
    return {
        str(key).lower(): str(value)
        for key, value in (getattr(stat, "metadata", {}) or {}).items()
    }


def _expected_metadata_value(metadata: Mapping[str, str], key: str) -> str:
    lowered = key.lower()
    return str(
        metadata.get(lowered)
        or metadata.get("x-amz-meta-" + lowered)
        or ""
    )


def _upload_immutable_file(
    client: Any,
    settings: MigrationSettings,
    *,
    object_key: str,
    local_path: Path,
    content_type: str,
    metadata: Mapping[str, str],
) -> dict[str, Any]:
    from minio.error import S3Error

    expected_size = local_path.stat().st_size
    try:
        existing = client.stat_object(settings.minio_bucket, object_key)
    except S3Error as exc:
        if str(getattr(exc, "code", "")) not in {
            "NoSuchKey",
            "NoSuchObject",
            "NoSuchVersion",
        }:
            raise
        existing = None
    if existing is not None:
        existing_metadata = _stat_metadata(existing)
        expected_sha = str(metadata.get("content-sha256", ""))
        actual_sha = _expected_metadata_value(existing_metadata, "content-sha256")
        if int(existing.size) != expected_size or actual_sha != expected_sha:
            raise MigrationError(f"MinIO 对象键已存在但内容契约不同：{object_key}")
        stat = existing
    else:
        client.fput_object(
            settings.minio_bucket,
            object_key,
            str(local_path),
            content_type=content_type,
            metadata={str(key): str(value) for key, value in metadata.items()},
        )
        stat = client.stat_object(settings.minio_bucket, object_key)
        actual_sha = _expected_metadata_value(_stat_metadata(stat), "content-sha256")
        if int(stat.size) != expected_size or actual_sha != metadata.get(
            "content-sha256", ""
        ):
            raise MigrationError(f"MinIO 上传后验证失败：{object_key}")
    return {
        "bucket": settings.minio_bucket,
        "object_key": object_key,
        "object_version_id": str(getattr(stat, "version_id", "") or ""),
        "etag": str(getattr(stat, "etag", "") or "").strip('"'),
        "byte_size": int(stat.size),
        "object_url": f"minio://{settings.minio_bucket}/{object_key}",
    }


def _verify_archived_object(
    client: Any, settings: MigrationSettings, payload: Mapping[str, Any]
) -> None:
    object_info = payload.get("object") or {}
    stat = client.stat_object(
        str(object_info["bucket"]),
        str(object_info["object_key"]),
        version_id=str(object_info.get("object_version_id") or "") or None,
    )
    metadata = _stat_metadata(stat)
    actual_sha = _expected_metadata_value(metadata, "content-sha256")
    if int(stat.size) != int(payload["byte_size"]):
        raise MigrationError(f"MinIO 归档尺寸已变：{object_info['object_key']}")
    if actual_sha != str(payload["content_sha256"]):
        raise MigrationError(f"MinIO 归档哈希已变：{object_info['object_key']}")


def _relation_checkpoint(
    manifest: Mapping[str, Any], dataset_key: str, relation_name: str
) -> Mapping[str, Any] | None:
    checkpoint = (manifest.get("checkpoints") or {}).get(
        _checkpoint_key("archive-relation", f"{dataset_key}/{relation_name}")
    )
    if not isinstance(checkpoint, Mapping) or checkpoint.get("status") != "complete":
        return None
    payload = checkpoint.get("payload")
    return payload if isinstance(payload, Mapping) else None


def _archive_relation(
    *,
    client: Any,
    settings: MigrationSettings,
    manifest: Mapping[str, Any],
    connection: Any,
    table: Any,
    scenario: ScenarioSpec,
    relation_name: str,
    batch_size: int,
) -> dict[str, Any]:
    planned = manifest["source"]["datasets"][scenario.key]["relations"][relation_name]
    with tempfile.TemporaryDirectory(prefix="ontology-pg-archive-") as temp_dir:
        path = Path(temp_dir) / "relation.parquet"
        result = _write_relation_parquet(
            connection,
            table,
            dataset_key=scenario.key,
            relation_name=relation_name,
            output_path=path,
            batch_size=batch_size,
        )
        if int(result["row_count"]) != int(planned["row_count"]):
            raise MigrationError(f"{scenario.key}/{relation_name} 源行数在 plan 后变化")
        if result["row_hash"] != planned["row_hash"]:
            raise MigrationError(f"{scenario.key}/{relation_name} 源内容在 plan 后变化")
        object_key = _object_key(
            settings,
            run_id=str(manifest["run_id"]),
            dataset_key=scenario.key,
            relation_key=relation_name,
            content_sha256=result["content_sha256"],
            suffix="parquet",
        )
        object_info = _upload_immutable_file(
            client,
            settings,
            object_key=object_key,
            local_path=path,
            content_type="application/vnd.apache.parquet",
            metadata={
                "content-sha256": result["content_sha256"],
                "source-row-hash": result["row_hash"],
                "provenance-kind": PROVENANCE_KIND,
                "migration-run-id": str(manifest["run_id"]),
            },
        )
    ids = {
        "bucket_file_id": _deterministic_id(
            "bucket-file", scenario.key, relation_name, result["content_sha256"]
        ),
        "asset_id": _deterministic_id(
            "data-asset", str(planned["schema_hash"]), scenario.key, relation_name
        ),
        "asset_version_id": _deterministic_id(
            "data-asset-version", scenario.key, relation_name, result["content_sha256"]
        ),
    }
    return {
        **result,
        **ids,
        "relation_name": relation_name,
        "schema_hash": planned["schema_hash"],
        "provenance_kind": PROVENANCE_KIND,
        "object": object_info,
    }


def _archive_dataset_manifest(
    *,
    client: Any,
    settings: MigrationSettings,
    manifest: Mapping[str, Any],
    scenario: ScenarioSpec,
    relations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    dataset_manifest = {
        "format_version": 1,
        "migration_name": MIGRATION_NAME,
        "migration_run_id": manifest["run_id"],
        "scenario_id": scenario.id,
        "dataset_key": scenario.key,
        "provenance_kind": PROVENANCE_KIND,
        "provenance_notice": (
            "Reconstructed from retained MySQL relations; this object is not "
            "the original SQLite/XLSX upload."
        ),
        "relations": {
            name: {
                "row_count": payload["row_count"],
                "row_hash": payload["row_hash"],
                "schema_hash": payload["schema_hash"],
                "content_sha256": payload["content_sha256"],
                "byte_size": payload["byte_size"],
                "object": payload["object"],
            }
            for name, payload in sorted(relations.items())
        },
        "derived_relations": manifest["source"]["datasets"][scenario.key].get(
            "derived_relations", {}
        ),
    }
    content = (_canonical_json(dataset_manifest) + "\n").encode("utf-8")
    content_sha = hashlib.sha256(content).hexdigest()
    with tempfile.TemporaryDirectory(prefix="ontology-pg-manifest-") as temp_dir:
        path = Path(temp_dir) / "dataset-manifest.json"
        path.write_bytes(content)
        object_key = _object_key(
            settings,
            run_id=str(manifest["run_id"]),
            dataset_key=scenario.key,
            relation_key="manifest",
            content_sha256=content_sha,
            suffix="json",
        )
        object_info = _upload_immutable_file(
            client,
            settings,
            object_key=object_key,
            local_path=path,
            content_type="application/json",
            metadata={
                "content-sha256": content_sha,
                "provenance-kind": PROVENANCE_KIND,
                "migration-run-id": str(manifest["run_id"]),
            },
        )
    return {
        "bucket_file_id": _deterministic_id(
            "bucket-file", scenario.key, "manifest", content_sha
        ),
        "content_sha256": content_sha,
        "byte_size": len(content),
        "object": object_info,
        "dataset_manifest": dataset_manifest,
        "provenance_kind": PROVENANCE_KIND,
    }


def archive_business_datasets(
    settings: MigrationSettings,
    manifest: MutableMapping[str, Any],
    *,
    confirmation: str,
    batch_size: int,
) -> dict[str, Any]:
    _require_prerequisites(manifest, "archive")
    _require_confirmation(manifest, "archive", confirmation)
    client = _minio_client(settings)
    if not client.bucket_exists(settings.minio_bucket):
        raise MigrationError(f"MinIO bucket 不存在：{settings.minio_bucket}")
    if "archive" in _completed_phases(manifest):
        for dataset in (manifest.get("archive") or {}).get("datasets", {}).values():
            for payload in dataset.get("relations", {}).values():
                _verify_archived_object(client, settings, payload)
            _verify_archived_object(client, settings, dataset["manifest"])
        return dict(manifest["archive"])

    _mark_phase(manifest, "archive", "running")
    _write_json_atomic(settings.manifest_path, manifest)
    engine = _mysql_engine(settings)
    try:
        with _mysql_readonly_snapshot(engine) as connection:
            source_metadata = _reflect_source_metadata(connection)
            _validate_exact_scenarios(_scenario_rows(connection, source_metadata))
            for scenario in SCENARIOS:
                relation_payloads: dict[str, Any] = {}
                for relation_name in scenario.relations:
                    existing = _relation_checkpoint(
                        manifest, scenario.key, relation_name
                    )
                    if existing is not None:
                        current = _table_digest(
                            connection,
                            source_metadata.tables[relation_name],
                            batch_size=batch_size,
                        )
                        planned = manifest["source"]["datasets"][scenario.key][
                            "relations"
                        ][relation_name]
                        if (
                            current["row_count"] != planned["row_count"]
                            or current["row_hash"] != planned["row_hash"]
                        ):
                            raise MigrationError(
                                f"{scenario.key}/{relation_name} 源数据在恢复归档前已变化"
                            )
                        _verify_archived_object(client, settings, existing)
                        payload = dict(existing)
                    else:
                        payload = _archive_relation(
                            client=client,
                            settings=settings,
                            manifest=manifest,
                            connection=connection,
                            table=source_metadata.tables[relation_name],
                            scenario=scenario,
                            relation_name=relation_name,
                            batch_size=batch_size,
                        )
                        _put_checkpoint(
                            manifest,
                            stage="archive-relation",
                            item_key=f"{scenario.key}/{relation_name}",
                            payload=payload,
                        )
                        _write_json_atomic(settings.manifest_path, manifest)
                    relation_payloads[relation_name] = payload

                manifest_checkpoint = (manifest.get("checkpoints") or {}).get(
                    _checkpoint_key("archive-manifest", scenario.key)
                )
                if manifest_checkpoint:
                    manifest_payload = dict(manifest_checkpoint["payload"])
                    _verify_archived_object(client, settings, manifest_payload)
                else:
                    manifest_payload = _archive_dataset_manifest(
                        client=client,
                        settings=settings,
                        manifest=manifest,
                        scenario=scenario,
                        relations=relation_payloads,
                    )
                    _put_checkpoint(
                        manifest,
                        stage="archive-manifest",
                        item_key=scenario.key,
                        payload=manifest_payload,
                    )
                manifest.setdefault("archive", {}).setdefault("datasets", {})[
                    scenario.key
                ] = {
                    "scenario_id": scenario.id,
                    "relations": relation_payloads,
                    "manifest": manifest_payload,
                    "derived_relations": manifest["source"]["datasets"][
                        scenario.key
                    ].get("derived_relations", {}),
                    "content_hash": _sha256_json(
                        {
                            name: payload["content_sha256"]
                            for name, payload in sorted(relation_payloads.items())
                        }
                    ),
                }
                _write_json_atomic(settings.manifest_path, manifest)

        if sum(
            len(dataset["relations"])
            for dataset in manifest["archive"]["datasets"].values()
        ) != 19:
            raise MigrationError("归档完成关系数不是 19")
        _mark_phase(manifest, "archive", "complete")
        _sync_migration_run(settings, manifest, phase="archive", status="running")
        _write_json_atomic(settings.manifest_path, manifest)
        return dict(manifest["archive"])
    except Exception as exc:
        error = _safe_error(settings, exc)
        _mark_phase(manifest, "archive", "failed", error=error)
        _write_json_atomic(settings.manifest_path, manifest)
        raise MigrationError(f"archive 失败：{error}") from exc
    finally:
        engine.dispose()


def _target_required_without_value(column: Any) -> bool:
    return bool(
        not column.nullable
        and not column.primary_key
        and column.default is None
        and column.server_default is None
        and getattr(column, "autoincrement", False) is not True
    )


def _common_copy_columns(source_table: Any, target_table: Any) -> tuple[Any, ...]:
    common_names = [name for name in source_table.c.keys() if name in target_table.c]
    missing_required = [
        column.name
        for column in target_table.columns
        if column.name not in common_names and _target_required_without_value(column)
    ]
    if missing_required:
        raise MigrationError(
            f"表 {target_table.name} 新增必填列无默认值：{missing_required}"
        )
    if not common_names:
        raise MigrationError(f"表 {target_table.name} 源/目标没有公共列")
    return tuple(source_table.c[name] for name in common_names)


def _target_digest(
    connection: Any,
    table: Any,
    *,
    column_names: Sequence[str],
    batch_size: int,
) -> dict[str, Any]:
    columns = [table.c[name] for name in column_names]
    return _table_digest(
        connection, table, columns=columns, batch_size=batch_size
    )


def _insert_batches(
    target_connection: Any,
    target_table: Any,
    rows: Iterable[Mapping[str, Any]],
    *,
    batch_size: int,
) -> int:
    batch: list[dict[str, Any]] = []
    inserted = 0
    for row in rows:
        batch.append(dict(row))
        if len(batch) >= batch_size:
            target_connection.execute(target_table.insert(), batch)
            inserted += len(batch)
            batch.clear()
    if batch:
        target_connection.execute(target_table.insert(), batch)
        inserted += len(batch)
    return inserted


def _copy_platform_table(
    *,
    source_connection: Any,
    target_connection: Any,
    source_table: Any,
    target_table: Any,
    planned: Mapping[str, Any],
    batch_size: int,
) -> dict[str, Any]:
    from sqlalchemy import func, select

    source_columns = _common_copy_columns(source_table, target_table)
    column_names = [column.name for column in source_columns]
    target_columns = {name: target_table.c[name] for name in column_names}
    current_source = _table_digest(
        source_connection,
        source_table,
        columns=source_columns,
        batch_size=batch_size,
        target_columns=target_columns,
    )
    if (
        current_source["row_count"] != planned["row_count"]
        or current_source["row_hash"] != planned["row_hash"]
        or current_source["columns"] != planned["columns"]
    ):
        raise MigrationError(f"平台表 {source_table.name} 在 plan 后发生变化")
    target_count = int(
        target_connection.execute(select(func.count()).select_from(target_table)).scalar_one()
    )
    if target_count:
        current_target = _target_digest(
            target_connection,
            target_table,
            column_names=column_names,
            batch_size=batch_size,
        )
        if (
            current_target["row_count"] == planned["row_count"]
            and current_target["row_hash"] == planned["row_hash"]
        ):
            return {**current_target, "action": "verified-existing"}
        raise MigrationError(
            f"目标表 {target_table.name} 已有与计划不同的数据，拒绝覆盖"
        )

    def normalized_rows() -> Iterator[dict[str, Any]]:
        for source_row in _stream_rows(
            source_connection,
            source_table,
            columns=source_columns,
            batch_size=batch_size,
        ):
            yield {
                name: _normalize_target_value(source_row[name], target_columns[name])
                for name in column_names
            }

    inserted = _insert_batches(
        target_connection,
        target_table,
        normalized_rows(),
        batch_size=batch_size,
    )
    current_target = _target_digest(
        target_connection,
        target_table,
        column_names=column_names,
        batch_size=batch_size,
    )
    if (
        inserted != planned["row_count"]
        or current_target["row_count"] != planned["row_count"]
        or current_target["row_hash"] != planned["row_hash"]
    ):
        raise MigrationError(f"平台表 {target_table.name} 导入后哈希验证失败")
    return {**current_target, "action": "inserted"}


def _insert_exact(connection: Any, table: Any, record: Mapping[str, Any]) -> None:
    from sqlalchemy import select

    filtered = {
        key: value for key, value in record.items() if key in table.c
    }
    primary_keys = [column.name for column in table.primary_key.columns]
    if not primary_keys or any(name not in filtered for name in primary_keys):
        raise MigrationError(f"表 {table.name} 缺少确定性主键值")
    missing_required = [
        column.name
        for column in table.columns
        if column.name not in filtered and _target_required_without_value(column)
    ]
    if missing_required:
        raise MigrationError(f"表 {table.name} 记录缺少必填列：{missing_required}")
    predicate = None
    for name in primary_keys:
        clause = table.c[name] == filtered[name]
        predicate = clause if predicate is None else predicate & clause
    existing = connection.execute(select(table).where(predicate)).mappings().first()
    if existing is None:
        connection.execute(table.insert().values(**filtered))
        return
    for key, expected in filtered.items():
        actual = existing[key]
        if _canonical_value(actual) != _canonical_value(expected):
            raise MigrationError(
                f"表 {table.name} 主键 {tuple(filtered[k] for k in primary_keys)} "
                f"的已有记录列 {key} 不一致"
            )


def _logical_type(physical_type: str) -> str:
    normalized = physical_type.upper()
    if "BOOL" in normalized or "BIT" in normalized:
        return "boolean"
    if "INT" in normalized:
        return "integer"
    if any(token in normalized for token in ("DECIMAL", "NUMERIC", "FLOAT", "DOUBLE", "REAL")):
        return "decimal"
    if "TIMESTAMP" in normalized or "DATETIME" in normalized:
        return "datetime"
    if normalized.startswith("DATE"):
        return "date"
    if normalized.startswith("TIME"):
        return "time"
    if "JSON" in normalized:
        return "json"
    if any(token in normalized for token in ("BINARY", "BLOB", "BYTE")):
        return "binary"
    return "string"


def _field_key(source_name: str) -> str:
    if len(source_name) <= 180:
        return source_name
    return source_name[:150] + "-" + _short_hash(source_name)


def _catalog_ids(
    manifest: Mapping[str, Any], scenario: ScenarioSpec
) -> dict[str, Any]:
    source_dataset = manifest["source"]["datasets"][scenario.key]
    archived = manifest["archive"]["datasets"][scenario.key]
    tenant_id = str(source_dataset.get("tenant_id") or "")
    if not HEX32_RE.fullmatch(tenant_id):
        raise MigrationError(f"{scenario.display_name} 缺少合法 tenant_id")
    dataset_id = _deterministic_id("logical-dataset", tenant_id, scenario.key)
    schema_document = {
        "provenance_kind": PROVENANCE_KIND,
        "relations": {
            name: details["schema"]
            for name, details in sorted(source_dataset["relations"].items())
        },
        "derived_relations": source_dataset.get("derived_relations", {}),
    }
    schema_hash = _sha256_json(schema_document)
    schema_id = _deterministic_id("dataset-schema", dataset_id, schema_hash)
    version_id = _deterministic_id(
        "dataset-version", dataset_id, archived["content_hash"]
    )
    head_id = _deterministic_id("dataset-head", dataset_id, "dev")
    binding_id = _deterministic_id(
        "scenario-dataset-binding", scenario.id, dataset_id, "primary-input"
    )
    all_relation_names = (
        *scenario.relations,
        *tuple(scenario.derived_relations.keys()),
    )
    relation_ids = {
        relation_name: _deterministic_id(
            "dataset-relation", schema_id, relation_name
        )
        for relation_name in all_relation_names
    }
    return {
        "tenant_id": tenant_id,
        "dataset_id": dataset_id,
        "schema_id": schema_id,
        "schema_hash": schema_hash,
        "schema_document": schema_document,
        "version_id": version_id,
        "head_id": head_id,
        "binding_id": binding_id,
        "relation_ids": relation_ids,
    }


def _bucket_file_record(
    *,
    bucket_file_id: str,
    data_source_id: str,
    filename: str,
    content_sha256: str,
    byte_size: int,
    object_info: Mapping[str, Any],
    mime: str,
    created_at: datetime,
) -> dict[str, Any]:
    return {
        "id": bucket_file_id,
        "data_source_id": data_source_id,
        "filename": filename,
        "stored_path": object_info["object_url"],
        "storage_provider": "minio",
        "bucket_name": object_info["bucket"],
        "object_key": object_info["object_key"],
        "object_version_id": object_info.get("object_version_id", ""),
        "etag": object_info.get("etag", ""),
        "object_url": object_info["object_url"],
        "size": byte_size,
        "mime": mime,
        "content_sha256": content_sha256,
        "status": "parsed",
        "error": "",
        "parsed_text": "",
        "created_at": created_at,
    }


def _register_dataset_catalog(
    connection: Any,
    metadata: Any,
    manifest: Mapping[str, Any],
    scenario: ScenarioSpec,
) -> dict[str, Any]:
    required_tables = {
        "bucket_files",
        "data_assets",
        "data_asset_versions",
        "logical_datasets",
        "dataset_schemas",
        "dataset_relations",
        "dataset_fields",
        "dataset_versions",
        "dataset_version_assets",
        "dataset_fragments",
        "dataset_heads",
        "scenario_dataset_bindings",
    }
    missing = required_tables - set(metadata.tables)
    if missing:
        raise MigrationError("目标 ORM 缺少通用 catalog 表：" + ", ".join(sorted(missing)))
    ids = _catalog_ids(manifest, scenario)
    archived = manifest["archive"]["datasets"][scenario.key]
    source_dataset = manifest["source"]["datasets"][scenario.key]
    created_at = datetime.fromisoformat(
        str(manifest["created_at"]).replace("Z", "+00:00")
    )

    # BucketFile remains the single storage-object identity used by the rest
    # of the application; catalog versions/fragments reference it with
    # ON DELETE RESTRICT.
    bucket_files = metadata.tables["bucket_files"]
    for relation_name in scenario.relations:
        payload = archived["relations"][relation_name]
        _insert_exact(
            connection,
            bucket_files,
            _bucket_file_record(
                bucket_file_id=payload["bucket_file_id"],
                data_source_id=scenario.bucket_source_id,
                filename=f"{relation_name}.parquet",
                content_sha256=payload["content_sha256"],
                byte_size=int(payload["byte_size"]),
                object_info=payload["object"],
                mime="application/vnd.apache.parquet",
                created_at=created_at,
            ),
        )
    manifest_payload = archived["manifest"]
    _insert_exact(
        connection,
        bucket_files,
        _bucket_file_record(
            bucket_file_id=manifest_payload["bucket_file_id"],
            data_source_id=scenario.bucket_source_id,
            filename=f"{scenario.key}-dataset-manifest.json",
            content_sha256=manifest_payload["content_sha256"],
            byte_size=int(manifest_payload["byte_size"]),
            object_info=manifest_payload["object"],
            mime="application/json",
            created_at=created_at,
        ),
    )

    logical_datasets = metadata.tables["logical_datasets"]
    _insert_exact(
        connection,
        logical_datasets,
        {
            "id": ids["dataset_id"],
            "tenant_id": ids["tenant_id"],
            "key": f"legacy-{scenario.key}",
            "name": f"{scenario.display_name}数据产品",
            "description": "从保留 MySQL 业务关系重建的通用数据集，非原始上传文件。",
            "lifecycle_status": "active",
            "labels": {
                "provenance_kind": PROVENANCE_KIND,
                "migration_run_id": manifest["run_id"],
            },
            "created_by_user_id": None,
            "created_at": created_at,
            "updated_at": created_at,
            "retired_at": None,
        },
    )
    _insert_exact(
        connection,
        metadata.tables["dataset_schemas"],
        {
            "id": ids["schema_id"],
            "tenant_id": ids["tenant_id"],
            "dataset_id": ids["dataset_id"],
            "schema_version": 1,
            "schema_hash": ids["schema_hash"],
            "compatibility": "none",
            "schema_document": ids["schema_document"],
            "created_by_user_id": None,
            "created_at": created_at,
        },
    )

    dataset_relations = metadata.tables["dataset_relations"]
    dataset_fields = metadata.tables["dataset_fields"]
    for relation_ordinal, relation_name in enumerate(scenario.relations):
        relation_id = ids["relation_ids"][relation_name]
        _insert_exact(
            connection,
            dataset_relations,
            {
                "id": relation_id,
                "tenant_id": ids["tenant_id"],
                "dataset_id": ids["dataset_id"],
                "schema_id": ids["schema_id"],
                "relation_key": relation_name,
                "display_name": relation_name,
                "kind": "table",
                "ordinal": relation_ordinal,
                "description": f"MySQL 关系 {relation_name} 的不可变快照",
            },
        )
        for field in source_dataset["relations"][relation_name]["schema"]:
            _insert_exact(
                connection,
                dataset_fields,
                {
                    "id": _deterministic_id(
                        "dataset-field", relation_id, str(field["name"])
                    ),
                    "tenant_id": ids["tenant_id"],
                    "dataset_id": ids["dataset_id"],
                    "schema_id": ids["schema_id"],
                    "dataset_relation_id": relation_id,
                    "field_key": _field_key(str(field["name"])),
                    "source_name": str(field["name"]),
                    "logical_type": _logical_type(str(field["physical_type"])),
                    "physical_type": str(field["physical_type"]),
                    "nullable": bool(field["nullable"]),
                    "ordinal": int(field["ordinal"]),
                    "key_ordinal": field.get("key_ordinal"),
                    "semantic_role": "primary_key" if field.get("key_ordinal") is not None else "",
                    "field_document": {
                        "source": "mysql",
                        "provenance_kind": PROVENANCE_KIND,
                    },
                },
            )

    derived_inventory = source_dataset.get("derived_relations", {})
    for derived_offset, (relation_name, details) in enumerate(
        derived_inventory.items(), start=len(scenario.relations)
    ):
        relation_id = ids["relation_ids"][relation_name]
        _insert_exact(
            connection,
            dataset_relations,
            {
                "id": relation_id,
                "tenant_id": ids["tenant_id"],
                "dataset_id": ids["dataset_id"],
                "schema_id": ids["schema_id"],
                "relation_key": relation_name,
                "display_name": relation_name,
                "kind": "view",
                "ordinal": derived_offset,
                "description": "由基础 Parquet 关系可重建的受控逻辑视图",
            },
        )
        for field in details["schema"]:
            _insert_exact(
                connection,
                dataset_fields,
                {
                    "id": _deterministic_id(
                        "dataset-field", relation_id, str(field["name"])
                    ),
                    "tenant_id": ids["tenant_id"],
                    "dataset_id": ids["dataset_id"],
                    "schema_id": ids["schema_id"],
                    "dataset_relation_id": relation_id,
                    "field_key": _field_key(str(field["name"])),
                    "source_name": str(field["name"]),
                    "logical_type": _logical_type(str(field["physical_type"])),
                    "physical_type": str(field["physical_type"]),
                    "nullable": bool(field["nullable"]),
                    "ordinal": int(field["ordinal"]),
                    "key_ordinal": field.get("key_ordinal"),
                    "semantic_role": "primary_key" if field.get("key_ordinal") is not None else "",
                    "field_document": {
                        "derived": True,
                        "view_sql_sha256": _short_hash(str(details["view_sql"])),
                    },
                },
            )

    assets = metadata.tables["data_assets"]
    asset_versions = metadata.tables["data_asset_versions"]
    relation_asset_versions: dict[str, str] = {}
    for relation_name in scenario.relations:
        payload = archived["relations"][relation_name]
        _insert_exact(
            connection,
            assets,
            {
                "id": payload["asset_id"],
                "tenant_id": ids["tenant_id"],
                "key": f"mysql-reconstruction/{scenario.key}/{_short_hash(relation_name)}",
                "name": f"{relation_name} Parquet 归档",
                "description": "从 MySQL 保留关系重建，不是原始上传文件。",
                "kind": "file",
                "media_type": "application/vnd.apache.parquet",
                "lifecycle_status": "active",
                "labels": {
                    "dataset_key": scenario.key,
                    "relation_name": relation_name,
                    "provenance_kind": PROVENANCE_KIND,
                },
                "created_by_user_id": None,
                "created_at": created_at,
                "updated_at": created_at,
                "retired_at": None,
            },
        )
        _insert_exact(
            connection,
            asset_versions,
            {
                "id": payload["asset_version_id"],
                "tenant_id": ids["tenant_id"],
                "asset_id": payload["asset_id"],
                "version_number": 1,
                "bucket_file_id": payload["bucket_file_id"],
                "bucket_data_source_id": scenario.bucket_source_id,
                "provenance_kind": "reconstruction",
                "status": "ready",
                "content_sha256": payload["content_sha256"],
                "byte_size": int(payload["byte_size"]),
                "source_locator": {
                    "system": "mysql",
                    "database_fingerprint": _short_hash(
                        str(manifest["connections"]["mysql"]["database"])
                    ),
                    "relation": relation_name,
                },
                "version_document": {
                    "provenance_kind": PROVENANCE_KIND,
                    "source_row_hash": payload["row_hash"],
                    "original_source_available": False,
                    "migration_run_id": manifest["run_id"],
                },
                "created_by_user_id": None,
                "created_at": created_at,
            },
        )
        relation_asset_versions[relation_name] = payload["asset_version_id"]

    manifest_asset_id = _deterministic_id("data-asset", scenario.key, "manifest")
    manifest_asset_version_id = _deterministic_id(
        "data-asset-version", scenario.key, "manifest", manifest_payload["content_sha256"]
    )
    _insert_exact(
        connection,
        assets,
        {
            "id": manifest_asset_id,
            "tenant_id": ids["tenant_id"],
            "key": f"mysql-reconstruction/{scenario.key}/manifest",
            "name": f"{scenario.display_name}数据集 manifest",
            "description": "归档内容、Schema 与行哈希的签名清单。",
            "kind": "generated",
            "media_type": "application/json",
            "lifecycle_status": "active",
            "labels": {"provenance_kind": PROVENANCE_KIND},
            "created_by_user_id": None,
            "created_at": created_at,
            "updated_at": created_at,
            "retired_at": None,
        },
    )
    _insert_exact(
        connection,
        asset_versions,
        {
            "id": manifest_asset_version_id,
            "tenant_id": ids["tenant_id"],
            "asset_id": manifest_asset_id,
            "version_number": 1,
            "bucket_file_id": manifest_payload["bucket_file_id"],
            "bucket_data_source_id": scenario.bucket_source_id,
            "provenance_kind": "generated",
            "status": "ready",
            "content_sha256": manifest_payload["content_sha256"],
            "byte_size": int(manifest_payload["byte_size"]),
            "source_locator": {"migration_run_id": manifest["run_id"]},
            "version_document": {"provenance_kind": PROVENANCE_KIND},
            "created_by_user_id": None,
            "created_at": created_at,
        },
    )

    total_rows = sum(
        int(payload["row_count"]) for payload in archived["relations"].values()
    )
    total_bytes = sum(
        int(payload["byte_size"]) for payload in archived["relations"].values()
    )
    _insert_exact(
        connection,
        metadata.tables["dataset_versions"],
        {
            "id": ids["version_id"],
            "tenant_id": ids["tenant_id"],
            "dataset_id": ids["dataset_id"],
            "schema_id": ids["schema_id"],
            "version_number": 1,
            "parent_version_id": None,
            "status": "ready",
            "record_count": total_rows,
            "fragment_count": len(scenario.relations),
            "byte_size": total_bytes,
            "manifest_bucket_file_id": manifest_payload["bucket_file_id"],
            "manifest_data_source_id": scenario.bucket_source_id,
            "content_hash": archived["content_hash"],
            "manifest": manifest_payload["dataset_manifest"],
            "created_by_user_id": None,
            "created_at": created_at,
            "ready_at": created_at,
        },
    )
    version_assets = metadata.tables["dataset_version_assets"]
    fragments = metadata.tables["dataset_fragments"]
    for ordinal, relation_name in enumerate(scenario.relations):
        payload = archived["relations"][relation_name]
        relation_id = ids["relation_ids"][relation_name]
        _insert_exact(
            connection,
            version_assets,
            {
                "id": _deterministic_id(
                    "dataset-version-asset", ids["version_id"], payload["asset_version_id"]
                ),
                "tenant_id": ids["tenant_id"],
                "dataset_id": ids["dataset_id"],
                "dataset_version_id": ids["version_id"],
                "asset_version_id": payload["asset_version_id"],
                "role": "source",
                "ordinal": ordinal,
                "binding_document": {"relation_id": relation_id},
                "created_at": created_at,
            },
        )
        _insert_exact(
            connection,
            fragments,
            {
                "id": _deterministic_id(
                    "dataset-fragment", ids["version_id"], relation_id, "0"
                ),
                "tenant_id": ids["tenant_id"],
                "dataset_id": ids["dataset_id"],
                "dataset_version_id": ids["version_id"],
                "dataset_relation_id": relation_id,
                "schema_id": ids["schema_id"],
                "bucket_file_id": payload["bucket_file_id"],
                "bucket_data_source_id": scenario.bucket_source_id,
                "ordinal": 0,
                "format": "parquet",
                "compression": "zstd",
                "status": "ready",
                "row_count": int(payload["row_count"]),
                "byte_size": int(payload["byte_size"]),
                "content_sha256": payload["content_sha256"],
                "statistics": {
                    "source_row_hash": payload["row_hash"],
                    "schema_hash": payload["schema_hash"],
                    "provenance_kind": PROVENANCE_KIND,
                },
                "created_at": created_at,
            },
        )
    _insert_exact(
        connection,
        version_assets,
        {
            "id": _deterministic_id(
                "dataset-version-asset", ids["version_id"], manifest_asset_version_id
            ),
            "tenant_id": ids["tenant_id"],
            "dataset_id": ids["dataset_id"],
            "dataset_version_id": ids["version_id"],
            "asset_version_id": manifest_asset_version_id,
            "role": "manifest",
            "ordinal": len(scenario.relations),
            "binding_document": {},
            "created_at": created_at,
        },
    )
    _insert_exact(
        connection,
        metadata.tables["dataset_heads"],
        {
            "id": ids["head_id"],
            "tenant_id": ids["tenant_id"],
            "dataset_id": ids["dataset_id"],
            "environment": "dev",
            "dataset_version_id": ids["version_id"],
            "updated_by_user_id": None,
            "updated_at": created_at,
        },
    )
    _insert_exact(
        connection,
        metadata.tables["scenario_dataset_bindings"],
        {
            "id": ids["binding_id"],
            "tenant_id": ids["tenant_id"],
            "scenario_id": scenario.id,
            "dataset_id": ids["dataset_id"],
            "binding_key": "primary-input",
            "role": "input",
            "binding_mode": "head",
            "dataset_head_id": ids["head_id"],
            "dataset_version_id": None,
            "is_required": True,
            "status": "active",
            "config": {
                "provenance_kind": PROVENANCE_KIND,
                "migration_run_id": manifest["run_id"],
            },
            "created_at": created_at,
            "updated_at": created_at,
        },
    )

    if "ingestion_runs" in metadata.tables:
        _insert_exact(
            connection,
            metadata.tables["ingestion_runs"],
            {
                "id": _deterministic_id("ingestion-run", str(manifest["run_id"]), scenario.key),
                "tenant_id": ids["tenant_id"],
                "dataset_id": ids["dataset_id"],
                "output_version_id": ids["version_id"],
                "pipeline_kind": "mysql_reconstruction",
                "pipeline_version": MIGRATION_NAME,
                "idempotency_key": f"{manifest['run_id']}:{scenario.key}",
                "status": "succeeded",
                "requested_by_user_id": None,
                "records_read": total_rows,
                "records_written": total_rows,
                "bytes_written": total_bytes,
                "checkpoint": {"relations": len(scenario.relations)},
                "error": "",
                "lease_token": "",
                "lease_expires_at": None,
                "trace_bucket_file_id": manifest_payload["bucket_file_id"],
                "trace_data_source_id": scenario.bucket_source_id,
                "created_at": created_at,
                "started_at": created_at,
                "finished_at": created_at,
            },
        )
    return {**ids, "total_rows": total_rows, "total_bytes": total_bytes}


def _merged_binding_ref(
    current: Any,
    *,
    ids: Mapping[str, Any],
    relation_id: str | None,
) -> dict[str, Any]:
    base = _sanitize_mapping(current if isinstance(current, Mapping) else {})
    base.update(
        {
            "adapter": "dataset",
            "dataset_id": ids["dataset_id"],
            "dataset_version_id": ids["version_id"],
            "scenario_dataset_binding_id": ids["binding_id"],
        }
    )
    if relation_id:
        base["dataset_relation_id"] = relation_id
    return base


def _convert_data_sources_and_mappings(
    connection: Any,
    metadata: Any,
    manifest: Mapping[str, Any],
    catalog_by_scenario: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    from sqlalchemy import select, update

    data_sources = metadata.tables["data_sources"]
    source_rows = [
        dict(row)
        for row in connection.execute(select(data_sources)).mappings()
    ]
    sql_source_ids = {scenario.sql_source_id for scenario in SCENARIOS}
    for row in source_rows:
        source_type = str(row.get("type") or "").lower()
        if source_type in SQL_SOURCE_TYPES and str(row["id"]) not in sql_source_ids:
            raise MigrationError(
                f"发现未归档的 SQL 数据源 {row['id']}，拒绝假切换"
            )

    converted_sources = 0
    for scenario in SCENARIOS:
        ids = catalog_by_scenario[scenario.id]
        current = next(
            (row for row in source_rows if str(row["id"]) == scenario.sql_source_id),
            None,
        )
        if current is None:
            raise MigrationError(f"目标缺少 SQL 数据源 {scenario.sql_source_id}")
        desired_config = dataset_connector_config(
            dataset_id=ids["dataset_id"],
            dataset_version_id=ids["version_id"],
            binding_id=ids["binding_id"],
        )
        revision = int(current.get("connector_revision") or 1)
        if str(current.get("type")) != "dataset" or _canonical_value(
            current.get("config")
        ) != _canonical_value(desired_config):
            revision += 1
        connection.execute(
            update(data_sources)
            .where(data_sources.c.id == scenario.sql_source_id)
            .values(
                type="dataset",
                config=desired_config,
                scenario_id=None,
                connector_revision=revision,
                status="ok",
                last_error="",
            )
        )
        converted_sources += 1

        bucket_current = next(
            (row for row in source_rows if str(row["id"]) == scenario.bucket_source_id),
            None,
        )
        if bucket_current is None:
            raise MigrationError(f"目标缺少文件桶数据源 {scenario.bucket_source_id}")
        connection.execute(
            update(data_sources)
            .where(data_sources.c.id == scenario.bucket_source_id)
            .values(
                scenario_id=None,
                config=_sanitize_mapping(bucket_current.get("config") or {}),
            )
        )

    mapping_count = 0
    data_mappings = metadata.tables.get("data_mappings")
    mapping_relation_by_id: dict[str, str] = {}
    if data_mappings is not None:
        for scenario in SCENARIOS:
            ids = catalog_by_scenario[scenario.id]
            rows = [
                dict(row)
                for row in connection.execute(
                    select(data_mappings).where(
                        data_mappings.c.data_source_id == scenario.sql_source_id
                    )
                ).mappings()
            ]
            for row in rows:
                relation_name = str(row.get("table_name") or "")
                relation_id = ids["relation_ids"].get(relation_name)
                if not relation_id:
                    raise MigrationError(
                        f"数据映射 {row['id']} 指向非白名单关系 {relation_name!r}"
                    )
                values: dict[str, Any] = {
                    "data_source_binding_ref": _merged_binding_ref(
                        row.get("data_source_binding_ref"),
                        ids=ids,
                        relation_id=relation_id,
                    )
                }
                if "dataset_relation_id" in data_mappings.c:
                    values["dataset_relation_id"] = relation_id
                connection.execute(
                    update(data_mappings)
                    .where(data_mappings.c.id == row["id"])
                    .values(**values)
                )
                mapping_relation_by_id[str(row["id"])] = relation_id
                mapping_count += 1

    relation_mapping_count = 0
    relation_mappings = metadata.tables.get("relation_data_mappings")
    if relation_mappings is not None:
        for scenario in SCENARIOS:
            ids = catalog_by_scenario[scenario.id]
            rows = [
                dict(row)
                for row in connection.execute(
                    select(relation_mappings).where(
                        relation_mappings.c.data_source_id == scenario.sql_source_id
                    )
                ).mappings()
            ]
            for row in rows:
                relation_name = str(row.get("table_name") or "")
                relation_id = ids["relation_ids"].get(relation_name)
                if relation_name and not relation_id:
                    raise MigrationError(
                        f"关系映射 {row['id']} 指向非白名单关系 {relation_name!r}"
                    )
                if relation_id is None:
                    relation_id = mapping_relation_by_id.get(str(row.get("source_mapping_id")))
                values = {
                    "data_source_binding_ref": _merged_binding_ref(
                        row.get("data_source_binding_ref"),
                        ids=ids,
                        relation_id=relation_id,
                    )
                }
                if "dataset_relation_id" in relation_mappings.c:
                    values["dataset_relation_id"] = relation_id
                connection.execute(
                    update(relation_mappings)
                    .where(relation_mappings.c.id == row["id"])
                    .values(**values)
                )
                relation_mapping_count += 1

    # No stored connector config may retain a password/access key after the
    # cutover.  This applies to file sources too; MinIO credentials are process
    # configuration, never tenant data.
    for row in connection.execute(select(data_sources)).mappings():
        config = row.get("config") or {}
        sanitized = _sanitize_mapping(config)
        if _canonical_value(config) != _canonical_value(sanitized):
            connection.execute(
                update(data_sources)
                .where(data_sources.c.id == row["id"])
                .values(config=sanitized)
            )
    return {
        "converted_sql_sources": converted_sources,
        "data_mappings": mapping_count,
        "relation_data_mappings": relation_mapping_count,
    }


def _foreign_key_violations(connection: Any, metadata: Any) -> list[dict[str, Any]]:
    from sqlalchemy import and_, exists, func, select

    violations: list[dict[str, Any]] = []
    for child in metadata.tables.values():
        for constraint in child.foreign_key_constraints:
            elements = list(constraint.elements)
            if not elements:
                continue
            parent_table = elements[0].column.table
            parent = parent_table.alias(f"{parent_table.name}_fk_parent")
            local_columns = [element.parent for element in elements]
            remote_names = [element.column.name for element in elements]
            non_null = and_(*(column.is_not(None) for column in local_columns))
            match = and_(
                *(
                    parent.c[remote_name] == local
                    for local, remote_name in zip(
                        local_columns, remote_names, strict=True
                    )
                )
            )
            statement = select(func.count()).select_from(child).where(
                non_null, ~exists(select(1).select_from(parent).where(match))
            )
            count = int(connection.execute(statement).scalar_one())
            if count:
                violations.append(
                    {
                        "table": child.name,
                        "constraint": constraint.name or "<unnamed>",
                        "local_columns": [column.name for column in local_columns],
                        "referenced_table": parent_table.name,
                        "referenced_columns": remote_names,
                        "count": count,
                    }
                )
    return violations


def _normalize_nullable_set_null_orphans(
    connection: Any, metadata: Any
) -> list[dict[str, Any]]:
    """Apply declared SET NULL semantics to legacy dangling references only."""

    from sqlalchemy import and_, exists, func, select, update

    normalized: list[dict[str, Any]] = []
    for child in metadata.tables.values():
        for constraint in child.foreign_key_constraints:
            if str(constraint.ondelete or "").upper() != "SET NULL":
                continue
            elements = list(constraint.elements)
            if not elements:
                continue
            local_columns = [element.parent for element in elements]
            if any(not column.nullable for column in local_columns):
                continue
            parent_table = elements[0].column.table
            parent = parent_table.alias(f"{parent_table.name}_orphan_parent")
            remote_names = [element.column.name for element in elements]
            non_null = and_(*(column.is_not(None) for column in local_columns))
            match = and_(
                *(
                    parent.c[remote_name] == local
                    for local, remote_name in zip(
                        local_columns, remote_names, strict=True
                    )
                )
            )
            orphaned = and_(
                non_null,
                ~exists(select(1).select_from(parent).where(match)),
            )
            count = int(
                connection.execute(
                    select(func.count()).select_from(child).where(orphaned)
                ).scalar_one()
            )
            if not count:
                continue
            result = connection.execute(
                update(child)
                .where(orphaned)
                .values({column.name: None for column in local_columns})
            )
            if result.rowcount not in {-1, count}:
                raise MigrationError(
                    f"表 {child.name} 的 SET NULL 悬空引用规范化数量不一致"
                )
            normalized.append(
                {
                    "table": child.name,
                    "constraint": constraint.name or "<unnamed>",
                    "local_columns": [column.name for column in local_columns],
                    "referenced_table": parent_table.name,
                    "referenced_columns": remote_names,
                    "count": count,
                }
            )
    return normalized


def _record_target_checkpoint(
    connection: Any,
    manifest: Mapping[str, Any],
    *,
    stage: str,
    item_key: str,
    payload: Mapping[str, Any],
    row_count: int | None = None,
) -> None:
    from sqlalchemy import text

    digest = _sha256_json(payload)
    existing = connection.execute(
        text(
            """
            SELECT payload_sha256 FROM platform_migration_checkpoints
             WHERE run_id = :run_id AND stage = :stage AND item_key = :item_key
            """
        ),
        {"run_id": manifest["run_id"], "stage": stage, "item_key": item_key},
    ).scalar_one_or_none()
    if existing is not None:
        if str(existing) != digest:
            raise MigrationError(f"目标检查点 {stage}:{item_key} 内容冲突")
        return
    connection.execute(
        text(
            """
            INSERT INTO platform_migration_checkpoints
              (run_id, stage, item_key, status, payload_sha256, row_count,
               payload, completed_at)
            VALUES
              (:run_id, :stage, :item_key, 'complete', :payload_sha256,
               :row_count, CAST(:payload AS JSONB), :completed_at)
            """
        ),
        {
            "run_id": manifest["run_id"],
            "stage": stage,
            "item_key": item_key,
            "payload_sha256": digest,
            "row_count": row_count,
            "payload": _canonical_json(payload),
            "completed_at": _utc_now(),
        },
    )


def _assert_target_run_identity(
    connection: Any, manifest: Mapping[str, Any]
) -> None:
    from sqlalchemy import text

    row = connection.execute(
        text(
            """
            SELECT migration_name, plan_digest, source_fingerprint
              FROM platform_migration_runs
             WHERE id = :run_id
            """
        ),
        {"run_id": manifest["run_id"]},
    ).mappings().one_or_none()
    if row is None:
        raise MigrationError("PostgreSQL 不存在当前 migration run")
    expected = {
        "migration_name": MIGRATION_NAME,
        "plan_digest": str(manifest["plan_digest"]),
        "source_fingerprint": str(manifest["source"]["source_fingerprint"]),
    }
    actual = {key: str(row[key]) for key in expected}
    if actual != expected:
        raise MigrationError(
            "PostgreSQL migration run 身份冲突：" + _canonical_json(actual)
        )


def _read_target_checkpoint(
    connection: Any,
    manifest: Mapping[str, Any],
    *,
    stage: str,
    item_key: str,
) -> dict[str, Any] | None:
    from sqlalchemy import text

    _assert_target_run_identity(connection, manifest)
    row = connection.execute(
        text(
            """
            SELECT status, payload_sha256, payload
              FROM platform_migration_checkpoints
             WHERE run_id = :run_id AND stage = :stage AND item_key = :item_key
            """
        ),
        {"run_id": manifest["run_id"], "stage": stage, "item_key": item_key},
    ).mappings().one_or_none()
    if row is None:
        return None
    if str(row["status"]) not in {"complete", "verified"}:
        raise MigrationError(f"目标检查点 {stage}:{item_key} 状态不完整")
    payload: Any = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise MigrationError(
                f"目标检查点 {stage}:{item_key} payload 不是有效 JSON"
            ) from exc
    if not isinstance(payload, Mapping):
        raise MigrationError(f"目标检查点 {stage}:{item_key} payload 类型错误")
    restored = dict(payload)
    expected_digest = _sha256_json(restored)
    if not secrets.compare_digest(str(row["payload_sha256"]), expected_digest):
        raise MigrationError(f"目标检查点 {stage}:{item_key} payload 哈希损坏")
    source_fingerprint = str(restored.get("source_fingerprint") or "")
    if source_fingerprint != str(manifest["source"]["source_fingerprint"]):
        raise MigrationError(f"目标检查点 {stage}:{item_key} 源指纹不匹配")
    return restored


def _find_target_checkpoint(
    connection: Any,
    manifest: Mapping[str, Any],
    *,
    stage: str,
    item_keys: Sequence[str],
) -> tuple[str, dict[str, Any]] | None:
    for item_key in item_keys:
        payload = _read_target_checkpoint(
            connection,
            manifest,
            stage=stage,
            item_key=item_key,
        )
        if payload is not None:
            return item_key, payload
    return None


def _store_authoritative_local_checkpoint(
    manifest: MutableMapping[str, Any],
    *,
    stage: str,
    item_key: str,
    payload: Mapping[str, Any],
) -> None:
    """Mirror a committed PostgreSQL checkpoint into the repairable local file."""

    key = _checkpoint_key(stage, item_key)
    previous = (manifest.get("checkpoints") or {}).get(key) or {}
    manifest.setdefault("checkpoints", {})[key] = {
        "stage": stage,
        "item_key": item_key,
        "status": "complete",
        "payload_sha256": _sha256_json(payload),
        "payload": dict(payload),
        "completed_at": previous.get("completed_at") or _utc_iso(),
        "authority": "postgresql",
    }


def _complete_local_phase_from_target(
    settings: MigrationSettings,
    manifest: MutableMapping[str, Any],
    *,
    phase: str,
    item_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    _store_authoritative_local_checkpoint(
        manifest,
        stage=phase,
        item_key=item_key,
        payload=payload,
    )
    if phase == "verify":
        manifest["verification"] = dict(payload)
    _mark_phase(manifest, phase, "complete")
    _write_json_atomic(settings.manifest_path, manifest)
    return dict(payload)


def _update_target_run_state(
    connection: Any,
    manifest: Mapping[str, Any],
    *,
    phase: str,
    status: str,
    completed: bool = False,
) -> None:
    from sqlalchemy import text

    result = connection.execute(
        text(
            """
            UPDATE platform_migration_runs
               SET current_phase = :phase, status = :status,
                   updated_at = :updated_at,
                   completed_at = CASE
                       WHEN :completed THEN :completed_at
                       ELSE completed_at
                   END,
                   last_error = ''
             WHERE id = :run_id
               AND migration_name = :migration_name
               AND plan_digest = :plan_digest
               AND source_fingerprint = :source_fingerprint
            """
        ),
        {
            "phase": phase,
            "status": status,
            "updated_at": _utc_now(),
            "completed": completed,
            "completed_at": _utc_now() if completed else None,
            "run_id": manifest["run_id"],
            "migration_name": MIGRATION_NAME,
            "plan_digest": manifest["plan_digest"],
            "source_fingerprint": manifest["source"]["source_fingerprint"],
        },
    )
    if result.rowcount not in {-1, 1}:
        raise MigrationError("PostgreSQL migration run 状态更新未命中当前计划")


def import_to_postgresql(
    settings: MigrationSettings,
    manifest: MutableMapping[str, Any],
    *,
    confirmation: str,
    batch_size: int,
) -> dict[str, Any]:
    from sqlalchemy import text

    _require_prerequisites(manifest, "import")
    _require_confirmation(manifest, "import", confirmation)
    target_engine = _postgres_engine(settings)
    source_engine = None
    try:
        # PostgreSQL is authoritative once the import transaction commits.  A
        # crash before the local manifest write must never replay converted
        # control-plane rows as if the target were empty.
        with target_engine.begin() as target_connection:
            target_connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": MIGRATION_NAME},
            )
            recovered = _find_target_checkpoint(
                target_connection,
                manifest,
                stage="import",
                item_keys=(
                    IMPORT_TARGET_CHECKPOINT,
                    LEGACY_IMPORT_TARGET_CHECKPOINT,
                ),
            )
        if recovered is not None:
            return _complete_local_phase_from_target(
                settings,
                manifest,
                phase="import",
                item_key=recovered[0],
                payload=recovered[1],
            )

        _mark_phase(manifest, "import", "running")
        _write_json_atomic(settings.manifest_path, manifest)
        source_engine = _mysql_engine(settings)
        target_metadata = _load_orm_metadata()
        platform_results: dict[str, Any] = {}
        catalog_results: dict[str, Any] = {}
        with _mysql_readonly_snapshot(source_engine) as source_connection:
            source_metadata = _reflect_source_metadata(source_connection)
            platform_names = _platform_table_names(source_metadata, target_metadata)
            _validate_exact_scenarios(_scenario_rows(source_connection, source_metadata))
            with target_engine.begin() as target_connection:
                target_connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": MIGRATION_NAME},
                )
                target_connection.exec_driver_sql("SET LOCAL timezone = 'UTC'")
                target_connection.exec_driver_sql(
                    "SET LOCAL session_replication_role = 'replica'"
                )
                for table_name in platform_names:
                    result = _copy_platform_table(
                        source_connection=source_connection,
                        target_connection=target_connection,
                        source_table=source_metadata.tables[table_name],
                        target_table=target_metadata.tables[table_name],
                        planned=manifest["source"]["platform"][table_name],
                        batch_size=batch_size,
                    )
                    platform_results[table_name] = result

                for scenario in SCENARIOS:
                    catalog_results[scenario.id] = _register_dataset_catalog(
                        target_connection, target_metadata, manifest, scenario
                    )
                binding_result = _convert_data_sources_and_mappings(
                    target_connection,
                    target_metadata,
                    manifest,
                    catalog_results,
                )
                normalized_orphans = _normalize_nullable_set_null_orphans(
                    target_connection, target_metadata
                )
                post_import_platform = {
                    table_name: _target_digest(
                        target_connection,
                        target_metadata.tables[table_name],
                        column_names=manifest["source"]["platform"][table_name][
                            "columns"
                        ],
                        batch_size=batch_size,
                    )
                    for table_name in platform_names
                }
                target_connection.exec_driver_sql(
                    "SET LOCAL session_replication_role = 'origin'"
                )
                fk_violations = _foreign_key_violations(
                    target_connection, target_metadata
                )
                if fk_violations:
                    raise MigrationError(
                        "PostgreSQL 外键闭包验证失败：" + _canonical_json(fk_violations)
                    )
                summary = {
                    "checkpoint_contract_version": 2,
                    "plan_digest": manifest["plan_digest"],
                    "platform_table_count": len(platform_results),
                    "platform_row_count": sum(
                        int(result["row_count"])
                        for result in platform_results.values()
                    ),
                    "catalog": catalog_results,
                    "bindings": binding_result,
                    "normalized_set_null_orphans": normalized_orphans,
                    "post_import_platform": post_import_platform,
                    "foreign_key_violations": 0,
                    "source_fingerprint": manifest["source"]["source_fingerprint"],
                }
                _record_target_checkpoint(
                    target_connection,
                    manifest,
                    stage="import",
                    item_key=IMPORT_TARGET_CHECKPOINT,
                    payload=summary,
                    row_count=summary["platform_row_count"],
                )
                _update_target_run_state(
                    target_connection,
                    manifest,
                    phase="import",
                    status="running",
                )
        return _complete_local_phase_from_target(
            settings,
            manifest,
            phase="import",
            item_key=IMPORT_TARGET_CHECKPOINT,
            payload=summary,
        )
    except Exception as exc:
        error = _safe_error(settings, exc)
        _mark_phase(manifest, "import", "failed", error=error)
        try:
            with target_engine.connect() as target_connection:
                committed = _find_target_checkpoint(
                    target_connection,
                    manifest,
                    stage="import",
                    item_keys=(
                        IMPORT_TARGET_CHECKPOINT,
                        LEGACY_IMPORT_TARGET_CHECKPOINT,
                    ),
                )
            if committed is None:
                _sync_migration_run(
                    settings, manifest, phase="import", status="failed", error=error
                )
        except Exception:
            pass
        _write_json_atomic(settings.manifest_path, manifest)
        raise MigrationError(f"import 失败：{error}") from exc
    finally:
        if source_engine is not None:
            source_engine.dispose()
        target_engine.dispose()


def _deep_verify_parquet_object(
    client: Any,
    settings: MigrationSettings,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        import duckdb
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - dependency validation
        raise MigrationError("端到端验证需要 duckdb 和 pyarrow") from exc
    object_info = payload["object"]
    with tempfile.TemporaryDirectory(prefix="ontology-pg-verify-") as temp_dir:
        path = Path(temp_dir) / "fragment.parquet"
        client.fget_object(
            str(object_info["bucket"]),
            str(object_info["object_key"]),
            str(path),
            version_id=str(object_info.get("object_version_id") or "") or None,
        )
        content_sha = _file_sha256(path)
        if content_sha != payload["content_sha256"]:
            raise MigrationError(
                f"Parquet 下载内容哈希错误：{object_info['object_key']}"
            )
        parquet_rows = int(pq.ParquetFile(str(path)).metadata.num_rows)
        with duckdb.connect(database=":memory:") as connection:
            duckdb_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM read_parquet(?)", [str(path)]
                ).fetchone()[0]
            )
            # This reads actual columns through the same engine used by the
            # dataset connector; LIMIT 1 is valid even for an empty relation.
            connection.execute(
                "SELECT * FROM read_parquet(?) LIMIT 1", [str(path)]
            ).fetchall()
        expected_rows = int(payload["row_count"])
        if parquet_rows != expected_rows or duckdb_rows != expected_rows:
            raise MigrationError(
                f"Parquet 行数验证失败：{object_info['object_key']}"
            )
        return {
            "content_sha256": content_sha,
            "parquet_rows": parquet_rows,
            "duckdb_rows": duckdb_rows,
        }


def _verify_target_catalog(
    connection: Any,
    metadata: Any,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    from sqlalchemy import func, select

    catalog_results: dict[str, Any] = {}
    for scenario in SCENARIOS:
        ids = _catalog_ids(manifest, scenario)
        version = connection.execute(
            select(metadata.tables["dataset_versions"]).where(
                metadata.tables["dataset_versions"].c.id == ids["version_id"]
            )
        ).mappings().first()
        if (
            version is None
            or version["status"] != "ready"
            or str(version["tenant_id"]) != ids["tenant_id"]
            or str(version["dataset_id"]) != ids["dataset_id"]
            or str(version["schema_id"]) != ids["schema_id"]
            or str(version["manifest_data_source_id"]) != scenario.bucket_source_id
        ):
            raise MigrationError(f"{scenario.key} 数据集版本未 ready")
        fragments = metadata.tables["dataset_fragments"]
        fragment_rows = int(
            connection.execute(
                select(func.coalesce(func.sum(fragments.c.row_count), 0)).where(
                    fragments.c.dataset_version_id == ids["version_id"]
                )
            ).scalar_one()
        )
        fragment_count = int(
            connection.execute(
                select(func.count()).where(
                    fragments.c.dataset_version_id == ids["version_id"]
                )
            ).scalar_one()
        )
        if fragment_rows != int(version["record_count"]):
            raise MigrationError(f"{scenario.key} Fragment/版本行数不一致")
        if fragment_count != len(scenario.relations):
            raise MigrationError(f"{scenario.key} Fragment 数不一致")
        invalid_fragment_scope = int(
            connection.execute(
                select(func.count()).where(
                    fragments.c.dataset_version_id == ids["version_id"],
                    (
                        (fragments.c.tenant_id != ids["tenant_id"])
                        | (fragments.c.dataset_id != ids["dataset_id"])
                        | (fragments.c.schema_id != ids["schema_id"])
                        | (
                            fragments.c.bucket_data_source_id
                            != scenario.bucket_source_id
                        )
                    ),
                )
            ).scalar_one()
        )
        if invalid_fragment_scope:
            raise MigrationError(f"{scenario.key} Fragment Schema 归属不一致")
        binding = connection.execute(
            select(metadata.tables["scenario_dataset_bindings"]).where(
                metadata.tables["scenario_dataset_bindings"].c.id == ids["binding_id"]
            )
        ).mappings().first()
        if (
            binding is None
            or binding["status"] != "active"
            or str(binding["tenant_id"]) != ids["tenant_id"]
            or str(binding["dataset_id"]) != ids["dataset_id"]
            or str(binding["dataset_head_id"]) != ids["head_id"]
            or binding["dataset_version_id"] is not None
        ):
            raise MigrationError(f"{scenario.key} 场景数据集绑定未激活")
        source = connection.execute(
            select(metadata.tables["data_sources"]).where(
                metadata.tables["data_sources"].c.id == scenario.sql_source_id
            )
        ).mappings().one()
        if source["type"] != "dataset":
            raise MigrationError(f"{scenario.key} SQL 数据源未转为 dataset")
        if source["scenario_id"] is not None:
            raise MigrationError(f"{scenario.key} dataset 连接器仍被场景所有")
        config = source["config"] or {}
        if _canonical_value(config) != _canonical_value(_sanitize_mapping(config)):
            raise MigrationError(f"{scenario.key} dataset 连接器仍含凭据")
        if str(config.get("dataset_version_id")) != ids["version_id"]:
            raise MigrationError(f"{scenario.key} dataset 连接器版本错误")
        catalog_results[scenario.key] = {
            "dataset_id": ids["dataset_id"],
            "version_id": ids["version_id"],
            "fragment_count": fragment_count,
            "record_count": fragment_rows,
        }
    for row in connection.execute(select(metadata.tables["data_sources"])).mappings():
        if str(row.get("type") or "").lower() in SQL_SOURCE_TYPES:
            raise MigrationError(f"目标仍存在直连 SQL 数据源：{row['id']}")
        config = row.get("config") or {}
        if _canonical_value(config) != _canonical_value(_sanitize_mapping(config)):
            raise MigrationError(f"目标数据源 {row['id']} 仍含凭据键")
    return catalog_results


def verify_migration(
    settings: MigrationSettings,
    manifest: MutableMapping[str, Any],
    *,
    batch_size: int,
    deep: bool = True,
) -> dict[str, Any]:
    from sqlalchemy import select, text

    target_engine = _postgres_engine(settings)
    try:
        local_verify_complete = "verify" in _completed_phases(manifest)
        checkpoint_item = (
            VERIFY_DEEP_CHECKPOINT if deep else VERIFY_SHALLOW_CHECKPOINT
        )
        with target_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": MIGRATION_NAME},
            )
            import_checkpoint = _find_target_checkpoint(
                connection,
                manifest,
                stage="import",
                item_keys=(
                    IMPORT_TARGET_CHECKPOINT,
                    LEGACY_IMPORT_TARGET_CHECKPOINT,
                ),
            )
            recovered_verification = _read_target_checkpoint(
                connection,
                manifest,
                stage="verify",
                item_key=checkpoint_item,
            )
        if import_checkpoint is None:
            raise MigrationError("PostgreSQL 缺少 import 权威检查点")
        _store_authoritative_local_checkpoint(
            manifest,
            stage="import",
            item_key=import_checkpoint[0],
            payload=import_checkpoint[1],
        )
        _mark_phase(manifest, "import", "complete")
        if not local_verify_complete and recovered_verification is not None:
            return _complete_local_phase_from_target(
                settings,
                manifest,
                phase="verify",
                item_key=checkpoint_item,
                payload=recovered_verification,
            )
        _require_prerequisites(manifest, "verify")

        client = _minio_client(settings)
        source_inventory = _source_inventory(settings, batch_size=batch_size)
        if (
            source_inventory["source_fingerprint"]
            != manifest["source"]["source_fingerprint"]
        ):
            raise MigrationError("MySQL 回退源在 import 后发生变化")
        object_verification: dict[str, Any] = {}
        for scenario in SCENARIOS:
            dataset_archive = manifest["archive"]["datasets"][scenario.key]
            for relation_name in scenario.relations:
                payload = dataset_archive["relations"][relation_name]
                _verify_archived_object(client, settings, payload)
                if deep:
                    object_verification[f"{scenario.key}/{relation_name}"] = (
                        _deep_verify_parquet_object(client, settings, payload)
                    )
            _verify_archived_object(client, settings, dataset_archive["manifest"])

        target_metadata = _load_orm_metadata()
        imported = import_checkpoint[1]
        with target_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": MIGRATION_NAME},
            )
            scenario_rows = [
                dict(row)
                for row in connection.execute(
                    select(
                        target_metadata.tables["business_scenarios"].c.id,
                        target_metadata.tables["business_scenarios"].c.name,
                        target_metadata.tables["business_scenarios"].c.tenant_id,
                    )
                ).mappings()
            ]
            _validate_exact_scenarios(scenario_rows)
            platform_hashes: dict[str, Any] = {}
            for table_name, expected in imported["post_import_platform"].items():
                actual = _target_digest(
                    connection,
                    target_metadata.tables[table_name],
                    column_names=expected["columns"],
                    batch_size=batch_size,
                )
                if (
                    actual["row_count"] != expected["row_count"]
                    or actual["row_hash"] != expected["row_hash"]
                ):
                    raise MigrationError(f"PostgreSQL 平台表 {table_name} 验证失败")
                platform_hashes[table_name] = actual
            catalog_results = _verify_target_catalog(
                connection, target_metadata, manifest
            )
            fk_violations = _foreign_key_violations(connection, target_metadata)
            if fk_violations:
                raise MigrationError("PostgreSQL 外键闭包破损：" + _canonical_json(fk_violations))
            result = {
                "checkpoint_contract_version": 2,
                "plan_digest": manifest["plan_digest"],
                "deep_object_verification": deep,
                "source_fingerprint": source_inventory["source_fingerprint"],
                "platform_table_count": len(platform_hashes),
                "catalog": catalog_results,
                "objects": object_verification,
                "foreign_key_violations": 0,
                "mysql_preserved": True,
            }
            _record_target_checkpoint(
                connection,
                manifest,
                stage="verify",
                item_key=checkpoint_item,
                payload=result,
            )
            _update_target_run_state(
                connection,
                manifest,
                phase="verify",
                status="verified",
                completed=True,
            )
        return _complete_local_phase_from_target(
            settings,
            manifest,
            phase="verify",
            item_key=checkpoint_item,
            payload=result,
        )
    except Exception as exc:
        error = _safe_error(settings, exc)
        _mark_phase(manifest, "verify", "failed", error=error)
        _write_json_atomic(settings.manifest_path, manifest)
        raise MigrationError(f"verify 失败：{error}") from exc
    finally:
        target_engine.dispose()


def _render_cutover_env(
    current_text: str,
    *,
    database: str,
    runtime_user: str,
    runtime_password: str,
    admin_user: str | None = None,
    admin_password: str | None = None,
) -> str:
    replacements = {
        "DATABASE_BACKEND": "postgresql",
        "POSTGRESQL_DATABASE": database,
        "POSTGRESQL_USER": runtime_user,
        "POSTGRESQL_PASSWORD": runtime_password,
    }
    if admin_user:
        replacements["POSTGRESQL_ADMIN_USER"] = admin_user
    if admin_password:
        replacements["POSTGRESQL_ADMIN_PASSWORD"] = admin_password
    lines = current_text.splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in replacements:
            if key not in seen:
                output.append(f"{key}={replacements[key]}")
                seen.add(key)
        elif key in {"DATABASE_URL", "POSTGRESQL_RUNTIME_PASSWORD"}:
            # The runtime password becomes the canonical POSTGRESQL_PASSWORD;
            # retaining a second copy creates ambiguous precedence. DATABASE_URL
            # has higher precedence than DATABASE_BACKEND and must not survive.
            continue
        else:
            output.append(line)
    for key, value in replacements.items():
        if key not in seen:
            output.append(f"{key}={value}")
    return "\n".join(output).rstrip("\n") + "\n"


def _render_mysql_rollback_env(current_text: str) -> str:
    lines = current_text.splitlines()
    output: list[str] = []
    backend_written = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key == "DATABASE_URL":
            continue
        if key == "DATABASE_BACKEND":
            if not backend_written:
                output.append("DATABASE_BACKEND=mysql")
                backend_written = True
            continue
        output.append(line)
    if not backend_written:
        output.append("DATABASE_BACKEND=mysql")
    return "\n".join(output).rstrip("\n") + "\n"


def _active_dotenv_values(content: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _assert_database_selector(content: str, *, backend: str) -> None:
    values = _active_dotenv_values(content)
    if values.get("DATABASE_BACKEND", "").lower() != backend:
        raise MigrationError(f"env 未明确选择 DATABASE_BACKEND={backend}")
    if "DATABASE_URL" in values:
        raise MigrationError("env 仍含 DATABASE_URL，会覆盖 DATABASE_BACKEND")
    if backend == "mysql":
        required = (
            "ANNUAL_MYSQL_HOST",
            "ANNUAL_MYSQL_PORT",
            "ANNUAL_MYSQL_DATABASE",
            "ANNUAL_MYSQL_USER",
        )
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise MigrationError(
                "MySQL rollback env 缺少配置：" + ", ".join(missing)
            )


def _atomic_write_text(path: Path, content: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def cutover_environment(
    settings: MigrationSettings,
    manifest: MutableMapping[str, Any],
    *,
    confirmation: str,
) -> dict[str, Any]:
    from sqlalchemy import text

    _require_confirmation(manifest, "cutover", confirmation)
    if str(os.environ.get("DATABASE_URL", "")).strip():
        raise MigrationError(
            "进程环境中的 DATABASE_URL 会覆盖 DATABASE_BACKEND；请清除后重试"
        )
    current = settings.env_file.read_text(encoding="utf-8-sig")
    rendered = _render_cutover_env(
        current,
        database=settings.postgresql_target_database,
        runtime_user=settings.postgresql_runtime_role,
        runtime_password=settings.postgresql_runtime_password,
        admin_user=settings.postgresql_admin_user,
        admin_password=settings.postgresql_admin_password,
    )
    _assert_database_selector(rendered, backend="postgresql")
    rollback_rendered = _render_mysql_rollback_env(current)
    _assert_database_selector(rollback_rendered, backend="mysql")
    backup = settings.env_file.with_name(
        f"{settings.env_file.name}.pre-mysql-rollback.{manifest['run_id']}.bak"
    )
    common = {
        "checkpoint_contract_version": 2,
        "plan_digest": manifest["plan_digest"],
        "source_fingerprint": manifest["source"]["source_fingerprint"],
        "database_backend": "postgresql",
        "database": settings.postgresql_target_database,
        "runtime_role": settings.postgresql_runtime_role,
        "rollback_env_backup": str(backup),
        "rollback_database_backend": "mysql",
        "mysql_preserved": True,
        "mysql_deletion_performed": False,
    }
    prepared_payload = {**common, "state": "prepared"}
    finalized_payload = {**common, "state": "finalized"}

    target_engine = _postgres_engine(settings)
    try:
        with target_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": MIGRATION_NAME},
            )
            imported = _find_target_checkpoint(
                connection,
                manifest,
                stage="import",
                item_keys=(
                    IMPORT_TARGET_CHECKPOINT,
                    LEGACY_IMPORT_TARGET_CHECKPOINT,
                ),
            )
            verified = _find_target_checkpoint(
                connection,
                manifest,
                stage="verify",
                item_keys=(VERIFY_DEEP_CHECKPOINT, LEGACY_VERIFY_CHECKPOINT),
            )
            if imported is not None:
                _store_authoritative_local_checkpoint(
                    manifest,
                    stage="import",
                    item_key=imported[0],
                    payload=imported[1],
                )
                _mark_phase(manifest, "import", "complete")
            if verified is not None:
                if not bool(verified[1].get("deep_object_verification")):
                    raise MigrationError("cutover 必须依赖 deep verify 检查点")
                _store_authoritative_local_checkpoint(
                    manifest,
                    stage="verify",
                    item_key=verified[0],
                    payload=verified[1],
                )
                manifest["verification"] = dict(verified[1])
                _mark_phase(manifest, "verify", "complete")
            _require_prerequisites(manifest, "cutover")
            if imported is None or verified is None:
                raise MigrationError("PostgreSQL 缺少 import/deep verify 权威检查点")

            existing_prepared = _read_target_checkpoint(
                connection,
                manifest,
                stage="cutover",
                item_key=CUTOVER_PREPARED_CHECKPOINT,
            )
            existing_finalized = _read_target_checkpoint(
                connection,
                manifest,
                stage="cutover",
                item_key=CUTOVER_FINALIZED_CHECKPOINT,
            )
            if existing_prepared is not None and existing_prepared != prepared_payload:
                raise MigrationError("cutover prepared 检查点与当前计划冲突")
            if existing_finalized is not None and existing_finalized != finalized_payload:
                raise MigrationError("cutover finalized 检查点与当前计划冲突")
            if existing_prepared is None:
                _record_target_checkpoint(
                    connection,
                    manifest,
                    stage="cutover",
                    item_key=CUTOVER_PREPARED_CHECKPOINT,
                    payload=prepared_payload,
                )
            _update_target_run_state(
                connection,
                manifest,
                phase="cutover",
                status="verified",
            )

        # The rollback file is independently usable and always selects MySQL;
        # rewriting it also repairs the invalid backup produced by the v1 live run.
        _atomic_write_text(backup, rollback_rendered)
        _assert_database_selector(
            backup.read_text(encoding="utf-8-sig"), backend="mysql"
        )
        _atomic_write_text(settings.env_file, rendered)
        _assert_database_selector(
            settings.env_file.read_text(encoding="utf-8-sig"),
            backend="postgresql",
        )

        with target_engine.begin() as connection:
            connection.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": MIGRATION_NAME},
            )
            prepared = _read_target_checkpoint(
                connection,
                manifest,
                stage="cutover",
                item_key=CUTOVER_PREPARED_CHECKPOINT,
            )
            if prepared != prepared_payload:
                raise MigrationError("cutover prepared 检查点缺失或损坏")
            _record_target_checkpoint(
                connection,
                manifest,
                stage="cutover",
                item_key=CUTOVER_FINALIZED_CHECKPOINT,
                payload=finalized_payload,
            )
            _update_target_run_state(
                connection,
                manifest,
                phase="cutover",
                status="cutover",
                completed=True,
            )

        _store_authoritative_local_checkpoint(
            manifest,
            stage="cutover",
            item_key=CUTOVER_PREPARED_CHECKPOINT,
            payload=prepared_payload,
        )
        return _complete_local_phase_from_target(
            settings,
            manifest,
            phase="cutover",
            item_key=CUTOVER_FINALIZED_CHECKPOINT,
            payload=finalized_payload,
        )
    except Exception as exc:
        error = _safe_error(settings, exc)
        _mark_phase(manifest, "cutover", "failed", error=error)
        _write_json_atomic(settings.manifest_path, manifest)
        raise MigrationError(f"cutover 失败，可安全重跑收敛：{error}") from exc
    finally:
        target_engine.dispose()


def _assert_settings_match_manifest(
    settings: MigrationSettings, manifest: Mapping[str, Any]
) -> None:
    expected = manifest.get("connections") or {}
    actual = settings.public_summary()
    for backend in ("mysql", "minio"):
        if _canonical_value(expected.get(backend)) != _canonical_value(actual.get(backend)):
            raise MigrationError(f"{backend} 连接身份与 plan 不一致")
    expected_pg = expected.get("postgresql") or {}
    actual_pg = actual["postgresql"]
    for key in ("host", "port", "database", "owner_role", "runtime_role", "readonly_role"):
        if expected_pg.get(key) != actual_pg.get(key):
            raise MigrationError(f"postgresql.{key} 与 plan 不一致")


def _default_paths(arguments: argparse.Namespace) -> tuple[Path, Path, Path]:
    backend_root = Path(arguments.backend_root).resolve()
    env_file = (
        Path(arguments.env_file).resolve()
        if arguments.env_file
        else backend_root / ".env"
    )
    manifest_path = (
        Path(arguments.manifest).resolve()
        if arguments.manifest
        else backend_root / "migration-manifests" / "mysql-to-postgresql.json"
    )
    return backend_root, env_file, manifest_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="将平台控制面迁移到 PostgreSQL，业务数据归档到 MinIO/Parquet"
    )
    parser.add_argument("phase", nargs="?", choices=PHASES, default="plan")
    parser.add_argument(
        "--backend-root", default=str(Path(__file__).resolve().parents[1])
    )
    parser.add_argument("--env-file")
    parser.add_argument("--manifest")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument(
        "--replace-plan",
        action="store_true",
        help="仅在尚未执行任何远程写入阶段时替换旧 plan",
    )
    parser.add_argument(
        "--shallow-verify",
        action="store_true",
        help="跳过 Parquet 下载/DuckDB 读取（不适用于最终切换）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    if not 100 <= arguments.batch_size <= 20_000:
        parser.error("--batch-size 必须在 100..20000 之间")
    if arguments.replace_plan and arguments.phase != "plan":
        parser.error("--replace-plan 只能与 plan 一起使用")
    if arguments.shallow_verify and arguments.phase != "verify":
        parser.error("--shallow-verify 只能与 verify 一起使用")
    backend_root, env_file, manifest_path = _default_paths(arguments)
    try:
        settings = load_settings(
            backend_root=backend_root,
            env_file=env_file,
            manifest_path=manifest_path,
        )
        if arguments.phase == "plan":
            if manifest_path.exists():
                existing = load_manifest(manifest_path)
                completed_remote = _completed_phases(existing) & set(MUTATING_PHASES)
                if completed_remote:
                    raise MigrationError(
                        "旧清单已有远程写入阶段，拒绝覆盖："
                        + ", ".join(sorted(completed_remote))
                    )
                if not arguments.replace_plan:
                    raise MigrationError("清单已存在；确认丢弃未执行 plan 后使用 --replace-plan")
            manifest = build_plan(settings, batch_size=arguments.batch_size)
            _write_json_atomic(manifest_path, manifest)
            print(f"manifest={manifest_path}")
            print(f"plan_digest={manifest['plan_digest']}")
            for phase in MUTATING_PHASES:
                print(f"confirm_{phase}={manifest['confirmations'][phase]}")
            print("remote_writes=0")
            return 0

        manifest = load_manifest(manifest_path)
        _assert_settings_match_manifest(settings, manifest)
        if arguments.phase == "bootstrap":
            result = bootstrap_target(
                settings, manifest, confirmation=arguments.confirm
            )
        elif arguments.phase == "archive":
            result = archive_business_datasets(
                settings,
                manifest,
                confirmation=arguments.confirm,
                batch_size=arguments.batch_size,
            )
        elif arguments.phase == "import":
            result = import_to_postgresql(
                settings,
                manifest,
                confirmation=arguments.confirm,
                batch_size=arguments.batch_size,
            )
        elif arguments.phase == "verify":
            result = verify_migration(
                settings,
                manifest,
                batch_size=arguments.batch_size,
                deep=not arguments.shallow_verify,
            )
        else:
            if arguments.shallow_verify:
                raise MigrationError("浅验证结果不能用于 cutover")
            result = cutover_environment(
                settings, manifest, confirmation=arguments.confirm
            )
        print(_canonical_json(_sanitize_mapping(result)))
        return 0
    except (MigrationError, OSError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
