"""Deterministic, read-only medical insurance audit strategies.

The Agent-facing contract is deliberately smaller than the underlying medical
schema.  Callers select a versioned strategy and provide business values only;
table names, column names and SQL are never accepted from the model.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError

from ..models import DataSource
from . import dataset_query_service, datasource_service


AUDIT_VERSION = "medical-audit-v1"
STRATEGIES = frozenset(
    {
        "charge_threshold",
        "daily_overstay",
        "included_service_duplicate",
        "limited_drug_duration",
    }
)
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_FACILITY_SCOPE_MATCHES = 256
RESULT_SCHEMA_VERSION = 2
MAPPING_CONTRACT_VERSION = 1


def _property(entity_api_name: str, property_api_name: str) -> str:
    return f"{entity_api_name}.{property_api_name}"


_CHARGE = "medical_charge_line"
_ENCOUNTER = "medical_encounter"

C_CHARGE_ID = _property(_CHARGE, "charge_line_id")
C_ENCOUNTER_ID = _property(_CHARGE, "encounter_id")
C_FACILITY_NAME = _property(_CHARGE, "facility_name")
C_PATIENT_ID = _property(_CHARGE, "patient_id")
C_SERVICE_CODE = _property(_CHARGE, "service_code")
C_SERVICE_NAME = _property(_CHARGE, "service_name")
C_QUANTITY = _property(_CHARGE, "quantity")
C_UNIT_PRICE = _property(_CHARGE, "unit_price")
C_AMOUNT = _property(_CHARGE, "total_amount")
C_ELIGIBLE_AMOUNT = _property(_CHARGE, "eligible_amount")
C_OCCURRED_AT = _property(_CHARGE, "occurred_at")
C_CYCLE_DAYS = _property(_CHARGE, "cycle_days")

E_ENCOUNTER_ID = _property(_ENCOUNTER, "encounter_id")
E_PATIENT_ID = _property(_ENCOUNTER, "patient_id")
E_FACILITY_NAME = _property(_ENCOUNTER, "facility_name")
E_STARTED_AT = _property(_ENCOUNTER, "started_at")
E_ENDED_AT = _property(_ENCOUNTER, "ended_at")
E_STAY_DAYS = _property(_ENCOUNTER, "hospitalization_days")
E_DIAGNOSIS_NAME = _property(_ENCOUNTER, "diagnosis_name")

PropertyRequirement = tuple[frozenset[str], ...]


def _all_of(*properties: str) -> PropertyRequirement:
    return (frozenset(properties),)


def _one_of(*alternatives: tuple[str, ...]) -> PropertyRequirement:
    return tuple(frozenset(alternative) for alternative in alternatives)


_STAY_REQUIREMENT = _one_of(
    (E_STAY_DAYS,),
    (E_STARTED_AT, E_ENDED_AT),
)
_DURATION_REQUIREMENT = _one_of((C_OCCURRED_AT,), (C_CYCLE_DAYS,))
_DAILY_PATIENT_REQUIREMENT = _one_of((E_PATIENT_ID,), (C_PATIENT_ID,))

_STRATEGY_EXECUTION_REQUIREMENTS: dict[str, PropertyRequirement] = {
    "charge_threshold": _all_of(
        C_CHARGE_ID,
        C_ENCOUNTER_ID,
        C_FACILITY_NAME,
        C_SERVICE_NAME,
        C_QUANTITY,
        C_AMOUNT,
    ),
    "daily_overstay": _all_of(
        C_ENCOUNTER_ID,
        C_FACILITY_NAME,
        C_SERVICE_NAME,
        C_QUANTITY,
        C_AMOUNT,
        E_ENCOUNTER_ID,
    ),
    "included_service_duplicate": _all_of(
        C_CHARGE_ID,
        C_ENCOUNTER_ID,
        C_FACILITY_NAME,
        C_SERVICE_NAME,
        C_QUANTITY,
        C_AMOUNT,
    ),
    "limited_drug_duration": _all_of(
        C_ENCOUNTER_ID,
        C_FACILITY_NAME,
        C_SERVICE_NAME,
        C_QUANTITY,
        C_AMOUNT,
    ),
}

_RECORD_FIELD_REQUIREMENTS: dict[str, dict[str, PropertyRequirement]] = {
    "charge_threshold": {
        "charge_line_id": _all_of(C_CHARGE_ID),
        "encounter_id": _all_of(C_ENCOUNTER_ID),
        "patient_id": _all_of(C_PATIENT_ID),
        "facility_name": _all_of(C_FACILITY_NAME),
        "service_code": _all_of(C_SERVICE_CODE),
        "service_name": _all_of(C_SERVICE_NAME),
        "quantity": _all_of(C_QUANTITY),
        "unit_price": _all_of(C_UNIT_PRICE),
        "charged_amount": _all_of(C_AMOUNT),
        "eligible_amount": _all_of(C_ELIGIBLE_AMOUNT),
        "occurred_at": _all_of(C_OCCURRED_AT),
    },
    "daily_overstay": {
        "encounter_id": _all_of(C_ENCOUNTER_ID, E_ENCOUNTER_ID),
        "patient_id": _DAILY_PATIENT_REQUIREMENT,
        "facility_name": _all_of(C_FACILITY_NAME),
        "service_name": _all_of(C_SERVICE_NAME),
        "diagnosis_name": _all_of(E_DIAGNOSIS_NAME),
        "stay_days": _STAY_REQUIREMENT,
        "billed_quantity": _all_of(C_QUANTITY),
        "excess_quantity": _one_of(
            (C_QUANTITY, E_STAY_DAYS),
            (C_QUANTITY, E_STARTED_AT, E_ENDED_AT),
        ),
        "unit_price": _all_of(C_UNIT_PRICE),
        "charged_amount": _all_of(C_AMOUNT),
        "violation_amount": _one_of(
            (C_AMOUNT, C_QUANTITY, E_STAY_DAYS),
            (C_AMOUNT, C_QUANTITY, E_STARTED_AT, E_ENDED_AT),
        ),
    },
    "included_service_duplicate": {
        "charge_line_id": _all_of(C_CHARGE_ID),
        "encounter_id": _all_of(C_ENCOUNTER_ID),
        "patient_id": _all_of(C_PATIENT_ID),
        "facility_name": _all_of(C_FACILITY_NAME),
        "included_service": _all_of(C_SERVICE_NAME),
        "duplicate_service_code": _all_of(C_SERVICE_CODE),
        "duplicate_service": _all_of(C_SERVICE_NAME),
        "quantity": _all_of(C_QUANTITY),
        "unit_price": _all_of(C_UNIT_PRICE),
        "charged_amount": _all_of(C_AMOUNT),
        "occurred_at": _all_of(C_OCCURRED_AT),
    },
    "limited_drug_duration": {
        "encounter_id": _all_of(C_ENCOUNTER_ID),
        "patient_id": _all_of(C_PATIENT_ID),
        "facility_name": _all_of(C_FACILITY_NAME),
        "drug_name": _all_of(C_SERVICE_NAME),
        "diagnosis_name": _all_of(E_ENCOUNTER_ID, E_DIAGNOSIS_NAME),
        "observed_days": _DURATION_REQUIREMENT,
        "declared_cycle_days": _all_of(C_CYCLE_DAYS),
        "excess_days": _DURATION_REQUIREMENT,
        "quantity": _all_of(C_QUANTITY),
        "charged_amount": _all_of(C_AMOUNT),
        "eligible_amount": _all_of(C_ELIGIBLE_AMOUNT),
        "first_charge_at": _all_of(C_OCCURRED_AT),
        "last_charge_at": _all_of(C_OCCURRED_AT),
    },
}

_SUMMARY_FIELD_REQUIREMENTS: dict[str, dict[str, PropertyRequirement]] = {
    "charge_threshold": {
        "violation_count": _STRATEGY_EXECUTION_REQUIREMENTS["charge_threshold"],
        "affected_encounter_count": _all_of(C_ENCOUNTER_ID),
        "affected_patient_count": _all_of(C_PATIENT_ID),
        "violation_amount": _all_of(C_AMOUNT),
        "violating_quantity": _all_of(C_QUANTITY),
    },
    "daily_overstay": {
        "violation_count": _one_of(
            (*next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["daily_overstay"])), E_STAY_DAYS),
            (*next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["daily_overstay"])), E_STARTED_AT, E_ENDED_AT),
        ),
        "affected_encounter_count": _all_of(C_ENCOUNTER_ID, E_ENCOUNTER_ID),
        "affected_patient_count": _DAILY_PATIENT_REQUIREMENT,
        "violation_amount": _one_of(
            (C_AMOUNT, C_QUANTITY, E_STAY_DAYS),
            (C_AMOUNT, C_QUANTITY, E_STARTED_AT, E_ENDED_AT),
        ),
        "excess_quantity": _one_of(
            (C_QUANTITY, E_STAY_DAYS),
            (C_QUANTITY, E_STARTED_AT, E_ENDED_AT),
        ),
        "audited_scope_count": _all_of(C_ENCOUNTER_ID, E_ENCOUNTER_ID, C_SERVICE_NAME),
    },
    "included_service_duplicate": {
        "violation_count": _STRATEGY_EXECUTION_REQUIREMENTS["included_service_duplicate"],
        "affected_encounter_count": _all_of(C_ENCOUNTER_ID),
        "affected_patient_count": _all_of(C_PATIENT_ID),
        "violation_amount": _all_of(C_AMOUNT),
        "audited_scope_count": _all_of(C_ENCOUNTER_ID, C_SERVICE_NAME),
    },
    "limited_drug_duration": {
        "violation_count": _one_of(
            (*next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["limited_drug_duration"])), C_OCCURRED_AT),
            (*next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["limited_drug_duration"])), C_CYCLE_DAYS),
        ),
        "affected_encounter_count": _all_of(C_ENCOUNTER_ID),
        "affected_patient_count": _all_of(C_PATIENT_ID),
        "violation_amount": _all_of(C_AMOUNT),
        "audited_scope_count": _all_of(C_ENCOUNTER_ID, C_SERVICE_NAME),
        "max_observed_days": _DURATION_REQUIREMENT,
    },
}

_ALL_MEDICAL_PROPERTIES = frozenset(
    property_ref
    for strategy_fields in (
        *_RECORD_FIELD_REQUIREMENTS.values(),
        *_SUMMARY_FIELD_REQUIREMENTS.values(),
    )
    for requirement in strategy_fields.values()
    for alternative in requirement
    for property_ref in alternative
)
_SENSITIVE_PROPERTIES = frozenset({C_PATIENT_ID, E_PATIENT_ID, E_DIAGNOSIS_NAME})


@dataclass(frozen=True)
class MedicalAuditAccessPolicy:
    """Current ontology-property read grants for one Agent turn."""

    allowed_properties: frozenset[str]

    def allows(self, requirement: PropertyRequirement) -> bool:
        return any(
            alternative.issubset(self.allowed_properties)
            for alternative in requirement
        )

    def select(self, requirement: PropertyRequirement) -> frozenset[str] | None:
        return next(
            (
                alternative
                for alternative in requirement
                if alternative.issubset(self.allowed_properties)
            ),
            None,
        )

    def require(self, requirement: PropertyRequirement, strategy: str) -> frozenset[str]:
        selected = self.select(requirement)
        if selected is None:
            raise MedicalAuditError(
                "INVALID_QUERY",
                f"当前用户无权读取 {strategy} 所需的本体属性，不能执行该审计策略。",
                retryable=False,
            )
        return selected


def access_policy(allowed_properties: Sequence[str]) -> MedicalAuditAccessPolicy:
    """Build a closed policy; unknown or missing ontology properties never grant access."""

    return MedicalAuditAccessPolicy(
        frozenset(str(value) for value in allowed_properties) & _ALL_MEDICAL_PROPERTIES
    )

_SOURCE_PROPERTY_REFS: dict[str, dict[str, str]] = {
    "encounter": {
        "encounter_id": E_ENCOUNTER_ID,
        "facility_name": E_FACILITY_NAME,
        "patient_id": E_PATIENT_ID,
        "started_at": E_STARTED_AT,
        "ended_at": E_ENDED_AT,
        "stay_days": E_STAY_DAYS,
        "diagnosis_name": E_DIAGNOSIS_NAME,
    },
    "charge": {
        "charge_id": C_CHARGE_ID,
        "encounter_id": C_ENCOUNTER_ID,
        "facility_name": C_FACILITY_NAME,
        "patient_id": C_PATIENT_ID,
        "service_code": C_SERVICE_CODE,
        "service_name": C_SERVICE_NAME,
        "quantity": C_QUANTITY,
        "unit_price": C_UNIT_PRICE,
        "amount": C_AMOUNT,
        "eligible_amount": C_ELIGIBLE_AMOUNT,
        "occurred_at": C_OCCURRED_AT,
        "cycle_days": C_CYCLE_DAYS,
    },
}

_EVIDENCE_TEXT: dict[str, dict[str, str]] = {
    "charge_threshold": {
        "rule": "单条收费数量 > threshold",
    },
    "daily_overstay": {
        "rule": "同一就诊同一日计价项目累计数量 > 住院天数",
        "amount_basis": "超出数量 × 该就诊项目平均实收单价",
    },
    "included_service_duplicate": {
        "rule": "同一就诊已收 included_service 时，duplicate_service 不得另行收费",
    },
    "limited_drug_duration": {
        "rule": "同一就诊的药品实际用药天数 > max_days",
        "duration_basis": "优先按费用发生日期去重；源库无该字段时使用周期天数",
    },
}

_COMMON_ARGUMENTS = frozenset({"strategy", "facility_name", "limit", "offset"})
_STRATEGY_ARGUMENTS: dict[str, frozenset[str]] = {
    "charge_threshold": frozenset({"service_name", "threshold"}),
    "daily_overstay": frozenset({"service_names"}),
    "included_service_duplicate": frozenset({"included_service", "duplicate_service"}),
    "limited_drug_duration": frozenset({"drug_name", "max_days"}),
}


class MedicalAuditError(ValueError):
    """A stable, model-correctable error safe to expose through Agent tools."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True)
