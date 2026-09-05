"""Read-only semantic object-set queries over an explicit DatasetVersion."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, ClassVar, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from . import query_provider_compatibility

from ..models import (
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
)
from ..services import (
    business_query_service,
    capability_readiness_service,
    input_contract_validator,
    permission_service,
    runtime_definition_service,
)
from ..services.capability_contracts import (
    Actor,
    CapabilityRef,
    Request,
    ResolvedDeployment,
    RuntimeDataContext,
)
from ..services.provider_actor_service import require_actor_session


class SemanticDatasetQueryProviderError(ValueError):
    """The governed semantic dataset query cannot be resolved or executed."""


@dataclass(frozen=True, slots=True)
class _PreparedQueryExtension:
    query_template: Mapping[str, Any] | None
    context: Any = None
    terminal_output: Mapping[str, Any] | None = None


class _QueryExtension(Protocol):
    """Opaque Provider-owned query specialization used by the shared engine."""

    def prepare(
        self,
        definition: Any,
        function: Any,
        inputs: Mapping[str, Any],
    ) -> _PreparedQueryExtension: ...

    def finalize(
        self,
        context: Any,
        result: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class _QueryBinding:
    mapping_ids: tuple[str, ...]
    query_template: Mapping[str, Any] | None = None
    extension: _QueryExtension | None = None


def _query_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
                "description": "符合条件的对象集或聚合结果",
            },
            "columns": {"type": "array", "items": {"type": "string"}},
            "row_count": {"type": "integer", "minimum": 0},
            "truncated": {"type": "boolean"},
            "offset": {"type": "integer", "minimum": 0},
            "next_offset": {
                "anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]
            },
        },
        "required": [
            "records",
            "columns",
            "row_count",
            "truncated",
            "offset",
            "next_offset",
        ],
        "additionalProperties": False,
    }


def _text(value: Any, label: str, *, maximum: int = 240) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise SemanticDatasetQueryProviderError(f"{label} is invalid")
    return normalized


def _template_input_names(value: Any, *, depth: int = 0) -> set[str]:
    if depth > 20:
        raise SemanticDatasetQueryProviderError("query template is too deeply nested")
    if isinstance(value, Mapping):
        if "$input" in value:
            if set(value) != {"$input"}:
                raise SemanticDatasetQueryProviderError(
                    "query template input references cannot contain other fields"
                )
            return {_text(value["$input"], "query template input", maximum=100)}
        names: set[str] = set()
        for key, child in value.items():
            _text(key, "query template field", maximum=100)
            names.update(_template_input_names(child, depth=depth + 1))
        return names
    if isinstance(value, (list, tuple)):
        if len(value) > 200:
            raise SemanticDatasetQueryProviderError("query template is too large")
        names: set[str] = set()
        for child in value:
            names.update(_template_input_names(child, depth=depth + 1))
        return names
    if value is None or isinstance(value, (str, bool, int)):
        return set()
    if isinstance(value, float) and math.isfinite(value):
        return set()
    raise SemanticDatasetQueryProviderError("query template contains an invalid value")


def _resolve_query_template(value: Any, inputs: Mapping[str, Any]) -> Any:
    if isinstance(value, Mapping):
        if set(value) == {"$input"}:
            name = _text(value["$input"], "query template input", maximum=100)
            if name not in inputs:
                raise SemanticDatasetQueryProviderError(
                    "query template requires an invocation input"
                )
            return copy.deepcopy(inputs[name])
        return {
            str(key): _resolve_query_template(child, inputs)
            for key, child in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_resolve_query_template(child, inputs) for child in value]
    return copy.deepcopy(value)


def _is_input_reference(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == {"$input"}


def _query_items(value: Any, *, allow_input_references: bool) -> tuple[Any, ...]:
    if allow_input_references and _is_input_reference(value):
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _require_query_property_access(
    query: Any,
    catalog: Sequence[Mapping[str, Any]],
    *,
    allow_input_references: bool,
) -> None:
    """Reject property references outside the caller-filtered semantic catalog."""

    if allow_input_references and _is_input_reference(query):
        return
    if not isinstance(query, Mapping):
        raise SemanticDatasetQueryProviderError("semantic query is invalid")

    def entity_entry(selector: Any) -> Mapping[str, Any] | None:
        if allow_input_references and _is_input_reference(selector):
            return None
        candidates = list(catalog)
        if isinstance(selector, str):
            name = selector.strip()
            candidates = [item for item in candidates if item.get("entity_name") == name]
        elif isinstance(selector, Mapping):
            constrained = False
            for selector_key, catalog_key in (
                ("entity_id", "entity_id"),
                ("entity_name", "entity_name"),
            ):
                value = selector.get(selector_key)
                if allow_input_references and _is_input_reference(value):
                    continue
                if value is not None:
                    constrained = True
                    if not isinstance(value, str):
                        candidates = []
                        break
                    normalized = value.strip()
                    candidates = [
                        item for item in candidates if item.get(catalog_key) == normalized
                    ]
            if not constrained:
                if allow_input_references:
                    return None
                candidates = []
        else:
            candidates = []
        if len(candidates) != 1:
            raise SemanticDatasetQueryProviderError(
                "semantic query references an unavailable entity or property"
            )
        return candidates[0]

    def require_property(entry: Mapping[str, Any] | None, value: Any) -> None:
        if entry is None and allow_input_references:
            return
        if allow_input_references and _is_input_reference(value):
            return
        visible = {
            str(item.get("property_name") or "")
            for item in (entry.get("properties") if entry is not None else ()) or ()
            if isinstance(item, Mapping)
        }
        if not isinstance(value, str) or value.strip() not in visible:
            raise SemanticDatasetQueryProviderError(
                "semantic query references an unavailable entity or property"
            )

    def require_filters(entry: Mapping[str, Any] | None, value: Any) -> None:
        for item in _query_items(
            value,
            allow_input_references=allow_input_references,
        ):
            if isinstance(item, Mapping):
                require_property(entry, item.get("property"))

    base = entity_entry(query.get("base_entity"))
    for name in _query_items(
        query.get("base_properties"),
        allow_input_references=allow_input_references,
    ):
        require_property(base, name)
    require_filters(base, query.get("base_filters"))

    for item in _query_items(
        query.get("related_entities"),
        allow_input_references=allow_input_references,
    ):
        if not isinstance(item, Mapping):
            continue
        related = entity_entry(item)
        for name in _query_items(
            item.get("properties"),
            allow_input_references=allow_input_references,
        ):
            require_property(related, name)
        require_filters(related, item.get("filters"))
        join = item.get("join")
        if isinstance(join, Mapping):
            require_property(base, join.get("base_property"))
            require_property(related, join.get("related_property"))

    for collection_name in ("group_by", "aggregations", "sort"):
        for item in _query_items(
            query.get(collection_name),
            allow_input_references=allow_input_references,
        ):
            if not isinstance(item, Mapping):
                continue
            if collection_name == "sort" and item.get("property") in (None, ""):
                continue
            entry = entity_entry(item)
            property_name = item.get("property")
            if property_name not in (None, ""):
                require_property(entry, property_name)
            if collection_name == "aggregations":
                require_filters(entry, item.get("filters"))


@dataclass(frozen=True, slots=True)
class SemanticDatasetQueryProvider:
    """Compile ontology property requests into bounded DuckDB dataset queries."""

    _db: Session | None = None

    provider_key: ClassVar[str] = "builtin.semantic-dataset-query"
    provider_version: ClassVar[str] = "1.0.0"
    capability_kind: ClassVar[str] = "function"

    def definition_manifest(self) -> Mapping[str, Any]:
        return {
            "capability_kind": self.capability_kind,
            "display_name": "本体对象集查询",
            "description": "基于本次调用提供的受管数据版本执行有界、只读的语义查询。",
            "config_schema": {
                "type": "object",
                "properties": {
                    "semantic_mapping_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1, "maxLength": 64},
                        "minItems": 1,
                        "maxItems": 50,
                        "uniqueItems": True,
                    },
                    "query_template": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["semantic_mapping_ids"],
                "additionalProperties": False,
            },
            "default_config": {"semantic_mapping_ids": []},
            "input_schema": business_query_service.public_query_schema(),
            "output_schema": _query_output_schema(),
            "input_schema_mode": "fixed",
            "output_schema_mode": "fixed",
            "ui_schema": {
                "semantic_mapping_ids": {
                    "control": "semantic_mapping_multiselect",
                    "label": "可查询对象映射",
                    "help": "映射身份进入 Definition 和 Release；运行数据仍由每次调用显式提供。",
                    "placeholder": "选择已激活的语义映射",
                }
            },
            "deprecated": False,
            "migration_message": "",
        }

    def _normalize_provider_config(
        self,
        function: Any,
        provider_config: Any,
        *,
        compatibility_mode: bool,
    ) -> tuple[dict[str, Any], _QueryBinding]:
        if not isinstance(provider_config, Mapping):
            raise SemanticDatasetQueryProviderError(
                "query function Provider config is invalid"
            )
        allowed_fields = {"semantic_mapping_ids", "query_template"}
        extra_fields = set(provider_config) - allowed_fields
        extension: _QueryExtension | None = None
        normalized_extension: dict[str, Any] = {}
        if extra_fields:
            if provider_config.get("query_template") is not None:
                raise SemanticDatasetQueryProviderError(
                    "query_template and Provider compatibility extension are mutually exclusive"
                )
            try:
                compatibility = query_provider_compatibility.resolve_config_extension(
                    provider_key=self.provider_key,
                    provider_version=self.provider_version,
                    extra_fields=extra_fields,
                    provider_config=provider_config,
                    input_schema=getattr(function, "input_schema", None),
                    compatibility_mode=compatibility_mode,
                )
            except ValueError as exc:
                raise SemanticDatasetQueryProviderError(str(exc)) from exc
            if compatibility is None:
                raise SemanticDatasetQueryProviderError(
                    "query function Provider config contains unsupported fields"
                )
            normalized_extension, extension = compatibility
        if "semantic_mapping_ids" not in provider_config:
            raise SemanticDatasetQueryProviderError(
                "query function must pin semantic mappings"
            )
        raw_ids = provider_config.get("semantic_mapping_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 50:
            raise SemanticDatasetQueryProviderError(
                "query function must pin between 1 and 50 semantic mappings"
            )
        mapping_ids = tuple(
            _text(value, "semantic mapping id", maximum=64) for value in raw_ids
        )
        if len(mapping_ids) != len(set(mapping_ids)):
            raise SemanticDatasetQueryProviderError(
                "query function contains duplicate semantic mappings"
            )
        query_template = provider_config.get("query_template")
        if query_template is not None:
            if not isinstance(query_template, Mapping):
                raise SemanticDatasetQueryProviderError(
                    "query_template must be an object"
                )
            input_names = _template_input_names(query_template)
            input_schema = getattr(function, "input_schema", None)
            properties = (
                input_schema.get("properties")
                if isinstance(input_schema, Mapping)
                else None
            )
            if not isinstance(properties, Mapping) or not input_names.issubset(properties):
                raise SemanticDatasetQueryProviderError(
                    "query template inputs must be declared by the function input schema"
                )
            query_template = copy.deepcopy(dict(query_template))
        normalized = {
            "semantic_mapping_ids": list(mapping_ids),
            **({"query_template": query_template} if query_template is not None else {}),
            **normalized_extension,
        }
        return normalized, _QueryBinding(
            mapping_ids=mapping_ids,
            query_template=query_template,
            extension=extension,
        )

    def validate_definition(
        self,
        *,
        input_schema: Mapping[str, Any],
        output_schema: Mapping[str, Any],
        provider_config: Any,
        compatibility_mode: bool = False,
    ) -> Mapping[str, Any]:
        function = SimpleNamespace(input_schema=input_schema, output_schema=output_schema)
        normalized, _binding = self._normalize_provider_config(
            function,
            provider_config,
            compatibility_mode=compatibility_mode,
        )
        return normalized

    def bind_invocation(self, context: Any) -> "SemanticDatasetQueryProvider":
        if not isinstance(context, Session):
            raise SemanticDatasetQueryProviderError(
                "semantic dataset query Provider requires a database session"
            )
        return replace(self, _db=context)

    def _session(self) -> Session:
        if self._db is None:
            raise SemanticDatasetQueryProviderError(
                "semantic dataset query Provider is not bound"
            )
        return self._db

    def _resource(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> tuple[Any, Any, _QueryBinding]:
        if capability.kind != self.capability_kind:
            raise SemanticDatasetQueryProviderError(
                "capability kind does not match semantic dataset query Provider"
            )
        definition = deployment.definition
        try:
            function = runtime_definition_service.resolve_resource(
                definition,
                self.capability_kind,
                capability.resource_id,
            )
        except runtime_definition_service.RuntimeDefinitionError as exc:
            raise SemanticDatasetQueryProviderError(
                "query function is unavailable in the resolved definition"
            ) from exc
        runtime_kind = str(getattr(function, "runtime_kind", "") or "").strip()
        runtime_config = getattr(function, "runtime_config", None)
        if runtime_kind != "provider" or not isinstance(runtime_config, Mapping):
            raise SemanticDatasetQueryProviderError(
                "query function is not bound to a trusted Provider"
            )
        if set(runtime_config) != {
            "provider_key", "provider_version", "provider_config",
        }:
            raise SemanticDatasetQueryProviderError(
                "query function Provider binding is invalid"
            )
        if (
            str(runtime_config.get("provider_key") or "").strip().casefold()
            != self.provider_key
            or str(runtime_config.get("provider_version") or "").strip()
            != self.provider_version
        ):
            raise SemanticDatasetQueryProviderError(
                "query function Provider identity does not match"
            )
        _normalized_config, binding = self._normalize_provider_config(
            function,
            runtime_config.get("provider_config"),
            compatibility_mode=True,
        )
        return definition, function, binding

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        definition, function, binding = self._resource(
            capability,
            deployment,
        )
        catalog = self._semantic_catalog(
            definition=definition,
            deployment=deployment,
            mapping_ids=binding.mapping_ids,
        )
        if binding.query_template is not None:
            _require_query_property_access(
                binding.query_template,
                catalog,
                allow_input_references=True,
            )
        input_schema = (
            copy.deepcopy(dict(function.input_schema))
            if binding.query_template is not None or binding.extension is not None
            else business_query_service.public_query_schema()
        )
        original_description = str(input_schema.get("description") or "")
        self._publish_catalog(input_schema, catalog)
        if binding.query_template is not None or binding.extension is not None:
            input_schema["description"] = original_description or (
                "Typed inputs for a governed read-only object-set query."
            )
        return {
            "input_schema": input_schema,
            "required_roles": [],
            "required_scopes": [],
            "side_effect": False,
            "requires_confirmation": False,
            "idempotency_required": False,
        }

    def _semantic_catalog(
        self,
        *,
        definition: Any,
        deployment: ResolvedDeployment,
        mapping_ids: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        db = self._session()
        by_id = dict(getattr(definition, "semantic_mapping_contracts", {}) or {})
        if set(mapping_ids) - set(by_id):
            raise SemanticDatasetQueryProviderError(
                "one or more frozen semantic mapping contracts are unavailable"
            )

        catalog: list[dict[str, Any]] = []
        used_entities: set[str] = set()
        for mapping_id in mapping_ids:
            mapping = by_id[mapping_id]
            entity_id = str(mapping.get("entity_id") or "")
            entity = definition.entities.get(entity_id)
            if entity is None or entity_id in used_entities:
                raise SemanticDatasetQueryProviderError(
                    "semantic mappings do not resolve to unique active object types"
                )
            mapped_property_ids = {
                str(field.get("ontology_property_id") or "")
                for field in (mapping.get("fields") or [])
                if field.get("direction") in {"input", "bidirectional"}
            }
            properties = [
                prop
                for prop in (getattr(entity, "properties", []) or [])
                if str(prop.id) in mapped_property_ids
                and permission_service.can_read_property(db, prop)
            ]
            if not properties:
                raise SemanticDatasetQueryProviderError(
                    "semantic mapping has no readable mapped properties"
                )
            catalog.append(
                {
                    "entity_id": entity_id,
                    "entity_name": str(entity.name or ""),
                    "entity_api_name": str(getattr(entity, "api_name", "") or ""),
                    "semantic_mapping_id": mapping_id,
                    "properties": [
                        {
                            "property_name": str(prop.name or ""),
                            "api_name": str(getattr(prop, "api_name", "") or ""),
                            "description": str(getattr(prop, "description", "") or ""),
                            "data_type": str(getattr(prop, "data_type", "") or ""),
                            "is_key": bool(getattr(prop, "is_key", False)),
                        }
                        for prop in sorted(properties, key=lambda item: str(item.name))
                    ],
                }
            )
            used_entities.add(entity_id)
        return catalog

    @staticmethod
    def _publish_catalog(
        schema: dict[str, Any],
        catalog: list[dict[str, Any]],
    ) -> None:
        entity_ids = [item["entity_id"] for item in catalog]
        entity_names = [item["entity_name"] for item in catalog]
        property_names = sorted(
            {
                prop["property_name"]
                for item in catalog
                for prop in item["properties"]
            }
        )

        def enrich(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if isinstance(child, dict) and child.get("type") == "string":
                        if key == "entity_id":
                            child["enum"] = entity_ids
                            child["description"] = (
                                "Use an entity_id from x-ontology-catalog."
                            )
                        elif key == "entity_name":
                            child["enum"] = entity_names
                            child["description"] = (
                                "Use an entity_name from x-ontology-catalog."
                            )
                        elif key == "property":
                            child["enum"] = property_names
                            child["description"] = (
                                "Use a property_name published for the selected entity "
                                "in x-ontology-catalog."
                            )
                    if (
                        key in {"base_properties", "properties"}
                        and isinstance(child, dict)
                        and child.get("type") == "array"
                        and isinstance(child.get("items"), dict)
                        and child["items"].get("type") == "string"
                    ):
                        child["items"]["enum"] = property_names
                        child["items"]["description"] = (
                            "Use property_name values published for the selected entity "
                            "in x-ontology-catalog."
                        )
                    enrich(child)
            elif isinstance(value, list):
                for child in value:
                    enrich(child)

        enrich(schema)
        base_entity = schema.get("properties", {}).get("base_entity", {})
        if isinstance(base_entity, dict):
            alternatives = base_entity.get("oneOf")
            if (
                isinstance(alternatives, list)
                and len(alternatives) > 1
                and isinstance(alternatives[1], dict)
            ):
                alternatives[1]["enum"] = entity_names
                alternatives[1]["description"] = (
                    "Entity display-name shorthand from x-ontology-catalog."
                )
        schema["description"] = (
            "Read-only semantic object-set query. Entity and property references must "
            "come from x-ontology-catalog; physical tables, columns and data-source "
            "identifiers are never accepted."
        )
        schema["x-ontology-catalog"] = catalog

    @staticmethod
    def _dataset_handle(data_context: RuntimeDataContext) -> tuple[Any, str]:
        if not isinstance(data_context, RuntimeDataContext):
            raise SemanticDatasetQueryProviderError(
                "semantic dataset query requires a resolved runtime data context"
            )
        handles = [
            item for item in data_context.handles
            if item.binding_kind in {"dataset_head", "dataset_version"}
        ]
        if len(handles) != 1:
            raise SemanticDatasetQueryProviderError(
                "semantic dataset query requires exactly one DatasetVersion input"
            )
        handle = handles[0]
        if handle.binding_kind == "dataset_version":
            version_id = handle.reference_id
            if handle.version_id not in {None, version_id}:
                raise SemanticDatasetQueryProviderError(
                    "resolved DatasetVersion handle is inconsistent"
                )
        else:
            version_id = str(handle.version_id or "").strip()
            if not version_id:
                raise SemanticDatasetQueryProviderError(
                    "resolved DatasetHead handle has no frozen DatasetVersion"
                )
        return handle, version_id

    def _dataset_version(
        self,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> tuple[DatasetVersion, DatasetSchema]:
        db = self._session()
        handle, version_id = self._dataset_handle(data_context)
        version = db.execute(
            select(DatasetVersion).where(
                DatasetVersion.id == version_id,
                DatasetVersion.tenant_id == deployment.tenant_id,
                DatasetVersion.status == "ready",
            )
        ).scalar_one_or_none()
        if version is None or version.dataset_id is None:
            raise SemanticDatasetQueryProviderError(
                "managed DatasetVersion is unavailable"
            )
        if str(version.content_hash or "").strip().lower() != handle.signature:
            raise SemanticDatasetQueryProviderError(
                "managed DatasetVersion signature changed after resolution"
            )
        schema = db.execute(
            select(DatasetSchema)
            .options(
                selectinload(DatasetSchema.relations).selectinload(
                    DatasetRelation.fields
                )
            )
            .where(
                DatasetSchema.id == version.schema_id,
                DatasetSchema.dataset_id == version.dataset_id,
                DatasetSchema.tenant_id == deployment.tenant_id,
            )
        ).scalar_one_or_none()
        if schema is None:
            raise SemanticDatasetQueryProviderError(
                "managed DatasetVersion schema is unavailable"
            )
        return version, schema

    def _semantic_mappings(
        self,
        *,
        definition: Any,
        deployment: ResolvedDeployment,
        mapping_ids: tuple[str, ...],
        runtime_version: DatasetVersion,
        runtime_schema: DatasetSchema,
        source: Any,
        query_args: Mapping[str, Any],
    ) -> tuple[Any, tuple[Any, ...]]:
        db = self._session()
        by_id = dict(getattr(definition, "semantic_mapping_contracts", {}) or {})
        if set(mapping_ids) - set(by_id):
            raise SemanticDatasetQueryProviderError(
                "one or more frozen semantic mapping contracts are unavailable"
            )
        if query_args.get("related_entities"):
            selected_mapping_ids = set(mapping_ids)
            for relation_contract in (
                getattr(definition, "semantic_relation_mapping_contracts", {}) or {}
            ).values():
                endpoints = {
                    str(relation_contract.get("source_semantic_mapping_id") or ""),
                    str(relation_contract.get("target_semantic_mapping_id") or ""),
                }
                if endpoints <= selected_mapping_ids:
                    raise SemanticDatasetQueryProviderError(
                        "semantic relation mapping execution is not supported; "
                        "the frozen contract was not ignored"
                    )
        ordered_runtime_relations = sorted(
            tuple(runtime_schema.relations or ()),
            key=lambda item: (int(item.ordinal or 0), str(item.id or "")),
        )
        version_manifest = (
            runtime_version.manifest
            if isinstance(runtime_version.manifest, Mapping)
            else {}
        )
        relation_row_counts = version_manifest.get("relations")
        if not isinstance(relation_row_counts, Mapping):
            relation_row_counts = None
        try:
            observed_profile = input_contract_validator.build_observed_tabular_profile(
                ordered_runtime_relations,
                record_count=int(runtime_version.record_count or 0),
                relation_row_counts=relation_row_counts,
            )
        except input_contract_validator.InputContractError as exc:
            raise SemanticDatasetQueryProviderError(exc.message) from exc
        synthetic: dict[str, Any] = {}
        used_entities: set[str] = set()
        for mapping_id in mapping_ids:
            mapping = by_id[mapping_id]
            entity_id = str(mapping.get("entity_id") or "")
            entity = definition.entities.get(entity_id)
            if entity is None or entity_id in used_entities:
                raise SemanticDatasetQueryProviderError(
                    "semantic mappings do not resolve to unique active object types"
                )
            schema_document = mapping.get("schema_document")
            if not isinstance(schema_document, Mapping):
                raise SemanticDatasetQueryProviderError(
                    "frozen semantic mapping contract is invalid"
                )
            try:
                profile_match = input_contract_validator.validate_profile(
                    schema_document,
                    observed_profile,
                )
            except input_contract_validator.InputContractError as exc:
                raise SemanticDatasetQueryProviderError(
                    "runtime DatasetVersion does not satisfy a semantic mapping contract: "
                    + exc.message
                ) from exc
            if len(profile_match.relation_matches) != 1:
                raise SemanticDatasetQueryProviderError(
                    "semantic mapping contract did not resolve one logical relation"
                )
            relation_index = profile_match.relation_matches[0]
            if relation_index >= len(ordered_runtime_relations):
                raise SemanticDatasetQueryProviderError(
                    "semantic mapping relation resolution is invalid"
                )
            runtime_relation = ordered_runtime_relations[relation_index]
            runtime_fields: dict[str, Any] = {}
            for field in runtime_relation.fields or ():
                comparison_name = input_contract_validator.normalize_content_name(
                    field.source_name or field.field_key
                )
                if not comparison_name or comparison_name in runtime_fields:
                    raise SemanticDatasetQueryProviderError(
                        "runtime DatasetVersion contains ambiguous field names"
                    )
                runtime_fields[comparison_name] = field
            raw_contract = schema_document.get(
                input_contract_validator.CONTENT_CONTRACT_KEY
            )
            raw_relations = (
                raw_contract.get("relations")
                if isinstance(raw_contract, Mapping)
                else None
            )
            raw_contract_fields = (
                raw_relations[0].get("fields")
                if isinstance(raw_relations, Sequence)
                and len(raw_relations) == 1
                and isinstance(raw_relations[0], Mapping)
                else None
            )
            if not isinstance(raw_contract_fields, Sequence):
                raise SemanticDatasetQueryProviderError(
                    "frozen semantic mapping field contract is invalid"
                )
            all_entity_properties = {
                str(prop.id): prop for prop in (getattr(entity, "properties", []) or [])
            }
            visible_property_ids = {
                property_id
                for property_id, prop in all_entity_properties.items()
                if permission_service.can_read_property(db, prop)
            }
            column_map: dict[str, str] = {}
            mapped_property_ids: set[str] = set()
            for field_mapping in mapping.get("fields") or ():
                if field_mapping.get("direction") not in {"input", "bidirectional"}:
                    continue
                if field_mapping.get("transform"):
                    raise SemanticDatasetQueryProviderError(
                        "semantic dataset query does not support this field transform"
                    )
                property_id = str(field_mapping.get("ontology_property_id") or "")
                field_index = field_mapping.get("contract_field_index")
                if (
                    isinstance(field_index, bool)
                    or not isinstance(field_index, int)
                    or field_index < 0
                    or field_index >= len(raw_contract_fields)
                ):
                    raise SemanticDatasetQueryProviderError(
                        "frozen semantic mapping field index is invalid"
                    )
                raw_field_contract = raw_contract_fields[field_index]
                if not isinstance(raw_field_contract, Mapping):
                    raise SemanticDatasetQueryProviderError(
                        "frozen semantic mapping field contract is invalid"
                    )
                raw_aliases = raw_field_contract.get("aliases") or ()
                if not isinstance(raw_aliases, Sequence) or isinstance(
                    raw_aliases, (str, bytes, bytearray)
                ):
                    raise SemanticDatasetQueryProviderError(
                        "frozen semantic mapping aliases are invalid"
                    )
                accepted_names = {
                    input_contract_validator.normalize_content_name(value)
                    for value in (raw_field_contract.get("name"), *raw_aliases)
                    if input_contract_validator.normalize_content_name(value)
                }
                candidates = [
                    runtime_fields[name]
                    for name in accepted_names
                    if name in runtime_fields
                ]
                prop = all_entity_properties.get(property_id)
                if prop is None or len(candidates) != 1:
                    raise SemanticDatasetQueryProviderError(
                        "semantic field mapping no longer matches the object or schema"
                    )
                mapped_property_ids.add(property_id)
                if property_id not in visible_property_ids:
                    continue
                runtime_field = candidates[0]
                property_name = str(prop.name or "").strip()
                source_name = str(runtime_field.source_name or "").strip()
                if not property_name or not source_name or property_name in column_map:
                    raise SemanticDatasetQueryProviderError(
                        "semantic field mapping is incomplete or ambiguous"
                    )
                column_map[property_name] = source_name
            key_names = {
                property_id
                for property_id, prop in all_entity_properties.items()
                if bool(getattr(prop, "is_key", False))
            }
            if not key_names or not key_names.intersection(mapped_property_ids):
                raise SemanticDatasetQueryProviderError(
                    "semantic mapping must include the object type primary key"
                )
            if not column_map:
                raise SemanticDatasetQueryProviderError(
                    "semantic mapping has no readable mapped properties"
                )
            synthetic_mapping = SimpleNamespace(
                id=mapping_id,
                scenario_id=deployment.scenario_id,
                entity_id=entity_id,
                data_source_id=source.id,
                definition_data_source_id=source.id,
                table_name=runtime_relation.relation_key,
                column_map=column_map,
                transform_rules={},
                status="ok",
                entity=entity,
            )
            synthetic[mapping_id] = synthetic_mapping
            used_entities.add(entity_id)
        return replace(definition, mappings=synthetic, relation_mappings={}), tuple(
            synthetic.values()
        )

    def invoke(
        self,
        request: Request,
        actor: Actor,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> Mapping[str, Any]:
        db = self._session()
        definition, function, binding = self._resource(
            request.capability,
            deployment,
        )
        require_actor_session(db, actor)
        capability_readiness_service.require_executable(
            self.capability_kind,
            function,
            definition=definition,
            db=db,
        )
        permission_service.require_scenario_permission(
            db,
            definition.scenario,
            "read",
            message="semantic dataset query is not permitted",
        )
        catalog = self._semantic_catalog(
            definition=definition,
            deployment=deployment,
            mapping_ids=binding.mapping_ids,
        )
        extension_context: Any = None
        query_template = binding.query_template
        if binding.extension is not None:
            prepared = binding.extension.prepare(
                definition,
                function,
                request.inputs,
            )
            if prepared.terminal_output is not None:
                return prepared.terminal_output
            query_template = prepared.query_template
            extension_context = prepared.context
        query_args = (
            _resolve_query_template(query_template, request.inputs)
            if query_template is not None
            else dict(request.inputs)
        )
        _require_query_property_access(
            query_args,
            catalog,
            allow_input_references=False,
        )
        version, runtime_schema = self._dataset_version(deployment, data_context)
        source = SimpleNamespace(
            id=f"dataset-version:{version.id}",
            tenant_id=deployment.tenant_id,
            scenario_id=deployment.scenario_id,
            name="Managed DatasetVersion",
            type="dataset",
            connector_revision=0,
            config={
                "dataset_id": version.dataset_id,
                "dataset_version_id": version.id,
            },
        )
        query_definition, mappings = self._semantic_mappings(
            definition=definition,
            deployment=deployment,
            mapping_ids=binding.mapping_ids,
            runtime_version=version,
            runtime_schema=runtime_schema,
            source=source,
            query_args=query_args,
        )
        result = business_query_service.query_business_data(
            db,
            definition=query_definition,
            mappings=mappings,
            data_sources=(source,),
            args=query_args,
        )
        output = {
            "records": result["records"],
            "columns": result["columns"],
            "row_count": result["row_count"],
            "truncated": result["truncated"],
            "offset": result["offset"],
            "next_offset": result["next_offset"],
        }
        if binding.extension is not None:
            return binding.extension.finalize(extension_context, output)
        return output


__all__ = [
    "_PreparedQueryExtension",
    "_QueryBinding",
    "_query_output_schema",
    "SemanticDatasetQueryProvider",
    "SemanticDatasetQueryProviderError",
]
