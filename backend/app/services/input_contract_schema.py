"""Parsing and construction for bounded governed input schemas."""
from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .capability_contracts import canonical_hash


CONTENT_CONTRACT_KEY = "x-platform-input-contract"
CONTENT_CONTRACT_VERSION = "tabular-content/v1"
_MAX_RELATIONS = 64
_MAX_FIELDS = 512
_MAX_FIELD_ALIASES = 32
_MAX_FIELD_NAME_CHARS = 300
_CONTENT_KEYS = frozenset({"version", "relations", "allow_additional_relations"})
_RELATION_KEYS = frozenset({
    "fields",
    "minimum_data_rows",
    "allow_additional_fields",
})
_FIELD_KEYS = frozenset({
    "name",
    "aliases",
    "logical_type",
    "logical_types",
    "required",
})
_LOGICAL_TYPES = {
    "boolean",
    "date",
    "datetime",
    "integer",
    "null",
    "number",
    "object",
    "string",
    "unknown",
}


class InputContractError(ValueError):
    """A safe deterministic failure while validating structural input."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "input_contract_invalid")
        self.message = str(message or "Input contract validation failed")
        self.details = MappingProxyType(dict(details or {}))


@dataclass(frozen=True, slots=True)
class _FieldContract:
    names: frozenset[str]
    logical_types: frozenset[str]
    required: bool


@dataclass(frozen=True, slots=True)
class _RelationContract:
    fields: tuple[_FieldContract, ...]
    minimum_data_rows: int
    allow_additional_fields: bool


@dataclass(frozen=True, slots=True)
class _TabularContract:
    relations: tuple[_RelationContract, ...]
    allow_additional_relations: bool


@dataclass(frozen=True, slots=True)
class _PortContract:
    key: str
    required: bool
    cardinality: str
    binding_kinds: frozenset[str]
    schema_document: Mapping[str, Any]
    content_contract: _TabularContract | None


def _read(value: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _normalized_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return " ".join(text.split())


def normalize_content_name(value: Any) -> str:
    """Return the canonical comparison form used for headers and aliases."""

    return _normalized_name(value)


def _boolean(value: Any, label: str, *, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise InputContractError(
            "invalid_content_contract", f"{label} must be boolean"
        )
    return value


def _closed_mapping(
    value: Mapping[Any, Any],
    allowed: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise InputContractError(
            "invalid_content_contract",
            f"{label} contains unsupported fields: {', '.join(unknown[:10])}",
        )


def _contract_name(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) > _MAX_FIELD_NAME_CHARS:
        raise InputContractError(
            "invalid_content_contract",
            f"{label} must be a bounded string",
        )
    normalized = _normalized_name(value)
    if not normalized:
        raise InputContractError(
            "invalid_content_contract",
            f"{label} must not be empty",
        )
    return normalized


def _types(value: Any, label: str) -> frozenset[str]:
    if value in (None, "", (), []):
        return frozenset()
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
        raise InputContractError(
            "invalid_content_contract", f"{label} must be an array"
        )
    normalized = frozenset(str(item or "").strip().lower() for item in values)
    if not normalized or not normalized <= _LOGICAL_TYPES:
        raise InputContractError(
            "invalid_content_contract", f"{label} contains an unsupported type"
        )
    return normalized


def _content_document(schema_document: Any) -> Mapping[str, Any] | None:
    if not isinstance(schema_document, Mapping):
        raise InputContractError(
            "invalid_content_contract", "Port schema document must be an object"
        )
    if CONTENT_CONTRACT_KEY not in schema_document:
        return None
    raw = schema_document[CONTENT_CONTRACT_KEY]
    if not isinstance(raw, Mapping):
        raise InputContractError(
            "invalid_content_contract", "Content contract must be an object"
        )
    return raw


def has_content_contract(schema_document: Any) -> bool:
    return _content_document(schema_document) is not None


def validate_content_contract(schema_document: Any) -> bool:
    """Parse one optional contract and reject malformed definitions early."""

    return _parse_contract(schema_document) is not None


def _parse_contract(schema_document: Any) -> _TabularContract | None:
    raw = _content_document(schema_document)
    if raw is None:
        return None
    _closed_mapping(raw, _CONTENT_KEYS, "Content contract")
    if str(raw.get("version") or "") != CONTENT_CONTRACT_VERSION:
        raise InputContractError(
            "invalid_content_contract", "Content contract version is unsupported"
        )
    raw_relations = raw.get("relations")
    if (
        not isinstance(raw_relations, Sequence)
        or isinstance(raw_relations, (str, bytes, bytearray))
        or not raw_relations
        or len(raw_relations) > _MAX_RELATIONS
    ):
        raise InputContractError(
            "invalid_content_contract",
            f"Content contract must declare 1-{_MAX_RELATIONS} relations",
        )
    relations: list[_RelationContract] = []
    for relation_index, raw_relation in enumerate(raw_relations):
        if not isinstance(raw_relation, Mapping):
            raise InputContractError(
                "invalid_content_contract", "Relation contract must be an object"
            )
        _closed_mapping(raw_relation, _RELATION_KEYS, f"Relation {relation_index}")
        raw_fields = raw_relation.get("fields")
        if (
            not isinstance(raw_fields, Sequence)
            or isinstance(raw_fields, (str, bytes, bytearray))
            or not raw_fields
            or len(raw_fields) > _MAX_FIELDS
        ):
            raise InputContractError(
                "invalid_content_contract",
                f"Relation {relation_index} must declare 1-{_MAX_FIELDS} fields",
            )
        fields: list[_FieldContract] = []
        claimed_names: set[str] = set()
        for raw_field in raw_fields:
            if not isinstance(raw_field, Mapping):
                raise InputContractError(
                    "invalid_content_contract", "Field contract must be an object"
                )
            _closed_mapping(raw_field, _FIELD_KEYS, "Field contract")
            if "logical_type" in raw_field and "logical_types" in raw_field:
                raise InputContractError(
                    "invalid_content_contract",
                    "Field contract cannot declare both logical_type and logical_types",
                )
            primary = _contract_name(raw_field.get("name"), "Field name")
            raw_aliases = raw_field.get("aliases") or []
            if not isinstance(raw_aliases, Sequence) or isinstance(
                raw_aliases, (str, bytes, bytearray)
            ) or len(raw_aliases) > _MAX_FIELD_ALIASES:
                raise InputContractError(
                    "invalid_content_contract",
                    f"Field aliases must be an array of at most {_MAX_FIELD_ALIASES} items",
                )
            names = frozenset(
                item
                for item in (
                    primary,
                    *(
                        _contract_name(alias, "Field alias")
                        for alias in raw_aliases
                    ),
                )
                if item
            )
            if not names or claimed_names.intersection(names):
                raise InputContractError(
                    "invalid_content_contract",
                    "Field names and aliases must be non-empty and unambiguous",
                )
            claimed_names.update(names)
            fields.append(
                _FieldContract(
                    names=names,
                    logical_types=_types(
                        raw_field.get("logical_types", raw_field.get("logical_type")),
                        "Field logical types",
                    ),
                    required=_boolean(
                        raw_field.get("required"), "Field required", default=True
                    ),
                )
            )
        raw_minimum = raw_relation.get("minimum_data_rows", 0)
        if isinstance(raw_minimum, bool) or not isinstance(raw_minimum, int):
            raise InputContractError(
                "invalid_content_contract", "minimum_data_rows must be an integer"
            )
        if raw_minimum < 0 or raw_minimum > 1_000_000_000:
            raise InputContractError(
                "invalid_content_contract", "minimum_data_rows is outside the limit"
            )
        relations.append(
            _RelationContract(
                fields=tuple(fields),
                minimum_data_rows=raw_minimum,
                allow_additional_fields=_boolean(
                    raw_relation.get("allow_additional_fields"),
                    "allow_additional_fields",
                    default=True,
                ),
            )
        )
    return _TabularContract(
        relations=tuple(relations),
        allow_additional_relations=_boolean(
            raw.get("allow_additional_relations"),
            "allow_additional_relations",
            default=True,
        ),
    )


def _profile_tables(profile: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    if str(profile.get("category") or "").strip().lower() != "table":
        return ()
    raw_tables = profile.get("tables") or []
    if not isinstance(raw_tables, Sequence) or isinstance(
        raw_tables, (str, bytes, bytearray)
    ):
        raise InputContractError(
            "invalid_observed_profile", "Observed profile tables must be an array"
        )
    if len(raw_tables) > _MAX_RELATIONS:
        raise InputContractError(
            "invalid_observed_profile", "Observed profile has too many relations"
        )
    tables: list[dict[str, Any]] = []
    for raw_table in raw_tables:
        if not isinstance(raw_table, Mapping):
            raise InputContractError(
                "invalid_observed_profile", "Observed relation must be an object"
            )
        raw_columns = raw_table.get("columns") or raw_table.get("fields") or []
        if not isinstance(raw_columns, Sequence) or isinstance(
            raw_columns, (str, bytes, bytearray)
        ):
            raise InputContractError(
                "invalid_observed_profile", "Observed fields must be an array"
            )
        if not raw_columns or len(raw_columns) > _MAX_FIELDS:
            raise InputContractError(
                "invalid_observed_profile", "Observed relation field count is invalid"
            )
        columns: dict[str, str] = {}
        for raw_column in raw_columns:
            if not isinstance(raw_column, Mapping):
                raise InputContractError(
                    "invalid_observed_profile", "Observed field must be an object"
                )
            name = _normalized_name(
                raw_column.get("name", raw_column.get("source_name"))
            )
            logical_type = str(
                raw_column.get("logical_type")
                or raw_column.get("type")
                or "unknown"
            ).strip().lower()
            if not name or logical_type not in _LOGICAL_TYPES:
                raise InputContractError(
                    "invalid_observed_profile", "Observed field metadata is invalid"
                )
            if name in columns:
                raise InputContractError(
                    "content_contract_ambiguous",
                    "Observed relation contains duplicate normalized field names",
                )
            columns[name] = logical_type
        raw_rows = raw_table.get(
            "record_count", raw_table.get("sample_row_count", 0)
        )
        if isinstance(raw_rows, bool) or not isinstance(raw_rows, int) or raw_rows < 0:
            raise InputContractError(
                "invalid_observed_profile", "Observed row count is invalid"
            )
        tables.append({"columns": columns, "row_count": raw_rows})
    return tuple(tables)


def structural_fingerprint(profile: Mapping[str, Any]) -> str:
    tables = _profile_tables(profile)
    document = {
        "category": str(profile.get("category") or "").strip().lower(),
        "relations": sorted(
            (
                {
                    "fields": sorted(table["columns"].items()),
                    "record_count": table["row_count"],
                }
                for table in tables
            ),
            key=lambda item: repr(item),
        ),
    }
    return canonical_hash(document, domain="observed-input-structure-v1")


def build_tabular_content_contract(relations: Iterable[Any]) -> dict[str, Any]:
    """Build a portable content contract from an authorized modeling schema."""

    relation_documents: list[dict[str, Any]] = []
    ordered_relations = sorted(
        tuple(relations),
        key=lambda item: (
            int(_read(item, "ordinal", default=0) or 0),
            str(_read(item, "id", default="") or ""),
        ),
    )
    if not ordered_relations or len(ordered_relations) > _MAX_RELATIONS:
        raise InputContractError(
            "invalid_content_contract",
            "A tabular modeling schema must contain a bounded relation set",
        )
    for relation in ordered_relations:
        raw_fields = tuple(_read(relation, "fields", default=()) or ())
        ordered_fields = sorted(
            raw_fields,
            key=lambda item: (
                int(_read(item, "ordinal", default=0) or 0),
                str(_read(item, "id", default="") or ""),
            ),
        )
        if not ordered_fields or len(ordered_fields) > _MAX_FIELDS:
            raise InputContractError(
                "invalid_content_contract",
                "Each tabular modeling relation must declare a bounded field set",
            )
        fields: list[dict[str, Any]] = []
        for field in ordered_fields:
            source_name = str(_read(field, "source_name", "name", default="") or "").strip()
            field_key = str(_read(field, "field_key", "key", default="") or "").strip()
            primary = source_name or field_key
            logical_type = str(
                _read(field, "logical_type", "type", default="unknown") or "unknown"
            ).strip().lower()
            if not primary or logical_type not in _LOGICAL_TYPES:
                raise InputContractError(
                    "invalid_content_contract",
                    "Modeling schema field metadata is invalid",
                )
            aliases = [
                value
                for value in (field_key, source_name)
                if value and _normalized_name(value) != _normalized_name(primary)
            ]
            fields.append(
                {
                    "name": primary,
                    "aliases": aliases,
                    "logical_types": [logical_type],
                    "required": True,
                }
            )
        relation_documents.append(
            {
                "fields": fields,
                "minimum_data_rows": 1,
                "allow_additional_fields": True,
            }
        )
    document = {
        "version": CONTENT_CONTRACT_VERSION,
        "relations": relation_documents,
        "allow_additional_relations": True,
    }
    # Parse before returning so malformed aliases/types never enter a release.
    _parse_contract({CONTENT_CONTRACT_KEY: document})
    return document


def build_observed_tabular_profile(
    relations: Iterable[Any],
    *,
    record_count: int,
    relation_row_counts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize an observed DatasetSchema without retaining relation names."""

    if isinstance(record_count, bool) or not isinstance(record_count, int) or record_count < 0:
        raise InputContractError(
            "invalid_observed_profile", "Observed dataset row count is invalid"
        )
    ordered_relations = sorted(
        tuple(relations),
        key=lambda item: (
            int(_read(item, "ordinal", default=0) or 0),
            str(_read(item, "id", default="") or ""),
        ),
    )
    tables: list[dict[str, Any]] = []
    for relation in ordered_relations:
        fields = sorted(
            tuple(_read(relation, "fields", default=()) or ()),
            key=lambda item: (
                int(_read(item, "ordinal", default=0) or 0),
                str(_read(item, "id", default="") or ""),
            ),
        )
        relation_key = str(_read(relation, "relation_key", "key", default="") or "")
        raw_relation_count = (
            relation_row_counts.get(relation_key)
            if relation_row_counts is not None
            else None
        )
        row_count = _read(
            raw_relation_count,
            "row_count",
            "record_count",
            default=raw_relation_count,
        )
        if row_count is None:
            # A total count proves a single relation is non-empty. It cannot
            # prove which relation contains rows in a multi-relation dataset.
            row_count = record_count if len(ordered_relations) == 1 else 0
        if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
            raise InputContractError(
                "invalid_observed_profile", "Observed relation row count is invalid"
            )
        tables.append(
            {
                "columns": [
                    {
                        "name": str(
                            _read(field, "source_name", "field_key", "name", default="")
                            or ""
                        ),
                        "logical_type": str(
                            _read(field, "logical_type", "type", default="unknown")
                            or "unknown"
                        ),
                    }
                    for field in fields
                ],
                "record_count": row_count,
            }
        )
    profile = {"category": "table", "tables": tables}
    _profile_tables(profile)
    return profile


def _port(value: Any) -> _PortContract:
    key = str(_read(value, "port_key", "key", default="") or "").strip().lower()
    cardinality = str(_read(value, "cardinality", default="one") or "one").lower()
    raw_kinds = _read(value, "binding_kinds", default=()) or ()
    if isinstance(raw_kinds, str):
        raw_kinds = (raw_kinds,)
    if not key or cardinality not in {"one", "many"} or not isinstance(
        raw_kinds, Sequence
    ):
        raise InputContractError(
            "invalid_content_contract", "Input port contract is invalid"
        )
    schema_document = _read(value, "schema_document", "schema", default={}) or {}
    return _PortContract(
        key=key,
        required=bool(_read(value, "required", "is_required", default=True)),
        cardinality=cardinality,
        binding_kinds=frozenset(str(item).strip().lower() for item in raw_kinds),
        schema_document=schema_document,
        content_contract=_parse_contract(schema_document),
    )


__all__ = [
    "CONTENT_CONTRACT_KEY",
    "CONTENT_CONTRACT_VERSION",
    "InputContractError",
    "build_observed_tabular_profile",
    "build_tabular_content_contract",
    "has_content_contract",
    "normalize_content_name",
    "structural_fingerprint",
    "validate_content_contract",
]