class _SourceSchema:
    source: DataSource
    path: Path | None
    tables: dict[str, str]
    columns: dict[str, dict[str, str]]

    def table(self, logical_name: str, *, required: bool = True) -> str | None:
        table = self.tables.get(logical_name)
        if required and not table:
            raise MedicalAuditError(
                "RESOURCE_NOT_FOUND",
                f"当前运行定义缺少 {logical_name} 对象的明确数据映射，无法执行该审计策略。",
                retryable=False,
            )
        return table

    def column(
        self,
        table_name: str,
        logical_name: str,
        *,
        required: bool = True,
    ) -> str | None:
        column = self.columns.get(table_name, {}).get(logical_name)
        if required and not column:
            raise MedicalAuditError(
                "INVALID_QUERY",
                f"医保数据源缺少“{logical_name}”对应字段，无法按当前口径审计。",
                retryable=False,
            )
        return column


@dataclass(frozen=True)
class MedicalAuditMappingContract:
    """An immutable-by-digest projection of the active runtime mappings.

    ``source`` is retained only as the already-authorized connector object. All
    query identifiers and persisted provenance come from the primitive fields
    covered by ``fingerprint``; mutating a nested dictionary therefore makes
    validation fail instead of silently changing the query target.
    """

    source: DataSource
    source_id: str
    source_name: str
    source_type: str
    connector_revision: int
    connector_config_hash: str
    path: Path | None
    tables: dict[str, str]
    columns: dict[str, dict[str, str]]
    mapping_ids: dict[str, str]
    mapping_provenance: dict[str, dict[str, Any]]
    definition_provenance: dict[str, Any]
    fingerprint: str

    def lineage(self) -> dict[str, Any]:
        return {
            "contract_version": MAPPING_CONTRACT_VERSION,
            "fingerprint": self.fingerprint,
            "mapping_ids": dict(sorted(self.mapping_ids.items())),
            "definition": dict(self.definition_provenance),
        }


class _AuditResult:
    """Small result facade shared by sqlite3 and SQLAlchemy connections."""

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def fetchone(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[Mapping[str, Any]]:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


_MYSQL_IDENTIFIER = re.compile(r'"((?:[^"\r\n]|"")*)"')


class _AuditConnection:
    """Read-only query facade over relational and immutable dataset sources."""

    def __init__(self, source: DataSource) -> None:
        self.dialect = str(source.type or "").lower()
        self._sqlite: sqlite3.Connection | None = None
        self._sqlalchemy = None
        self._dataset: dataset_query_service.DatasetConnection | None = None
        if self.dialect == "sqlite":
            raw_path = str((source.config or {}).get("path") or "").strip()
            path = Path(raw_path).expanduser().resolve()
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            self._sqlite = connection
        elif self.dialect == "mysql":
            self._sqlalchemy = datasource_service.get_engine(source).connect()
        elif self.dialect == "dataset":
            self._dataset = dataset_query_service.open_connection(source)
        else:
            raise MedicalAuditError(
                "INVALID_QUERY",
                "医保审计仅支持受管数据集或兼容的关系型数据源。",
                retryable=False,
            )

    @staticmethod
    def _mysql_sql(sql: str) -> str:
        def quote(match: re.Match[str]) -> str:
            value = match.group(1).replace('""', '"').replace("`", "``")
            return f"`{value}`"

        translated = _MYSQL_IDENTIFIER.sub(quote, sql)
        translated = re.sub(r"\bAS\s+MATERIALIZED\s*\(", "AS (", translated, flags=re.I)
        translated = re.sub(r"\s+AS\s+TEXT\)", " AS CHAR)", translated, flags=re.I)
        translated = re.sub(
            r"\s+AS\s+REAL\)",
            " AS DECIMAL(30, 8))",
            translated,
            flags=re.I,
        )
        return translated.replace("?", "%s")

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
    ) -> _AuditResult:
        values = tuple(params or ())
        if self._sqlite is not None:
            cursor = self._sqlite.execute(sql, values)
            return _AuditResult(cursor.fetchall())
        dataset = getattr(self, "_dataset", None)
        if dataset is not None:
            columns, rows = dataset.execute(sql, values)
            return _AuditResult(
                [dict(zip(columns, row, strict=True)) for row in rows]
            )
        if self._sqlalchemy is None:
            raise RuntimeError("医保审计连接已关闭")
        result = self._sqlalchemy.exec_driver_sql(self._mysql_sql(sql), values)
        return _AuditResult(result.mappings().all())

    def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
            self._sqlite = None
        if self._sqlalchemy is not None:
            self._sqlalchemy.rollback()
            self._sqlalchemy.close()
            self._sqlalchemy = None
        dataset = getattr(self, "_dataset", None)
        if dataset is not None:
            dataset.close()
            self._dataset = None


