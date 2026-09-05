"""Pure, protocol-neutral matching for governed invocation input contracts.

The validator consumes bounded structural profiles only. File names, relation
names, object locations and raw rows are deliberately outside its contract so
renaming an upload cannot change whether its content satisfies a port.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from . import input_contract_schema


CONTENT_CONTRACT_KEY = input_contract_schema.CONTENT_CONTRACT_KEY
CONTENT_CONTRACT_VERSION = input_contract_schema.CONTENT_CONTRACT_VERSION
InputContractError = input_contract_schema.InputContractError

_MAX_RELATIONS = input_contract_schema._MAX_RELATIONS
_MAX_FIELDS = input_contract_schema._MAX_FIELDS
_MAX_FIELD_ALIASES = input_contract_schema._MAX_FIELD_ALIASES
_MAX_FIELD_NAME_CHARS = input_contract_schema._MAX_FIELD_NAME_CHARS
_CONTENT_KEYS = input_contract_schema._CONTENT_KEYS
_RELATION_KEYS = input_contract_schema._RELATION_KEYS
_FIELD_KEYS = input_contract_schema._FIELD_KEYS
_LOGICAL_TYPES = input_contract_schema._LOGICAL_TYPES

_FieldContract = input_contract_schema._FieldContract
_RelationContract = input_contract_schema._RelationContract
_TabularContract = input_contract_schema._TabularContract
_PortContract = input_contract_schema._PortContract

_read = input_contract_schema._read
_normalized_name = input_contract_schema._normalized_name
normalize_content_name = input_contract_schema.normalize_content_name
_boolean = input_contract_schema._boolean
_closed_mapping = input_contract_schema._closed_mapping
_contract_name = input_contract_schema._contract_name
_types = input_contract_schema._types
_content_document = input_contract_schema._content_document
has_content_contract = input_contract_schema.has_content_contract
validate_content_contract = input_contract_schema.validate_content_contract
_parse_contract = input_contract_schema._parse_contract
_profile_tables = input_contract_schema._profile_tables
structural_fingerprint = input_contract_schema.structural_fingerprint
build_tabular_content_contract = input_contract_schema.build_tabular_content_contract
build_observed_tabular_profile = input_contract_schema.build_observed_tabular_profile
_port = input_contract_schema._port


@dataclass(frozen=True, slots=True)
class ProfileValidation:
    matched: bool
    structural_fingerprint: str
    relation_matches: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ObservedInput:
    index: int
    binding_kind: str
    reference_id: str
    profile: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or self.index < 0:
            raise InputContractError(
                "invalid_observed_input",
                "Observed input index must be a non-negative integer",
            )
        kind = str(self.binding_kind or "").strip().lower()
        reference = str(self.reference_id or "").strip()
        if not kind or not reference or not isinstance(self.profile, Mapping):
            raise InputContractError(
                "invalid_observed_input",
                "Observed input requires a binding kind, reference and profile",
            )
        object.__setattr__(self, "binding_kind", kind)
        object.__setattr__(self, "reference_id", reference)
        object.__setattr__(self, "profile", MappingProxyType(dict(self.profile)))


@dataclass(frozen=True, slots=True)
class InputAssignment:
    port_key: str
    input_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class InputMatchResult:
    assignments: tuple[InputAssignment, ...]
    supplementary_indices: tuple[int, ...]


def _type_matches(expected: frozenset[str], observed: str) -> bool:
    if not expected:
        return True
    if observed in expected:
        return True
    return observed == "integer" and "number" in expected


def _relation_matches(contract: _RelationContract, table: Mapping[str, Any]) -> bool:
    columns = table["columns"]
    matched_columns: set[str] = set()
    for field in contract.fields:
        candidates = [name for name in field.names if name in columns]
        if len(candidates) > 1:
            raise InputContractError(
                "content_contract_ambiguous",
                "Observed relation matches more than one alias for a contract field",
            )
        if not candidates:
            if field.required:
                return False
            continue
        selected = candidates[0]
        if not _type_matches(field.logical_types, columns[selected]):
            return False
        matched_columns.add(selected)
    if not contract.allow_additional_fields and set(columns) != matched_columns:
        return False
    return int(table["row_count"]) >= contract.minimum_data_rows


def _relation_assignments(
    contract: _TabularContract,
    tables: tuple[dict[str, Any], ...],
) -> list[tuple[int, ...]]:
    candidates = [
        tuple(
            table_index
            for table_index, table in enumerate(tables)
            if _relation_matches(relation, table)
        )
        for relation in contract.relations
    ]
    if any(not values for values in candidates):
        return []
    solutions: list[tuple[int, ...]] = []

    def visit(index: int, used: frozenset[int], selected: tuple[int, ...]) -> None:
        if len(solutions) >= 2:
            return
        if index >= len(candidates):
            if contract.allow_additional_relations or len(used) == len(tables):
                solutions.append(selected)
            return
        for candidate in candidates[index]:
            if candidate not in used:
                visit(index + 1, used | {candidate}, (*selected, candidate))

    visit(0, frozenset(), ())
    return solutions


def validate_profile(
    schema_document: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> ProfileValidation:
    return validate_profile_for_cardinality(
        schema_document,
        profile,
        cardinality="one",
    )


def validate_profile_for_cardinality(
    schema_document: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    cardinality: str,
) -> ProfileValidation:
    """Validate one managed input, including a repeated single-relation bundle."""

    if not isinstance(profile, Mapping):
        raise InputContractError(
            "invalid_observed_profile", "Observed input profile must be an object"
        )
    normalized_cardinality = str(cardinality or "one").strip().lower()
    if normalized_cardinality not in {"one", "many"}:
        raise InputContractError(
            "invalid_content_contract", "Input port cardinality is invalid"
        )
    fingerprint = structural_fingerprint(profile)
    contract = _parse_contract(schema_document)
    if contract is None:
        return ProfileValidation(matched=True, structural_fingerprint=fingerprint)
    tables = _profile_tables(profile)
    if normalized_cardinality == "many" and len(contract.relations) == 1:
        matches = tuple(
            table_index
            for table_index, table in enumerate(tables)
            if _relation_matches(contract.relations[0], table)
        )
        if not matches or (
            not contract.allow_additional_relations and len(matches) != len(tables)
        ):
            raise InputContractError(
                "content_contract_missing",
                "Input content does not satisfy the required structural contract",
            )
        return ProfileValidation(
            matched=True,
            structural_fingerprint=fingerprint,
            relation_matches=matches,
        )
    solutions = _relation_assignments(contract, tables)
    if not solutions:
        raise InputContractError(
            "content_contract_missing",
            "Input content does not satisfy the required structural contract",
        )
    if len(solutions) > 1:
        raise InputContractError(
            "content_contract_ambiguous",
            "Input content matches the structural contract ambiguously",
        )
    return ProfileValidation(
        matched=True,
        structural_fingerprint=fingerprint,
        relation_matches=solutions[0],
    )


def _compatible_kind(port: _PortContract, observed: ObservedInput) -> bool:
    return not port.binding_kinds or observed.binding_kind in port.binding_kinds


def match_inputs(
    ports: Iterable[Any],
    observed_inputs: Iterable[ObservedInput],
) -> InputMatchResult:
    normalized_ports = tuple(_port(value) for value in ports)
    keys = [item.key for item in normalized_ports]
    if len(keys) != len(set(keys)):
        raise InputContractError(
            "invalid_content_contract", "Input ports contain duplicate keys"
        )
    observed = tuple(observed_inputs)
    indices = [item.index for item in observed]
    if len(indices) != len(set(indices)):
        raise InputContractError(
            "invalid_observed_input", "Observed inputs contain duplicate indices"
        )
    by_index = {item.index: item for item in observed}
    candidates_by_port: dict[str, tuple[int, ...]] = {}
    content_ports = [item for item in normalized_ports if item.content_contract]
    for port in content_ports:
        candidates: list[int] = []
        for item in observed:
            if not _compatible_kind(port, item):
                continue
            try:
                validate_profile_for_cardinality(
                    port.schema_document,
                    item.profile,
                    cardinality=port.cardinality,
                )
            except InputContractError as exc:
                if exc.code == "content_contract_missing":
                    continue
                raise
            candidates.append(item.index)
        candidates_by_port[port.key] = tuple(candidates)

    for input_index in indices:
        matching_ports = [
            port.key
            for port in content_ports
            if input_index in candidates_by_port[port.key]
        ]
        if len(matching_ports) > 1:
            raise InputContractError(
                "content_contract_ambiguous",
                "One input matches more than one governed port contract",
                details={"input_index": input_index, "port_keys": matching_ports},
            )

    assignments: list[InputAssignment] = []
    consumed: set[int] = set()
    for port in content_ports:
        candidates = tuple(
            index for index in candidates_by_port[port.key] if index not in consumed
        )
        if port.cardinality == "one" and len(candidates) > 1:
            raise InputContractError(
                "content_contract_ambiguous",
                "More than one input satisfies a single-value port contract",
                details={"port_key": port.key, "input_indices": candidates},
            )
        selected = candidates if port.cardinality == "many" else candidates[:1]
        if port.required and not selected:
            raise InputContractError(
                "content_contract_missing",
                "A required governed input contract is not satisfied",
                details={"port_key": port.key},
            )
        if selected:
            assignments.append(InputAssignment(port.key, selected))
            consumed.update(selected)

    for port in (item for item in normalized_ports if item.content_contract is None):
        candidates = tuple(
            index
            for index, item in by_index.items()
            if index not in consumed and _compatible_kind(port, item)
        )
        selected = candidates if port.cardinality == "many" else candidates[:1]
        if port.required and not selected:
            raise InputContractError(
                "content_contract_missing",
                "A required input port has no compatible managed input",
                details={"port_key": port.key},
            )
        if selected:
            assignments.append(InputAssignment(port.key, selected))
            consumed.update(selected)

    return InputMatchResult(
        assignments=tuple(assignments),
        supplementary_indices=tuple(sorted(set(indices) - consumed)),
    )


__all__ = [
    "CONTENT_CONTRACT_KEY",
    "CONTENT_CONTRACT_VERSION",
    "InputAssignment",
    "InputContractError",
    "InputMatchResult",
    "ObservedInput",
    "ProfileValidation",
    "build_tabular_content_contract",
    "build_observed_tabular_profile",
    "has_content_contract",
    "match_inputs",
    "normalize_content_name",
    "structural_fingerprint",
    "validate_content_contract",
    "validate_profile",
    "validate_profile_for_cardinality",
]