def resolve_mapping_contract(
    data_sources: Sequence[DataSource],
    mappings: Sequence[Any],
    *,
    definition: Any,
) -> MedicalAuditMappingContract:
    """Resolve the specialized audit contract from one active runtime definition.

    The resolver never guesses physical identifiers. It accepts only the table
    and column bindings declared by the current ``DataMapping`` resources and
    rejects duplicate object types, duplicate mappings, cross-source bindings,
    missing connectors, and malformed definition provenance.
    """

    definition_provenance = _definition_provenance(definition)
    definition_entities = _field(definition, "entities", {})
    if not isinstance(definition_entities, Mapping):
        raise _mapping_contract_error("当前运行定义缺少可验证的对象类型集合。")

    entities_by_api: dict[str, list[Any]] = {
        api_name: [] for api_name in {"medical_charge_line", "medical_encounter"}
    }
    entities_by_id: dict[str, Any] = {}
    for raw_entity_id, entity in definition_entities.items():
        entity_id = str(_field(entity, "id", raw_entity_id) or "").strip()
        if not entity_id:
            raise _mapping_contract_error("当前运行定义包含无稳定 ID 的对象类型。")
        if entity_id in entities_by_id:
            raise _mapping_contract_error("当前运行定义包含重复的对象类型 ID。")
        entities_by_id[entity_id] = entity
        api_name = str(_field(entity, "api_name", "") or "").strip()
        if api_name in entities_by_api:
            entities_by_api[api_name].append(entity)
    for api_name, candidates in entities_by_api.items():
        if len(candidates) > 1:
            raise _mapping_contract_error(
                f"当前运行定义中的对象类型 {api_name} 不唯一，不能确定医保审计映射。"
            )

    mappings_by_entity: dict[str, list[Any]] = {}
    for mapping in mappings:
        entity_id = str(_field(mapping, "entity_id", "") or "").strip()
        if entity_id in entities_by_id:
            mappings_by_entity.setdefault(entity_id, []).append(mapping)

    source_by_id: dict[str, DataSource] = {}
    for source in data_sources:
        source_id = str(getattr(source, "id", "") or "").strip()
        if not source_id or source_id in source_by_id:
            raise _mapping_contract_error("当前 Agent 的数据源绑定缺少稳定 ID 或存在重复 ID。")
        source_by_id[source_id] = source

    tables: dict[str, str] = {}
    columns: dict[str, dict[str, str]] = {}
    mapping_ids: dict[str, str] = {}
    mapping_provenance: dict[str, dict[str, Any]] = {}
    selected_source: DataSource | None = None
    for logical_name, entity_api_name in (
        ("charge", "medical_charge_line"),
        ("encounter", "medical_encounter"),
    ):
        entity_candidates = entities_by_api[entity_api_name]
        if not entity_candidates:
            continue
        entity = entity_candidates[0]
        entity_id = str(_field(entity, "id", "") or "").strip()
        candidates = mappings_by_entity.get(entity_id, [])
        if len(candidates) > 1:
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 存在多个当前运行映射，不能确定唯一物理绑定。"
            )
        if not candidates:
            continue
        mapping = candidates[0]
        mapping_id = str(_field(mapping, "id", "") or "").strip()
        source_id = str(_field(mapping, "data_source_id", "") or "").strip()
        table_name = _mapped_identifier(_field(mapping, "table_name", None), "映射表名")
        if not mapping_id or not source_id:
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 的运行映射缺少稳定映射 ID 或数据源 ID。"
            )
        mapping_status = _field(mapping, "status", None)
        if mapping_status is not None and (
            str(mapping_status) not in {"ready", "ok"}
            or bool(str(_field(mapping, "last_error", "") or "").strip())
        ):
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 的当前运行映射未就绪。"
            )
        source = source_by_id.get(source_id)
        if source is None:
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 的运行映射未绑定到当前 Agent 可用数据源。"
            )
        source_type = str(getattr(source, "type", "") or "").lower()
        if source_type not in {"sqlite", "mysql", "dataset"}:
            raise _mapping_contract_error("医保审计运行映射必须绑定受管数据集或兼容关系型数据源。")
        if selected_source is not None and str(selected_source.id) != source_id:
            raise _mapping_contract_error("医保审计对象映射跨越多个物理数据源，拒绝执行关联审计。")
        selected_source = source

        raw_column_map = _field(mapping, "column_map", {})
        if not isinstance(raw_column_map, Mapping):
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 的字段映射不是有效对象。"
            )
        properties = list(_field(entity, "properties", []) or [])
        properties_by_api: dict[str, list[Any]] = {}
        for prop in properties:
            property_api_name = str(_field(prop, "api_name", "") or "").strip()
            if property_api_name:
                properties_by_api.setdefault(property_api_name, []).append(prop)

        resolved: dict[str, str] = {}
        for logical_column, property_ref in _SOURCE_PROPERTY_REFS[logical_name].items():
            property_api_name = property_ref.split(".", 1)[1]
            property_candidates = properties_by_api.get(property_api_name, [])
            if len(property_candidates) > 1:
                raise _mapping_contract_error(
                    f"对象类型 {entity_api_name} 的属性 {property_api_name} 不唯一。"
                )
            if not property_candidates:
                continue
            property_name = str(
                _field(property_candidates[0], "name", "") or ""
            ).strip()
            if not property_name or property_name not in raw_column_map:
                continue
            resolved[logical_column] = _mapped_identifier(
                raw_column_map[property_name],
                f"{entity_api_name}.{property_api_name} 的映射列名",
            )

        binding_ref = _plain_json_object(
            _field(mapping, "data_source_binding_ref", {}),
            f"对象类型 {entity_api_name} 的逻辑数据源绑定",
        )
        binding_adapter = str(binding_ref.get("adapter") or "").lower()
        if binding_adapter and binding_adapter != source_type:
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 的逻辑绑定与运行数据源方言不一致。"
            )
        transform_rules = _plain_json_object(
            _field(mapping, "transform_rules", {}),
            f"对象类型 {entity_api_name} 的转换规则",
        )
        _validate_specialized_transforms(
            logical_name,
            transform_rules,
            properties,
            raw_column_map,
        )
        binding_key = _field(mapping, "data_source_binding_key", "")
        definition_source_id = _field(
            mapping, "definition_data_source_id", source_id
        )
        if not isinstance(binding_key, str) or not isinstance(
            definition_source_id, str
        ) or not definition_source_id.strip():
            raise _mapping_contract_error(
                f"对象类型 {entity_api_name} 的逻辑数据源绑定格式无效。"
            )
        tables[logical_name] = table_name
        columns[logical_name] = resolved
        mapping_ids[logical_name] = mapping_id
        mapping_provenance[logical_name] = {
            "mapping_id": mapping_id,
            "entity_id": entity_id,
            "entity_api_name": entity_api_name,
            "runtime_data_source_id": source_id,
            "definition_data_source_id": definition_source_id,
            "data_source_binding_key": binding_key,
            "data_source_binding_ref": binding_ref,
            "table": table_name,
            "columns": dict(sorted(resolved.items())),
            "transform_rules": transform_rules,
        }

    if "charge" not in tables or selected_source is None:
        raise _mapping_contract_error(
            "当前运行定义缺少唯一的 medical_charge_line 对象映射，不能执行医保审计。"
        )
    source_id = str(getattr(selected_source, "id", "") or "").strip()
    source_name = str(getattr(selected_source, "name", "") or "")
    source_type = str(getattr(selected_source, "type", "") or "").lower()
    connector_revision = int(getattr(selected_source, "connector_revision", 0) or 0)
    source_config = _plain_json_object(
        getattr(selected_source, "config", None) or {}, "医保审计数据源配置"
    )
    connector_config_hash = _payload_sha256(source_config)
    path: Path | None = None
    if source_type == "sqlite":
        raw_path = str(source_config.get("path") or "").strip()
        if not raw_path:
            raise _mapping_contract_error("医保审计映射绑定的数据源缺少 SQLite 路径。")
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise MedicalAuditError(
                "RESOURCE_NOT_FOUND",
                "医保审计映射绑定的 SQLite 数据源不存在或不可读取。",
                retryable=False,
            )
    elif source_type == "dataset":
        if not str(source_config.get("dataset_version_id") or "").strip():
            raise _mapping_contract_error("医保审计映射绑定的数据集缺少固定版本标识。")
    elif not all(
        str(source_config.get(key) or "").strip()
        for key in ("host", "database", "user")
    ):
        raise _mapping_contract_error("医保审计映射绑定的 MySQL 数据源配置不完整。")
    payload = _mapping_contract_payload(
        source_id=source_id,
        source_name=source_name,
        source_type=source_type,
        connector_revision=connector_revision,
        connector_config_hash=connector_config_hash,
        path=path,
        tables=tables,
        columns=columns,
        mapping_provenance=mapping_provenance,
        definition_provenance=definition_provenance,
    )
    fingerprint = _payload_sha256(payload)
    return MedicalAuditMappingContract(
        source=selected_source,
        source_id=source_id,
        source_name=source_name,
        source_type=source_type,
        connector_revision=connector_revision,
        connector_config_hash=connector_config_hash,
        path=path,
        tables=tables,
        columns=columns,
        mapping_ids=mapping_ids,
        mapping_provenance=mapping_provenance,
        definition_provenance=definition_provenance,
        fingerprint=fingerprint,
    )


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mapping_contract_error(message: str) -> MedicalAuditError:
    return MedicalAuditError("INVALID_QUERY", message, retryable=False)


def _mapped_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise _mapping_contract_error(f"{label}不是字符串，拒绝执行医保审计。")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > 300
        or normalized == "*"
        or any(ord(character) < 32 for character in normalized)
    ):
        raise _mapping_contract_error(f"{label}为空或格式无效，拒绝执行医保审计。")
    return normalized


def _plain_json_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _mapping_contract_error(f"{label}不是有效对象。")
    try:
        encoded = json.dumps(
            dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise _mapping_contract_error(f"{label}不是稳定的 JSON 定义。") from exc
    if not isinstance(decoded, dict):
        raise _mapping_contract_error(f"{label}不是有效对象。")
    return decoded


def _validate_specialized_transforms(
    logical_name: str,
    transform_rules: Mapping[str, Any],
    properties: Sequence[Any],
    column_map: Mapping[str, Any],
) -> None:
    allowed: dict[str, Any] = {}
    if logical_name == "charge":
        charge_id_properties = [
            prop
            for prop in properties
            if str(_field(prop, "api_name", "") or "") == "charge_line_id"
        ]
        if len(charge_id_properties) == 1:
            property_name = str(
                _field(charge_id_properties[0], "name", "") or ""
            ).strip()
            if property_name in column_map:
                # Every strategy that emits the charge-line identity performs
                # this same conversion in SQL via CAST(... AS TEXT).
                allowed[property_name] = [{"op": "to_string"}]
    for property_name, operations in transform_rules.items():
        if operations in (None, [], {}):
            continue
        if not isinstance(property_name, str) or allowed.get(property_name) != operations:
            raise _mapping_contract_error(
                "医保审计专用 SQL 不能证明当前对象映射的转换规则，拒绝执行。"
            )


def _definition_provenance(definition: Any) -> dict[str, Any]:
    if definition is None:
        raise _mapping_contract_error("医保审计必须绑定一个明确的运行定义。")
    provenance = {
        "source": _field(definition, "source"),
        "environment": _field(definition, "environment"),
        "definition_hash": _field(definition, "definition_hash"),
        "snapshot_id": _field(definition, "snapshot_id"),
        "release_id": _field(definition, "release_id"),
    }
    source = provenance["source"]
    environment = provenance["environment"]
    definition_hash = provenance["definition_hash"]
    if (
        source not in {"live", "release"}
        or not isinstance(environment, str)
        or not environment.strip()
        or not isinstance(definition_hash, str)
        or len(definition_hash) != 64
        or any(character not in "0123456789abcdef" for character in definition_hash)
    ):
        raise _mapping_contract_error("医保审计运行定义缺少有效的版本来源或定义哈希。")
    for key in ("snapshot_id", "release_id"):
        value = provenance[key]
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise _mapping_contract_error(f"医保审计运行定义的 {key} 格式无效。")
    if source == "live" and (
        provenance["snapshot_id"] is not None
        or provenance["release_id"] is not None
    ):
        raise _mapping_contract_error("live 医保审计定义不能携带发布快照标识。")
    if source == "release" and provenance["snapshot_id"] is None:
        raise _mapping_contract_error("release 医保审计定义缺少快照标识。")
    return provenance


def _mapping_contract_payload(
    *,
    source_id: str,
    source_name: str = "",
    source_type: str = "sqlite",
    connector_revision: int,
    connector_config_hash: str = "",
    path: Path | None = None,
    tables: Mapping[str, str],
    columns: Mapping[str, Mapping[str, str]],
    mapping_provenance: Mapping[str, Mapping[str, Any]],
    definition_provenance: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "contract_version": MAPPING_CONTRACT_VERSION,
        "source_id": source_id,
        "source_name": source_name,
        "source_type": source_type,
        "connector_revision": connector_revision,
        "connector_config_hash": connector_config_hash,
        "path": str(path) if path is not None else "",
        "tables": dict(sorted(tables.items())),
        "columns": {
            key: dict(sorted(value.items()))
            for key, value in sorted(columns.items())
        },
        "mappings": {
            key: dict(value)
            for key, value in sorted(mapping_provenance.items())
        },
        "definition": dict(definition_provenance),
    }


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _mapping_contract_error("医保审计运行映射不能稳定序列化。") from exc
    return hashlib.sha256(encoded).hexdigest()


def _validate_mapping_contract(contract: MedicalAuditMappingContract) -> None:
    if not isinstance(contract, MedicalAuditMappingContract):
        raise _mapping_contract_error("医保审计缺少显式运行映射契约。")
    if set(contract.tables) != set(contract.columns) or set(contract.tables) != set(
        contract.mapping_ids
    ) or set(contract.tables) != set(contract.mapping_provenance):
        raise _mapping_contract_error("医保审计运行映射契约不完整。")
    if "charge" not in contract.tables or not set(contract.tables).issubset(
        {"charge", "encounter"}
    ):
        raise _mapping_contract_error("医保审计运行映射契约缺少收费明细映射。")
    if (
        str(getattr(contract.source, "id", "") or "") != contract.source_id
        or str(getattr(contract.source, "name", "") or "") != contract.source_name
        or str(getattr(contract.source, "type", "") or "").lower()
        != contract.source_type
        or contract.source_type not in {"sqlite", "mysql", "dataset"}
        or int(getattr(contract.source, "connector_revision", 0) or 0)
        != contract.connector_revision
    ):
        raise _mapping_contract_error("医保审计数据源已偏离已解析的运行映射契约。")
    current_config = _plain_json_object(
        getattr(contract.source, "config", None) or {}, "医保审计数据源配置"
    )
    if _payload_sha256(current_config) != contract.connector_config_hash:
        raise _mapping_contract_error("医保审计数据源配置已偏离已解析的运行映射契约。")
    if contract.source_type == "sqlite":
        current_path = Path(str(current_config.get("path") or "")).expanduser().resolve()
        if current_path != contract.path or not current_path.is_file():
            raise MedicalAuditError(
                "RESOURCE_NOT_FOUND",
                "医保审计数据源路径已变化或不可读取。",
                retryable=False,
            )
    elif contract.path is not None:
        raise _mapping_contract_error("远端医保审计契约不能携带本地数据库路径。")
    for logical_name, table_name in contract.tables.items():
        _mapped_identifier(table_name, f"{logical_name} 映射表名")
        for column in contract.columns[logical_name].values():
            _mapped_identifier(column, f"{logical_name} 映射列名")
        provenance = contract.mapping_provenance[logical_name]
        if (
            not isinstance(provenance, Mapping)
            or provenance.get("mapping_id") != contract.mapping_ids[logical_name]
            or provenance.get("table") != table_name
            or provenance.get("columns") != dict(sorted(contract.columns[logical_name].items()))
        ):
            raise _mapping_contract_error("医保审计映射来源与物理绑定不一致。")
    normalized_definition = _definition_provenance(contract.definition_provenance)
    if normalized_definition != contract.definition_provenance:
        raise _mapping_contract_error("医保审计定义来源不是规范形式。")
    payload = _mapping_contract_payload(
        source_id=contract.source_id,
        source_name=contract.source_name,
        source_type=contract.source_type,
        connector_revision=contract.connector_revision,
        connector_config_hash=contract.connector_config_hash,
        path=contract.path,
        tables=contract.tables,
        columns=contract.columns,
        mapping_provenance=contract.mapping_provenance,
        definition_provenance=contract.definition_provenance,
    )
    if contract.fingerprint != _payload_sha256(payload):
        raise _mapping_contract_error("医保审计运行映射契约校验失败。")


def _contract_column_for_property(
    contract: MedicalAuditMappingContract,
    property_ref: str,
) -> str | None:
    for logical_name, properties in _SOURCE_PROPERTY_REFS.items():
        for logical_column, expected_ref in properties.items():
            if expected_ref == property_ref:
                return contract.columns.get(logical_name, {}).get(logical_column)
    return None


def tool_schema() -> dict[str, Any]:
    """Return the closed JSON schema exposed only by medical-audit Agents."""

    return {
        "strategy": {
            "type": "string",
            "enum": sorted(STRATEGIES),
            "description": "受控审计策略",
            "required": True,
        },
        "facility_name": {
            "type": "string",
            "description": "可选；医疗机构完整名称，按等值匹配",
        },
        "service_name": {
            "type": "string",
            "description": "charge_threshold 的服务项目完整名称",
        },
        "threshold": {
            "type": "number",
            "minimum": 0,
            "description": "charge_threshold 的单条收费数量阈值，按大于判断",
        },
        "service_names": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 20,
            "description": "daily_overstay 纳入日计价审计的服务项目完整名称",
        },
        "included_service": {
            "type": "string",
            "description": "included_service_duplicate 的已包含服务项目完整名称",
        },
        "duplicate_service": {
            "type": "string",
            "description": "included_service_duplicate 中不应另行收费的项目完整名称",
        },
        "drug_name": {
            "type": "string",
            "description": "limited_drug_duration 的药品/项目完整名称",
        },
        "max_days": {
            "type": "integer",
            "minimum": 1,
            "description": "limited_drug_duration 允许的最大用药天数",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_LIMIT,
            "description": f"本页最多返回明细数，默认 {DEFAULT_LIMIT}",
        },
        "offset": {
            "type": "integer",
            "minimum": 0,
            "description": "从 0 开始的分页偏移；后续页使用 next_offset",
        },
    }


def run_medical_audit(
    mapping_contract: MedicalAuditMappingContract,
    args: Mapping[str, Any],
    *,
    include_sensitive: bool = False,
    property_access: MedicalAuditAccessPolicy | None = None,
) -> dict[str, Any]:
    """Execute one controlled strategy through an explicit runtime mapping."""

    canonical_args = _canonical_arguments(args)
    strategy = str(canonical_args["strategy"])
    limit = int(canonical_args["limit"])
    offset = int(canonical_args["offset"])
    facility_name = canonical_args.get("facility_name")
    policy = property_access or access_policy(
        _ALL_MEDICAL_PROPERTIES
        - (frozenset() if include_sensitive else _SENSITIVE_PROPERTIES)
    )
    used_properties = set(
        policy.require(_STRATEGY_EXECUTION_REQUIREMENTS[strategy], strategy)
    )
    schema = _source_schema(mapping_contract)

    runners = {
        "charge_threshold": _charge_threshold,
        "daily_overstay": _daily_overstay,
        "included_service_duplicate": _included_service_duplicate,
        "limited_drug_duration": _limited_drug_duration,
    }
    try:
        with closing(_connect_read_only(schema.source)) as connection:
            result = runners[strategy](
                connection,
                schema,
                canonical_args,
                facility_name=facility_name,
                limit=limit,
                offset=offset,
                property_access=policy,
            )
    except MedicalAuditError:
        raise
    except (sqlite3.Error, SQLAlchemyError, dataset_query_service.DatasetQueryError) as exc:
        raise MedicalAuditError(
            "TOOL_EXECUTION_FAILED",
            "医保审计查询执行失败，请检查数据源结构和字段类型。",
            retryable=False,
        ) from exc

    used_properties.update(result.pop("_used_properties", ()))
    available_record_fields = set(
        result.pop("_available_record_fields", _RECORD_FIELD_REQUIREMENTS[strategy])
    )
    available_summary_fields = set(
        result.pop("_available_summary_fields", _SUMMARY_FIELD_REQUIREMENTS[strategy])
    )
    record_fields = [
        field
        for field in _visible_field_names(_RECORD_FIELD_REQUIREMENTS[strategy], policy)
        if field in available_record_fields
    ]
    summary_fields = [
        field
        for field in _visible_field_names(_SUMMARY_FIELD_REQUIREMENTS[strategy], policy)
        if field in available_summary_fields
    ]
    records = [
        {
            field: record[field]
            for field in record_fields
            if field in record
        }
        for record in result["records"]
    ]
    summary = {
        field: result["summary"][field]
        for field in summary_fields
        if field in result["summary"]
    }
    if "violation_count" not in summary:
        raise MedicalAuditError(
            "INVALID_QUERY",
            "当前属性权限不足以返回审计命中数量。",
            retryable=False,
        )
    resolved_columns = {
        property_ref: column
        for property_ref, column in dict(
            result["evidence"].get("resolved_columns") or {}
        ).items()
        if property_ref in _ALL_MEDICAL_PROPERTIES
        and policy.allows(_all_of(property_ref))
        and isinstance(column, str)
        and bool(column)
    }
    evidence = {
        "source_id": mapping_contract.source_id,
        "source_name": mapping_contract.source_name,
        "connector_revision": mapping_contract.connector_revision,
        "matching": "exact",
        "rule": str(result["evidence"]["rule"]),
        "parameters": dict(result["evidence"]["parameters"]),
        "tables": list(result["evidence"]["tables"]),
        "resolved_columns": resolved_columns,
    }
    for optional_key in ("amount_basis", "duration_basis"):
        if optional_key in result["evidence"]:
            evidence[optional_key] = str(result["evidence"][optional_key])

    for field in record_fields:
        selected = policy.select(_RECORD_FIELD_REQUIREMENTS[strategy][field])
        if selected:
            used_properties.update(selected)
    for field in summary:
        selected = policy.select(_SUMMARY_FIELD_REQUIREMENTS[strategy][field])
        if selected:
            used_properties.update(selected)
    used_properties.update(resolved_columns)

    violation_count = int(summary.get("violation_count") or 0)
    row_count = len(records)
    truncated = offset + row_count < violation_count
    return {
        "ok": True,
        "audit_version": AUDIT_VERSION,
        "strategy": strategy,
        "empty": violation_count == 0,
        "message": (
            "本次未发现符合当前条件的违规明细。"
            if violation_count == 0
            else f"本次发现 {violation_count} 条（组）符合当前条件的违规证据。"
        ),
        "summary": summary,
        "records": records,
        "row_count": row_count,
        "offset": offset,
        "limit": limit,
        "truncated": truncated,
        "next_offset": offset + row_count if truncated else None,
        "evidence": evidence,
        "lineage": {
            "schema_version": RESULT_SCHEMA_VERSION,
            "audit_version": AUDIT_VERSION,
            "source_id": mapping_contract.source_id,
            "connector_revision": mapping_contract.connector_revision,
            "mapping_contract": mapping_contract.lineage(),
            "request": canonical_args,
            "record_fields": sorted(record_fields),
            "summary_fields": sorted(summary),
            "resolved_column_properties": sorted(resolved_columns),
            "property_refs": sorted(used_properties),
        },
    }


def _canonical_arguments(args: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(args, Mapping):
        raise MedicalAuditError("INVALID_TOOL_ARGUMENTS", "审计参数必须是 JSON 对象。")
    strategy = _text(args.get("strategy"), "strategy")
    if strategy not in STRATEGIES:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            "未知医保审计策略；请从工具 schema 的 strategy 枚举中选择。",
        )
    allowed = _COMMON_ARGUMENTS | _STRATEGY_ARGUMENTS[strategy]
    unexpected = sorted(str(key) for key in args if key not in allowed)
    if unexpected:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"策略 {strategy} 不接受参数：{', '.join(unexpected)}。",
        )
    limit, offset = _pagination(args)
    canonical: dict[str, Any] = {
        "strategy": strategy,
        "limit": limit,
        "offset": offset,
    }
    facility_name = _optional_text(args.get("facility_name"), "facility_name")
    if facility_name is not None:
        canonical["facility_name"] = facility_name
    if strategy == "charge_threshold":
        canonical["service_name"] = _text(args.get("service_name"), "service_name")
        canonical["threshold"] = _number(args.get("threshold"), "threshold", minimum=0)
    elif strategy == "daily_overstay":
        canonical["service_names"] = _text_list(
            args.get("service_names"), "service_names", max_items=20
        )
    elif strategy == "included_service_duplicate":
        included = _text(args.get("included_service"), "included_service")
        duplicate = _text(args.get("duplicate_service"), "duplicate_service")
        if included == duplicate:
            raise MedicalAuditError(
                "INVALID_TOOL_ARGUMENTS",
                "已包含项目和重复收费项目必须是两个不同的服务项目。",
            )
        canonical["included_service"] = included
        canonical["duplicate_service"] = duplicate
    else:
        canonical["drug_name"] = _text(args.get("drug_name"), "drug_name")
        canonical["max_days"] = _integer(args.get("max_days"), "max_days", minimum=1)
    return canonical


def _visible_field_names(
    requirements: Mapping[str, PropertyRequirement],
    policy: MedicalAuditAccessPolicy,
) -> list[str]:
    return [
        field
        for field, requirement in requirements.items()
        if policy.allows(requirement)
    ]


def authorize_historic_result(
    mapping_contract: MedicalAuditMappingContract,
    args: Mapping[str, Any],
    result: Any,
    *,
    property_access: MedicalAuditAccessPolicy,
) -> bool:
    """Validate persisted medical output without executing its business query."""

    try:
        canonical_args = _canonical_arguments(args)
        _validate_mapping_contract(mapping_contract)
    except (MedicalAuditError, TypeError, ValueError):
        return False
    if not isinstance(result, dict) or set(result) != {
        "ok",
        "audit_version",
        "strategy",
        "empty",
        "message",
        "summary",
        "records",
        "row_count",
        "offset",
        "limit",
        "truncated",
        "next_offset",
        "evidence",
        "lineage",
    }:
        return False
    strategy = str(canonical_args["strategy"])
    lineage = result.get("lineage")
    if not isinstance(lineage, dict) or set(lineage) != {
        "schema_version",
        "audit_version",
        "source_id",
        "connector_revision",
        "mapping_contract",
        "request",
        "record_fields",
        "summary_fields",
        "resolved_column_properties",
        "property_refs",
    }:
        return False
    if (
        result.get("ok") is not True
        or result.get("audit_version") != AUDIT_VERSION
        or result.get("strategy") != strategy
        or lineage.get("schema_version") != RESULT_SCHEMA_VERSION
        or lineage.get("audit_version") != AUDIT_VERSION
        or lineage.get("request") != canonical_args
        or lineage.get("mapping_contract") != mapping_contract.lineage()
    ):
        return False

    source_id = mapping_contract.source_id
    connector_revision = mapping_contract.connector_revision
    if (
        not source_id
        or lineage.get("source_id") != source_id
        or lineage.get("connector_revision") != connector_revision
    ):
        return False

    record_fields = _strict_string_list(lineage.get("record_fields"))
    summary_fields = _strict_string_list(lineage.get("summary_fields"))
    resolved_properties = _strict_string_list(
        lineage.get("resolved_column_properties")
    )
    property_refs = _strict_string_list(lineage.get("property_refs"))
    if any(value is None for value in (
        record_fields,
        summary_fields,
        resolved_properties,
        property_refs,
    )):
        return False
    assert record_fields is not None
    assert summary_fields is not None
    assert resolved_properties is not None
    assert property_refs is not None
    if not set(property_refs).issubset(property_access.allowed_properties):
        return False
    if not _requirements_covered(
        _STRATEGY_EXECUTION_REQUIREMENTS[strategy], set(property_refs)
    ):
        return False
    if strategy == "daily_overstay" and not _requirements_covered(
        _STAY_REQUIREMENT, set(property_refs)
    ):
        return False
    if strategy == "limited_drug_duration" and not _requirements_covered(
        _DURATION_REQUIREMENT, set(property_refs)
    ):
        return False

    known_record_fields = _RECORD_FIELD_REQUIREMENTS[strategy]
    known_summary_fields = _SUMMARY_FIELD_REQUIREMENTS[strategy]
    if (
        not set(record_fields).issubset(known_record_fields)
        or not set(summary_fields).issubset(known_summary_fields)
        or "violation_count" not in summary_fields
        or not set(resolved_properties).issubset(_ALL_MEDICAL_PROPERTIES)
        or not set(resolved_properties).issubset(property_refs)
    ):
        return False
    for field in record_fields:
        if not _requirements_covered(
            known_record_fields[field], set(property_refs)
        ):
            return False
    for field in summary_fields:
        if not _requirements_covered(
            known_summary_fields[field], set(property_refs)
        ):
            return False

    records = result.get("records")
    summary = result.get("summary")
    if (
        not isinstance(records, list)
        or not isinstance(summary, dict)
        or set(summary) != set(summary_fields)
        or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in summary.values()
        )
        or any(
            not isinstance(record, dict)
            or set(record) != set(record_fields)
            or any(not _is_scalar(value) for value in record.values())
            for record in records
        )
    ):
        return False
    violation_count = summary.get("violation_count")
    if (
        isinstance(violation_count, bool)
        or not isinstance(violation_count, int)
        or violation_count < 0
    ):
        return False
    row_count = len(records)
    offset = int(canonical_args["offset"])
    limit = int(canonical_args["limit"])
    truncated = offset + row_count < violation_count
    if (
        result.get("row_count") != row_count
        or row_count > limit
        or (row_count > 0 and offset + row_count > violation_count)
        or result.get("offset") != offset
        or result.get("limit") != limit
        or result.get("empty") is not (violation_count == 0)
        or result.get("truncated") is not truncated
        or result.get("next_offset") != (offset + row_count if truncated else None)
        or result.get("message") != (
            "本次未发现符合当前条件的违规明细。"
            if violation_count == 0
            else f"本次发现 {violation_count} 条（组）符合当前条件的违规证据。"
        )
    ):
        return False

    evidence = result.get("evidence")
    optional_evidence = {
        "charge_threshold": set(),
        "daily_overstay": {"amount_basis"},
        "included_service_duplicate": set(),
        "limited_drug_duration": {"duration_basis"},
    }[strategy]
    expected_evidence_keys = {
        "source_id",
        "source_name",
        "connector_revision",
        "matching",
        "rule",
        "parameters",
        "tables",
        "resolved_columns",
        *optional_evidence,
    }
    if not isinstance(evidence, dict) or set(evidence) != expected_evidence_keys:
        return False
    resolved_columns = evidence.get("resolved_columns")
    if (
        evidence.get("source_id") != source_id
        or evidence.get("source_name") != mapping_contract.source_name
        or evidence.get("connector_revision") != connector_revision
        or evidence.get("matching") != "exact"
        or evidence.get("parameters") != _expected_evidence_parameters(canonical_args)
        or evidence.get("rule") != _EVIDENCE_TEXT[strategy]["rule"]
        or not _valid_evidence_tables(
            strategy, evidence.get("tables"), mapping_contract
        )
        or not isinstance(resolved_columns, dict)
        or set(resolved_columns) != set(resolved_properties)
        or any(
            not isinstance(value, str)
            or value != _contract_column_for_property(mapping_contract, property_ref)
            for property_ref, value in resolved_columns.items()
        )
    ):
        return False
    return all(
        evidence.get(key) == _EVIDENCE_TEXT[strategy][key]
        for key in optional_evidence
    )


def _strict_string_list(value: Any) -> list[str] | None:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        return None
    return value


def _requirements_covered(
    requirement: PropertyRequirement,
    properties: set[str],
) -> bool:
    return any(alternative.issubset(properties) for alternative in requirement)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _valid_evidence_tables(
    strategy: str,
    value: Any,
    contract: MedicalAuditMappingContract,
) -> bool:
    if not isinstance(value, list):
        return False
    charge_table = contract.tables.get("charge")
    encounter_table = contract.tables.get("encounter")
    if strategy in {"charge_threshold", "included_service_duplicate"}:
        return value == [charge_table]
    if strategy == "daily_overstay":
        return value == [encounter_table, charge_table]
    return value in ([charge_table], [charge_table, encounter_table])


def _expected_evidence_parameters(args: Mapping[str, Any]) -> dict[str, Any]:
    strategy = str(args["strategy"])
    result: dict[str, Any] = {"facility_name": args.get("facility_name")}
    for key in _STRATEGY_ARGUMENTS[strategy]:
        result[key] = args[key]
    return result


def _source_schema(contract: MedicalAuditMappingContract) -> _SourceSchema:
    _validate_mapping_contract(contract)
    try:
        if contract.source_type == "dataset":
            described = datasource_service.list_tables(contract.source)
            objects = {
                str(item.get("name") or "")
                for item in described
                if isinstance(item, Mapping)
            }
            columns_by_relation = {
                str(item.get("name") or ""): {
                    str(column.get("name") or "")
                    for column in (item.get("columns") or [])
                    if isinstance(column, Mapping)
                }
                for item in described
                if isinstance(item, Mapping)
            }
            for logical_name, table_name in contract.tables.items():
                if table_name not in objects:
                    raise MedicalAuditError(
                        "RESOURCE_NOT_FOUND",
                        f"医保审计映射声明的 {logical_name} 逻辑关系不存在。",
                        retryable=False,
                    )
                actual_columns = columns_by_relation.get(table_name, set())
                missing_columns = sorted(
                    column
                    for column in contract.columns.get(logical_name, {}).values()
                    if column not in actual_columns
                )
                if missing_columns:
                    raise MedicalAuditError(
                        "RESOURCE_NOT_FOUND",
                        f"医保审计映射声明的 {logical_name} 字段在物理表中不存在。",
                        retryable=False,
                    )
        else:
            engine = datasource_service.get_engine(contract.source)
            with engine.connect() as connection:
                inspector = sa_inspect(connection)
                objects = set(inspector.get_table_names()) | set(inspector.get_view_names())
                for logical_name, table_name in contract.tables.items():
                    if table_name not in objects:
                        raise MedicalAuditError(
                            "RESOURCE_NOT_FOUND",
                            f"医保审计映射声明的 {logical_name} 物理表不存在。",
                            retryable=False,
                        )
                    actual_columns = {
                        str(column["name"])
                        for column in inspector.get_columns(table_name)
                    }
                    missing_columns = sorted(
                        column
                        for column in contract.columns.get(logical_name, {}).values()
                        if column not in actual_columns
                    )
                    if missing_columns:
                        raise MedicalAuditError(
                            "RESOURCE_NOT_FOUND",
                            f"医保审计映射声明的 {logical_name} 字段在物理表中不存在。",
                            retryable=False,
                        )
    except MedicalAuditError:
        raise
    except (sqlite3.Error, SQLAlchemyError, dataset_query_service.DatasetQueryError) as exc:
        raise MedicalAuditError(
            "RESOURCE_NOT_FOUND",
            "无法核验医保审计运行映射对应的关系型数据源结构。",
            retryable=False,
        ) from exc
    return _SourceSchema(
        source=contract.source,
        path=contract.path,
        tables=dict(contract.tables),
        columns={key: dict(value) for key, value in contract.columns.items()},
    )


def _connect_read_only(source: DataSource) -> _AuditConnection:
    return _AuditConnection(source)


def find_facility_names_in_text(
    mapping_contract: MedicalAuditMappingContract,
    user_message: str,
    *,
    property_access: MedicalAuditAccessPolicy,
) -> list[str]:
    """Resolve exact facility names through the governed charge mapping."""

    if not isinstance(user_message, str):
        raise MedicalAuditError(
            "INVALID_QUERY",
            "医保审计机构范围必须来自当前用户消息。",
            retryable=False,
        )
    if not user_message:
        return []
    property_access.require(_all_of(C_FACILITY_NAME), "机构范围识别")
    schema = _source_schema(mapping_contract)
    table = schema.table("charge")
    column = schema.column("charge", "facility_name")
    quoted_column = _q(column)
    query = f"""
        SELECT DISTINCT TRIM(CAST(c.{quoted_column} AS TEXT)) AS facility_name
        FROM {_q(table)} AS c
        WHERE c.{quoted_column} IS NOT NULL
          AND TRIM(CAST(c.{quoted_column} AS TEXT)) <> ''
          AND instr(?, TRIM(CAST(c.{quoted_column} AS TEXT))) > 0
        ORDER BY LENGTH(facility_name) DESC, facility_name ASC
        LIMIT ?
    """
    try:
        with closing(_connect_read_only(schema.source)) as connection:
            rows = connection.execute(
                query,
                (user_message, MAX_FACILITY_SCOPE_MATCHES + 1),
            ).fetchall()
    except (sqlite3.Error, SQLAlchemyError, dataset_query_service.DatasetQueryError) as exc:
        raise MedicalAuditError(
            "TOOL_EXECUTION_FAILED",
            "无法核验用户指定的医保机构范围。",
            retryable=False,
        ) from exc
    if len(rows) > MAX_FACILITY_SCOPE_MATCHES:
        raise MedicalAuditError(
            "INVALID_QUERY",
            "用户消息命中的医保机构范围过多，无法安全确定审计范围。",
            retryable=False,
        )

    return sorted(
        {
            str(row["facility_name"]).strip()
            for row in rows
            if str(row["facility_name"] or "").strip()
        },
        key=lambda value: (-len(value), value),
    )


def _charge_threshold(
    connection: _AuditConnection,
    schema: _SourceSchema,
    args: Mapping[str, Any],
    *,
    facility_name: str | None,
    limit: int,
    offset: int,
    property_access: MedicalAuditAccessPolicy,
) -> dict[str, Any]:
    service_name = _text(args.get("service_name"), "service_name")
    threshold = _number(args.get("threshold"), "threshold", minimum=0)
    table = schema.table("charge")
    assert table is not None
    required = {
        key: schema.column("charge", key)
        for key in (
            "charge_id",
            "encounter_id",
            "facility_name",
            "service_name",
            "quantity",
            "amount",
        )
    }
    patient = _optional_visible_column(
        schema, "charge", "patient_id", C_PATIENT_ID, property_access
    )
    service_code = _optional_visible_column(
        schema, "charge", "service_code", C_SERVICE_CODE, property_access
    )
    unit_price = _optional_visible_column(
        schema, "charge", "unit_price", C_UNIT_PRICE, property_access
    )
    occurred_at = _optional_visible_column(
        schema, "charge", "occurred_at", C_OCCURRED_AT, property_access
    )
    eligible_amount = _optional_visible_column(
        schema, "charge", "eligible_amount", C_ELIGIBLE_AMOUNT, property_access
    )

    where = [f"c.{_q(required['service_name'])} = ?", f"c.{_q(required['quantity'])} > ?"]
    params: list[Any] = [service_name, threshold]
    if facility_name:
        where.append(f"c.{_q(required['facility_name'])} = ?")
        params.append(facility_name)
    base = f"""
        SELECT
            CAST(c.{_q(required['charge_id'])} AS TEXT) AS charge_line_id,
            CAST(c.{_q(required['encounter_id'])} AS TEXT) AS encounter_id,
            {_optional_text_sql('c', patient)} AS patient_id,
            CAST(c.{_q(required['facility_name'])} AS TEXT) AS facility_name,
            {_optional_text_sql('c', service_code)} AS service_code,
            CAST(c.{_q(required['service_name'])} AS TEXT) AS service_name,
            CAST(c.{_q(required['quantity'])} AS REAL) AS quantity,
            {_optional_number_sql('c', unit_price)} AS unit_price,
            CAST(c.{_q(required['amount'])} AS REAL) AS charged_amount,
            {_optional_number_sql('c', eligible_amount)} AS eligible_amount,
            {_optional_text_sql('c', occurred_at)} AS occurred_at
        FROM {_q(table)} AS c
        WHERE {' AND '.join(where)}
    """
    summary = _summary(
        connection,
        base,
        params,
        amount_field="charged_amount",
        extra_sql="COALESCE(SUM(quantity), 0) AS violating_quantity",
    )
    records = _page_if_present(
        connection, base, params, summary=summary,
        order_by="charged_amount DESC, charge_line_id ASC", limit=limit, offset=offset,
    )
    return {
        "summary": summary,
        "records": records,
        "_used_properties": set(
            next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["charge_threshold"]))
        ),
        "_available_record_fields": {
            "charge_line_id",
            "encounter_id",
            "facility_name",
            "service_name",
            "quantity",
            "charged_amount",
            *({"patient_id"} if patient else set()),
            *({"service_code"} if service_code else set()),
            *({"unit_price"} if unit_price else set()),
            *({"eligible_amount"} if eligible_amount else set()),
            *({"occurred_at"} if occurred_at else set()),
        },
        "_available_summary_fields": {
            "violation_count",
            "affected_encounter_count",
            "violation_amount",
            "violating_quantity",
            *({"affected_patient_count"} if patient else set()),
        },
        "evidence": {
            "rule": "单条收费数量 > threshold",
            "parameters": {
                "facility_name": facility_name,
                "service_name": service_name,
                "threshold": threshold,
            },
            "tables": [table],
            "resolved_columns": _evidence_columns(schema, "charge", required),
        },
    }


def _daily_overstay(
    connection: _AuditConnection,
    schema: _SourceSchema,
    args: Mapping[str, Any],
    *,
    facility_name: str | None,
    limit: int,
    offset: int,
    property_access: MedicalAuditAccessPolicy,
) -> dict[str, Any]:
    service_names = _text_list(args.get("service_names"), "service_names", max_items=20)
    charge_table = schema.table("charge")
    encounter_table = schema.table("encounter")
    assert charge_table is not None and encounter_table is not None
    charge = {
        key: schema.column("charge", key)
        for key in ("encounter_id", "facility_name", "service_name", "quantity", "amount")
    }
    encounter_id = schema.column("encounter", "encounter_id")
    stay_days = schema.column("encounter", "stay_days", required=False)
    started_at = schema.column("encounter", "started_at", required=False)
    ended_at = schema.column("encounter", "ended_at", required=False)
    selected_stay_properties: frozenset[str] | None = None
    if stay_days and property_access.allows(_all_of(E_STAY_DAYS)):
        selected_stay_properties = frozenset({E_STAY_DAYS})
    elif (
        started_at
        and ended_at
        and property_access.allows(_all_of(E_STARTED_AT, E_ENDED_AT))
    ):
        selected_stay_properties = frozenset({E_STARTED_AT, E_ENDED_AT})
        stay_days = None
    elif not property_access.allows(_STAY_REQUIREMENT):
        property_access.require(_STAY_REQUIREMENT, "daily_overstay")
    if selected_stay_properties is None:
        raise MedicalAuditError(
            "INVALID_QUERY",
            "日计价审计需要当前用户可读的住院天数，或可读的就诊开始和结束时间。",
            retryable=False,
        )
    encounter_patient = _optional_visible_column(
        schema, "encounter", "patient_id", E_PATIENT_ID, property_access
    )
    charge_patient = None
    if encounter_patient is None:
        charge_patient = _optional_visible_column(
            schema, "charge", "patient_id", C_PATIENT_ID, property_access
        )
    diagnosis = _optional_visible_column(
        schema, "encounter", "diagnosis_name", E_DIAGNOSIS_NAME, property_access
    )
    unit_price = _optional_visible_column(
        schema, "charge", "unit_price", C_UNIT_PRICE, property_access
    )
    placeholders = ", ".join("?" for _ in service_names)
    where = [f"c.{_q(charge['service_name'])} IN ({placeholders})"]
    params: list[Any] = list(service_names)
    if facility_name:
        where.append(f"c.{_q(charge['facility_name'])} = ?")
        params.append(facility_name)
    if stay_days:
        stay_expression = f"CAST(e.{_q(stay_days)} AS REAL)"
    elif connection.dialect == "mysql":
        stay_expression = (
            f"DATEDIFF(DATE(e.{_q(ended_at)}), DATE(e.{_q(started_at)})) + 1"
        )
    else:
        stay_expression = (
            f"julianday(date(e.{_q(ended_at)})) - "
            f"julianday(date(e.{_q(started_at)})) + 1"
        )
    patient_expression = (
        f"MAX(CAST(e.{_q(encounter_patient)} AS TEXT))"
        if encounter_patient
        else _optional_group_text_sql("c", charge_patient)
    )
    grouped = f"""
        SELECT
            CAST(c.{_q(charge['encounter_id'])} AS TEXT) AS encounter_id,
            {patient_expression} AS patient_id,
            MAX(CAST(c.{_q(charge['facility_name'])} AS TEXT)) AS facility_name,
            CAST(c.{_q(charge['service_name'])} AS TEXT) AS service_name,
            MAX({_optional_text_sql('e', diagnosis)}) AS diagnosis_name,
            MAX({stay_expression}) AS stay_days,
            SUM(COALESCE(CAST(c.{_q(charge['quantity'])} AS REAL), 0)) AS billed_quantity,
            SUM(COALESCE(CAST(c.{_q(charge['amount'])} AS REAL), 0)) AS charged_amount,
            MAX({_optional_number_sql('c', unit_price)}) AS listed_unit_price
        FROM {_q(charge_table)} AS c
        JOIN {_q(encounter_table)} AS e
          ON e.{_q(encounter_id)} = c.{_q(charge['encounter_id'])}
        WHERE {' AND '.join(where)}
        GROUP BY CAST(c.{_q(charge['encounter_id'])} AS TEXT), CAST(c.{_q(charge['service_name'])} AS TEXT)
    """
    violations = """
        SELECT
            encounter_id,
            patient_id,
            facility_name,
            service_name,
            diagnosis_name,
            stay_days,
            billed_quantity,
            billed_quantity - stay_days AS excess_quantity,
            listed_unit_price AS unit_price,
            charged_amount,
            ROUND(
                (billed_quantity - stay_days)
                * CASE WHEN billed_quantity > 0 THEN charged_amount / billed_quantity ELSE 0 END,
                2
            ) AS violation_amount
        FROM grouped
        WHERE stay_days IS NOT NULL AND stay_days >= 0 AND billed_quantity > stay_days
    """
    rows = connection.execute(
        f"""
        WITH
        grouped AS MATERIALIZED ({grouped}),
        violations AS MATERIALIZED ({violations}),
        stats AS (
            SELECT
                COUNT(*) AS violation_count,
                COUNT(DISTINCT encounter_id) AS affected_encounter_count,
                COUNT(DISTINCT CASE
                    WHEN patient_id IS NOT NULL AND patient_id <> '' THEN patient_id END
                ) AS affected_patient_count,
                COALESCE(SUM(violation_amount), 0) AS violation_amount,
                COALESCE(SUM(excess_quantity), 0) AS excess_quantity
            FROM violations
        ),
        scope AS (
            SELECT COUNT(*) AS audited_scope_count FROM grouped
        ),
        page AS MATERIALIZED (
            SELECT 1 AS __present, violations.*
            FROM violations
            ORDER BY violation_amount DESC, encounter_id ASC, service_name ASC
            LIMIT ? OFFSET ?
        )
        SELECT
            stats.violation_count AS __violation_count,
            stats.affected_encounter_count AS __affected_encounter_count,
            stats.affected_patient_count AS __affected_patient_count,
            stats.violation_amount AS __violation_amount,
            stats.excess_quantity AS __excess_quantity,
            scope.audited_scope_count AS __audited_scope_count,
            page.*
        FROM stats
        CROSS JOIN scope
        LEFT JOIN page ON 1 = 1
        ORDER BY page.violation_amount DESC, page.encounter_id ASC, page.service_name ASC
        """,
        [*params, limit, offset],
    ).fetchall()
    first = rows[0]
    summary = {
        "violation_count": int(first["__violation_count"] or 0),
        "affected_encounter_count": int(first["__affected_encounter_count"] or 0),
        "affected_patient_count": int(first["__affected_patient_count"] or 0),
        "violation_amount": _clean_number(first["__violation_amount"]),
        "excess_quantity": _clean_number(first["__excess_quantity"]),
        "audited_scope_count": int(first["__audited_scope_count"] or 0),
    }
    record_columns = tuple(_RECORD_FIELD_REQUIREMENTS["daily_overstay"])
    records = [
        {field: _clean_value(row[field]) for field in record_columns}
        for row in rows
        if row["__present"]
    ]
    resolved_columns = {
        **_evidence_columns(schema, "charge", charge),
        E_ENCOUNTER_ID: encounter_id,
    }
    if stay_days:
        resolved_columns[E_STAY_DAYS] = stay_days
    else:
        resolved_columns[E_STARTED_AT] = started_at
        resolved_columns[E_ENDED_AT] = ended_at
    patient_available = bool(encounter_patient or charge_patient)
    return {
        "summary": summary,
        "records": records,
        "_used_properties": {
            *next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["daily_overstay"])),
            *selected_stay_properties,
            *(
                {E_PATIENT_ID}
                if encounter_patient
                else ({C_PATIENT_ID} if charge_patient else set())
            ),
        },
        "_available_record_fields": {
            "encounter_id",
            "facility_name",
            "service_name",
            "stay_days",
            "billed_quantity",
            "excess_quantity",
            "charged_amount",
            "violation_amount",
            *({"patient_id"} if patient_available else set()),
            *({"diagnosis_name"} if diagnosis else set()),
            *({"unit_price"} if unit_price else set()),
        },
        "_available_summary_fields": {
            "violation_count",
            "affected_encounter_count",
            "violation_amount",
            "excess_quantity",
            "audited_scope_count",
            *({"affected_patient_count"} if patient_available else set()),
        },
        "evidence": {
            "rule": "同一就诊同一日计价项目累计数量 > 住院天数",
            "amount_basis": "超出数量 × 该就诊项目平均实收单价",
            "parameters": {
                "facility_name": facility_name,
                "service_names": service_names,
            },
            "tables": [encounter_table, charge_table],
            "resolved_columns": resolved_columns,
        },
    }


def _included_service_duplicate(
    connection: _AuditConnection,
    schema: _SourceSchema,
    args: Mapping[str, Any],
    *,
    facility_name: str | None,
    limit: int,
    offset: int,
    property_access: MedicalAuditAccessPolicy,
) -> dict[str, Any]:
    included = _text(args.get("included_service"), "included_service")
    duplicate = _text(args.get("duplicate_service"), "duplicate_service")
    if included == duplicate:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            "已包含项目和重复收费项目必须是两个不同的服务项目。",
        )
    table = schema.table("charge")
    assert table is not None
    required = {
        key: schema.column("charge", key)
        for key in (
            "charge_id",
            "encounter_id",
            "facility_name",
            "service_name",
            "quantity",
            "amount",
        )
    }
    patient = _optional_visible_column(
        schema, "charge", "patient_id", C_PATIENT_ID, property_access
    )
    service_code = _optional_visible_column(
        schema, "charge", "service_code", C_SERVICE_CODE, property_access
    )
    unit_price = _optional_visible_column(
        schema, "charge", "unit_price", C_UNIT_PRICE, property_access
    )
    occurred_at = _optional_visible_column(
        schema, "charge", "occurred_at", C_OCCURRED_AT, property_access
    )
    parent_where = [f"p.{_q(required['service_name'])} = ?"]
    child_where = [f"c.{_q(required['service_name'])} = ?"]
    params: list[Any] = [included]
    if facility_name:
        parent_where.append(f"p.{_q(required['facility_name'])} = ?")
        params.append(facility_name)
    params.append(duplicate)
    if facility_name:
        child_where.append(f"c.{_q(required['facility_name'])} = ?")
        params.append(facility_name)
    included_cte = f"""
        SELECT DISTINCT p.{_q(required['encounter_id'])} AS encounter_id
        FROM {_q(table)} AS p
        WHERE {' AND '.join(parent_where)}
    """
    base = f"""
        WITH included_encounters AS ({included_cte})
        SELECT
            CAST(c.{_q(required['charge_id'])} AS TEXT) AS charge_line_id,
            CAST(c.{_q(required['encounter_id'])} AS TEXT) AS encounter_id,
            {_optional_text_sql('c', patient)} AS patient_id,
            CAST(c.{_q(required['facility_name'])} AS TEXT) AS facility_name,
            ? AS included_service,
            {_optional_text_sql('c', service_code)} AS duplicate_service_code,
            CAST(c.{_q(required['service_name'])} AS TEXT) AS duplicate_service,
            CAST(c.{_q(required['quantity'])} AS REAL) AS quantity,
            {_optional_number_sql('c', unit_price)} AS unit_price,
            CAST(c.{_q(required['amount'])} AS REAL) AS charged_amount,
            {_optional_text_sql('c', occurred_at)} AS occurred_at
        FROM {_q(table)} AS c
        JOIN included_encounters AS i
          ON i.encounter_id = c.{_q(required['encounter_id'])}
        WHERE {' AND '.join(child_where)}
    """
    # The literal included-service evidence is the first parameter in the main
    # SELECT, after the CTE parameters and before child filters.
    query_params = params[: (2 if facility_name else 1)] + [included] + params[(2 if facility_name else 1) :]
    summary = _summary(connection, base, query_params, amount_field="charged_amount")
    scope = connection.execute(
        f"WITH included_encounters AS ({included_cte}) SELECT COUNT(*) AS audited_scope_count FROM included_encounters",
        params[: (2 if facility_name else 1)],
    ).fetchone()
    summary["audited_scope_count"] = int(scope["audited_scope_count"] or 0)
    records = _page_if_present(
        connection, base, query_params, summary=summary,
        order_by="charged_amount DESC, charge_line_id ASC", limit=limit, offset=offset,
    )
    return {
        "summary": summary,
        "records": records,
        "_used_properties": set(
            next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["included_service_duplicate"]))
        ),
        "_available_record_fields": {
            "charge_line_id",
            "encounter_id",
            "facility_name",
            "included_service",
            "duplicate_service",
            "quantity",
            "charged_amount",
            *({"patient_id"} if patient else set()),
            *({"duplicate_service_code"} if service_code else set()),
            *({"unit_price"} if unit_price else set()),
            *({"occurred_at"} if occurred_at else set()),
        },
        "_available_summary_fields": {
            "violation_count",
            "affected_encounter_count",
            "violation_amount",
            "audited_scope_count",
            *({"affected_patient_count"} if patient else set()),
        },
        "evidence": {
            "rule": "同一就诊已收 included_service 时，duplicate_service 不得另行收费",
            "parameters": {
                "facility_name": facility_name,
                "included_service": included,
                "duplicate_service": duplicate,
            },
            "tables": [table],
            "resolved_columns": _evidence_columns(schema, "charge", required),
        },
    }


def _limited_drug_duration(
    connection: _AuditConnection,
    schema: _SourceSchema,
    args: Mapping[str, Any],
    *,
    facility_name: str | None,
    limit: int,
    offset: int,
    property_access: MedicalAuditAccessPolicy,
) -> dict[str, Any]:
    drug_name = _text(args.get("drug_name"), "drug_name")
    max_days = _integer(args.get("max_days"), "max_days", minimum=1)
    charge_table = schema.table("charge")
    assert charge_table is not None
    charge = {
        key: schema.column("charge", key)
        for key in ("encounter_id", "facility_name", "service_name", "quantity", "amount")
    }
    occurred_at = schema.column("charge", "occurred_at", required=False)
    cycle_days = schema.column("charge", "cycle_days", required=False)
    selected_duration_properties: frozenset[str] | None = None
    if occurred_at and property_access.allows(_all_of(C_OCCURRED_AT)):
        selected_duration_properties = frozenset({C_OCCURRED_AT})
        if not property_access.allows(_all_of(C_CYCLE_DAYS)):
            cycle_days = None
    elif cycle_days and property_access.allows(_all_of(C_CYCLE_DAYS)):
        selected_duration_properties = frozenset({C_CYCLE_DAYS})
        occurred_at = None
    elif not property_access.allows(_DURATION_REQUIREMENT):
        property_access.require(_DURATION_REQUIREMENT, "limited_drug_duration")
    if selected_duration_properties is None:
        raise MedicalAuditError(
            "INVALID_QUERY",
            "限疗程审计需要当前用户可读的费用发生时间或周期天数。",
            retryable=False,
        )
    patient = _optional_visible_column(
        schema, "charge", "patient_id", C_PATIENT_ID, property_access
    )
    eligible_amount = _optional_visible_column(
        schema, "charge", "eligible_amount", C_ELIGIBLE_AMOUNT, property_access
    )
    encounter_table = schema.table("encounter", required=False)
    encounter_id = (
        _optional_visible_column(
            schema, "encounter", "encounter_id", E_ENCOUNTER_ID, property_access
        )
        if encounter_table
        else None
    )
    diagnosis = (
        _optional_visible_column(
            schema, "encounter", "diagnosis_name", E_DIAGNOSIS_NAME, property_access
        )
        if encounter_table and encounter_id
        else None
    )
    where = [f"c.{_q(charge['service_name'])} = ?"]
    params: list[Any] = [drug_name]
    if facility_name:
        where.append(f"c.{_q(charge['facility_name'])} = ?")
        params.append(facility_name)
    observed_parts: list[str] = []
    if occurred_at:
        observed_parts.append(
            f"COUNT(DISTINCT CASE WHEN c.{_q(occurred_at)} IS NOT NULL "
            f"THEN date(c.{_q(occurred_at)}) END)"
        )
    if cycle_days:
        observed_parts.append(f"COALESCE(MAX(CAST(c.{_q(cycle_days)} AS REAL)), 0)")
    # Actual distinct treatment dates are the deterministic duration evidence.
    # The declared cycle remains visible evidence and is used only as a fallback
    # when the source has no occurrence timestamp.
    observed_expression = observed_parts[0]
    join_sql = ""
    diagnosis_sql = "NULL"
    if encounter_table and encounter_id:
        join_sql = (
            f"LEFT JOIN {_q(encounter_table)} AS e ON "
            f"e.{_q(encounter_id)} = c.{_q(charge['encounter_id'])}"
        )
        diagnosis_sql = f"MAX({_optional_text_sql('e', diagnosis)})"
    grouped = f"""
        SELECT
            CAST(c.{_q(charge['encounter_id'])} AS TEXT) AS encounter_id,
            {_optional_group_text_sql('c', patient)} AS patient_id,
            MAX(CAST(c.{_q(charge['facility_name'])} AS TEXT)) AS facility_name,
            CAST(c.{_q(charge['service_name'])} AS TEXT) AS drug_name,
            {diagnosis_sql} AS diagnosis_name,
            {observed_expression} AS observed_days,
            {f'COALESCE(MAX(CAST(c.{_q(cycle_days)} AS REAL)), 0)' if cycle_days else 'NULL'} AS declared_cycle_days,
            SUM(COALESCE(CAST(c.{_q(charge['quantity'])} AS REAL), 0)) AS quantity,
            SUM(COALESCE(CAST(c.{_q(charge['amount'])} AS REAL), 0)) AS charged_amount,
            SUM({_optional_number_sql('c', eligible_amount)}) AS eligible_amount,
            MIN({_optional_text_sql('c', occurred_at)}) AS first_charge_at,
            MAX({_optional_text_sql('c', occurred_at)}) AS last_charge_at
        FROM {_q(charge_table)} AS c
        {join_sql}
        WHERE {' AND '.join(where)}
        GROUP BY CAST(c.{_q(charge['encounter_id'])} AS TEXT), CAST(c.{_q(charge['service_name'])} AS TEXT)
    """
    base = f"""
        WITH grouped AS ({grouped})
        SELECT
            encounter_id,
            patient_id,
            facility_name,
            drug_name,
            diagnosis_name,
            observed_days,
            declared_cycle_days,
            observed_days - ? AS excess_days,
            quantity,
            charged_amount,
            eligible_amount,
            first_charge_at,
            last_charge_at
        FROM grouped
        WHERE observed_days > ?
    """
    query_params = [*params, max_days, max_days]
    stats = connection.execute(
        f"""
        WITH grouped AS ({grouped})
        SELECT
            COALESCE(SUM(CASE WHEN observed_days > ? THEN 1 ELSE 0 END), 0) AS violation_count,
            COUNT(DISTINCT CASE WHEN observed_days > ? THEN encounter_id END) AS affected_encounter_count,
            COUNT(DISTINCT CASE WHEN observed_days > ? AND patient_id IS NOT NULL
                AND patient_id <> '' THEN patient_id END) AS affected_patient_count,
            COALESCE(SUM(CASE WHEN observed_days > ? THEN charged_amount ELSE 0 END), 0)
                AS violation_amount,
            COUNT(*) AS audited_scope_count,
            COALESCE(MAX(observed_days), 0) AS max_observed_days
        FROM grouped
        """,
        [*params, max_days, max_days, max_days, max_days],
    ).fetchone()
    summary = {
        "violation_count": int(stats["violation_count"] or 0),
        "affected_encounter_count": int(stats["affected_encounter_count"] or 0),
        "affected_patient_count": int(stats["affected_patient_count"] or 0),
        "violation_amount": _clean_number(stats["violation_amount"]),
        "audited_scope_count": int(stats["audited_scope_count"] or 0),
        "max_observed_days": _clean_number(stats["max_observed_days"]),
    }
    records = _page_if_present(
        connection, base, query_params, summary=summary,
        order_by="observed_days DESC, encounter_id ASC", limit=limit, offset=offset,
    )
    tables = [charge_table]
    if encounter_table and encounter_id:
        tables.append(encounter_table)
    return {
        "summary": summary,
        "records": records,
        "_used_properties": {
            *next(iter(_STRATEGY_EXECUTION_REQUIREMENTS["limited_drug_duration"])),
            *selected_duration_properties,
        },
        "_available_record_fields": {
            "encounter_id",
            "facility_name",
            "drug_name",
            "observed_days",
            "excess_days",
            "quantity",
            "charged_amount",
            *({"patient_id"} if patient else set()),
            *({"diagnosis_name"} if diagnosis else set()),
            *({"declared_cycle_days"} if cycle_days else set()),
            *({"eligible_amount"} if eligible_amount else set()),
            *({"first_charge_at", "last_charge_at"} if occurred_at else set()),
        },
        "_available_summary_fields": {
            "violation_count",
            "affected_encounter_count",
            "violation_amount",
            "audited_scope_count",
            "max_observed_days",
            *({"affected_patient_count"} if patient else set()),
        },
        "evidence": {
            "rule": "同一就诊的药品实际用药天数 > max_days",
            "duration_basis": "优先按费用发生日期去重；源库无该字段时使用周期天数",
            "parameters": {
                "facility_name": facility_name,
                "drug_name": drug_name,
                "max_days": max_days,
            },
            "tables": tables,
            "resolved_columns": {
                **_evidence_columns(schema, "charge", charge),
                C_OCCURRED_AT: occurred_at,
                C_CYCLE_DAYS: cycle_days,
            },
        },
    }


def _summary(
    connection: _AuditConnection,
    base_sql: str,
    params: Sequence[Any],
    *,
    amount_field: str,
    extra_sql: str = "",
) -> dict[str, Any]:
    extras = f", {extra_sql}" if extra_sql else ""
    row = connection.execute(
        f"""
        SELECT
            COUNT(*) AS violation_count,
            COUNT(DISTINCT encounter_id) AS affected_encounter_count,
            COUNT(DISTINCT CASE WHEN patient_id IS NOT NULL AND patient_id <> '' THEN patient_id END)
                AS affected_patient_count,
            COALESCE(SUM({amount_field}), 0) AS violation_amount
            {extras}
        FROM ({base_sql}) AS violations
        """,
        params,
    ).fetchone()
    result = {
        "violation_count": int(row["violation_count"] or 0),
        "affected_encounter_count": int(row["affected_encounter_count"] or 0),
        "affected_patient_count": int(row["affected_patient_count"] or 0),
        "violation_amount": _clean_number(row["violation_amount"]),
    }
    if extra_sql:
        alias = extra_sql.rsplit(" AS ", 1)[-1].strip()
        result[alias] = _clean_number(row[alias])
    return result


def _page(
    connection: _AuditConnection,
    base_sql: str,
    params: Sequence[Any],
    *,
    order_by: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT * FROM ({base_sql}) AS violations ORDER BY {order_by} LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [
        {key: _clean_value(row[key]) for key in row.keys()}
        for row in rows
    ]


def _page_if_present(
    connection: _AuditConnection,
    base_sql: str,
    params: Sequence[Any],
    *,
    summary: Mapping[str, Any],
    order_by: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    if offset >= int(summary.get("violation_count") or 0):
        return []
    return _page(
        connection,
        base_sql,
        params,
        order_by=order_by,
        limit=limit,
        offset=offset,
    )


def _evidence_columns(
    schema: _SourceSchema,
    logical_table: str,
    required: Mapping[str, str | None],
) -> dict[str, str | None]:
    return {
        _SOURCE_PROPERTY_REFS.get(logical_table, {}).get(logical, ""): actual
        for logical, actual in required.items()
        if actual and _SOURCE_PROPERTY_REFS.get(logical_table, {}).get(logical)
    }


def _optional_visible_column(
    schema: _SourceSchema,
    logical_table: str,
    logical_column: str,
    property_ref: str,
    property_access: MedicalAuditAccessPolicy,
) -> str | None:
    if not property_access.allows(_all_of(property_ref)):
        return None
    return schema.column(logical_table, logical_column, required=False)


def _pagination(args: Mapping[str, Any]) -> tuple[int, int]:
    limit = _integer(args.get("limit", DEFAULT_LIMIT), "limit", minimum=1)
    offset = _integer(args.get("offset", 0), "offset", minimum=0)
    if limit > MAX_LIMIT:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"limit 不能超过 {MAX_LIMIT}。",
        )
    return limit, offset


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 必须是字符串。",
        )
    text = value.strip()
    if not text:
        raise MedicalAuditError("INVALID_TOOL_ARGUMENTS", f"缺少必填参数 {field}。")
    if len(text) > 200:
        raise MedicalAuditError("INVALID_TOOL_ARGUMENTS", f"参数 {field} 过长。")
    return text


def _optional_text(value: Any, field: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _text(value, field)


def _text_list(value: Any, field: str, *, max_items: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 必须是非空字符串数组。",
        )
    if len(value) > max_items:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 最多包含 {max_items} 项。",
        )
    result = [_text(item, field) for item in value]
    if len(set(result)) != len(result):
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 不能包含重复项目。",
        )
    return result


def _number(value: Any, field: str, *, minimum: float) -> float:
    if isinstance(value, bool):
        raise MedicalAuditError("INVALID_TOOL_ARGUMENTS", f"参数 {field} 必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 必须是数字。",
        ) from exc
    if not math.isfinite(number) or number < minimum:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 必须是不小于 {minimum:g} 的有限数字。",
        )
    return number


def _integer(value: Any, field: str, *, minimum: int) -> int:
    if isinstance(value, bool):
        raise MedicalAuditError("INVALID_TOOL_ARGUMENTS", f"参数 {field} 必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 必须是整数。",
        ) from exc
    if str(value).strip() not in {str(number), f"{number}.0"} or number < minimum:
        raise MedicalAuditError(
            "INVALID_TOOL_ARGUMENTS",
            f"参数 {field} 必须是不小于 {minimum} 的整数。",
        )
    return number


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _q(value: str | None) -> str:
    if not value:
        raise MedicalAuditError(
            "INVALID_QUERY",
            "审计所需字段未解析。",
            retryable=False,
        )
    return _quote_identifier(value)


def _optional_text_sql(alias: str, column: str | None) -> str:
    return f"CAST({alias}.{_q(column)} AS TEXT)" if column else "NULL"


def _optional_group_text_sql(alias: str, column: str | None) -> str:
    return f"MAX(CAST({alias}.{_q(column)} AS TEXT))" if column else "NULL"


def _optional_number_sql(alias: str, column: str | None) -> str:
    return f"COALESCE(CAST({alias}.{_q(column)} AS REAL), 0)" if column else "0"


def _clean_number(value: Any) -> int | float:
    number = float(value or 0)
    rounded = round(number, 2)
    return int(rounded) if rounded.is_integer() else rounded


def _clean_value(value: Any) -> Any:
    if isinstance(value, (float, Decimal)):
        return _clean_number(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value
