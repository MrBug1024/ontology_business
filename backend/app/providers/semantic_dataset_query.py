"""Read-only semantic object-set queries over an explicit DatasetVersion."""
from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import (
    DatasetRelation,
    DatasetSchema,
    DatasetVersion,
    SemanticFieldMapping,
    SemanticMapping,
)
from ..services import (
    business_query_service,
    capability_readiness_service,
    permission_service,
    runtime_definition_service,
    semantic_audit_rule_service,
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


@dataclass(frozen=True, slots=True)
class SemanticDatasetQueryProvider:
    """Compile ontology property requests into bounded DuckDB dataset queries."""

    _db: Session | None = None

    provider_key: ClassVar[str] = "builtin.semantic-dataset-query"
    provider_version: ClassVar[str] = "1.0.0"
    capability_kind: ClassVar[str] = "function"

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
    ) -> tuple[
        Any,
        Any,
        tuple[str, ...],
        Mapping[str, Any] | None,
        Mapping[str, Any] | None,
    ]:
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
        provider_config = runtime_config.get("provider_config")
        if (
            not isinstance(provider_config, Mapping)
            or "semantic_mapping_ids" not in provider_config
            or set(provider_config)
            - {"semantic_mapping_ids", "query_template", "rule_query"}
        ):
            raise SemanticDatasetQueryProviderError(
                "query function Provider config is invalid"
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
        rule_query = provider_config.get("rule_query")
        if query_template is not None and rule_query is not None:
            raise SemanticDatasetQueryProviderError(
                "query_template and rule_query are mutually exclusive"
            )
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
        if rule_query is not None:
            if not isinstance(rule_query, Mapping) or set(rule_query) != {
                "selector_input",
                "spec_version",
            }:
                raise SemanticDatasetQueryProviderError(
                    "rule_query must declare only selector_input and spec_version"
                )
            if (
                str(rule_query.get("spec_version") or "")
                != semantic_audit_rule_service.SPEC_VERSION
            ):
                raise SemanticDatasetQueryProviderError(
                    "rule_query semantic audit version is unsupported"
                )
            selector_input = _text(
                rule_query.get("selector_input"),
                "audit rule selector input",
                maximum=100,
            )
            input_schema = getattr(function, "input_schema", None)
            properties = (
                input_schema.get("properties")
                if isinstance(input_schema, Mapping)
                else None
            )
            selector_schema = (
                properties.get(selector_input) if isinstance(properties, Mapping) else None
            )
            if not isinstance(selector_schema, Mapping) or selector_schema.get("type") != "string":
                raise SemanticDatasetQueryProviderError(
                    "rule_query selector must be a declared string input"
                )
            rule_query = {
                "selector_input": selector_input,
                "spec_version": semantic_audit_rule_service.SPEC_VERSION,
            }
        return definition, function, mapping_ids, query_template, rule_query

    def contract(
        self,
        capability: CapabilityRef,
        deployment: ResolvedDeployment,
    ) -> Mapping[str, Any]:
        definition, function, mapping_ids, query_template, rule_query = self._resource(
            capability,
            deployment,
        )
        catalog = self._semantic_catalog(
            definition=definition,
            deployment=deployment,
            mapping_ids=mapping_ids,
        )
        input_schema = (
            copy.deepcopy(dict(function.input_schema))
            if query_template is not None or rule_query is not None
            else business_query_service.public_query_schema()
        )
        original_description = str(input_schema.get("description") or "")
        self._publish_catalog(input_schema, catalog)
        if query_template is not None or rule_query is not None:
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
        rows = list(
            db.scalars(
                select(SemanticMapping)
                .options(
                    selectinload(SemanticMapping.field_mappings).selectinload(
                        SemanticFieldMapping.ontology_property
                    )
                )
                .where(
                    SemanticMapping.id.in_(mapping_ids),
                    SemanticMapping.scenario_id == deployment.scenario_id,
                    SemanticMapping.tenant_id == deployment.tenant_id,
                    SemanticMapping.status == "active",
                )
            ).all()
        )
        by_id = {row.id: row for row in rows}
        if set(by_id) != set(mapping_ids):
            raise SemanticDatasetQueryProviderError(
                "one or more pinned semantic mappings are unavailable"
            )

        catalog: list[dict[str, Any]] = []
        used_entities: set[str] = set()
        for mapping_id in mapping_ids:
            mapping = by_id[mapping_id]
            entity_id = str(mapping.entity_id)
            entity = definition.entities.get(entity_id)
            if entity is None or entity_id in used_entities:
                raise SemanticDatasetQueryProviderError(
                    "semantic mappings do not resolve to unique active object types"
                )
            mapped_property_ids = {
                str(field.ontology_property_id)
                for field in mapping.field_mappings
                if field.direction in {"input", "bidirectional"}
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
    def _dataset_handle(data_context: RuntimeDataContext) -> Any:
        if not isinstance(data_context, RuntimeDataContext):
            raise SemanticDatasetQueryProviderError(
                "semantic dataset query requires a resolved runtime data context"
            )
        handles = [
            item for item in data_context.handles
            if item.binding_kind == "dataset_version"
        ]
        if len(handles) != 1 or len(data_context.handles) != 1:
            raise SemanticDatasetQueryProviderError(
                "semantic dataset query requires exactly one DatasetVersion input"
            )
        return handles[0]

    def _dataset_version(
        self,
        deployment: ResolvedDeployment,
        data_context: RuntimeDataContext,
    ) -> tuple[DatasetVersion, DatasetSchema]:
        db = self._session()
        handle = self._dataset_handle(data_context)
        version = db.execute(
            select(DatasetVersion).where(
                DatasetVersion.id == handle.reference_id,
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
        runtime_schema: DatasetSchema,
        source: Any,
    ) -> tuple[Any, tuple[Any, ...]]:
        db = self._session()
        rows = list(
            db.scalars(
                select(SemanticMapping)
                .options(
                    selectinload(SemanticMapping.dataset_schema),
                    selectinload(SemanticMapping.dataset_relation),
                    selectinload(SemanticMapping.field_mappings).selectinload(
                        SemanticFieldMapping.ontology_property
                    ),
                    selectinload(SemanticMapping.field_mappings).selectinload(
                        SemanticFieldMapping.dataset_field
                    ),
                )
                .where(
                    SemanticMapping.id.in_(mapping_ids),
                    SemanticMapping.scenario_id == deployment.scenario_id,
                    SemanticMapping.tenant_id == deployment.tenant_id,
                    SemanticMapping.status == "active",
                )
            ).all()
        )
        by_id = {row.id: row for row in rows}
        if set(by_id) != set(mapping_ids):
            raise SemanticDatasetQueryProviderError(
                "one or more pinned semantic mappings are unavailable"
            )
        runtime_relations = {
            str(relation.relation_key): relation
            for relation in runtime_schema.relations
        }
        synthetic: dict[str, Any] = {}
        used_entities: set[str] = set()
        for mapping_id in mapping_ids:
            mapping = by_id[mapping_id]
            authored_schema = mapping.dataset_schema
            if (
                authored_schema is None
                or str(authored_schema.schema_hash or "").strip().lower()
                != str(runtime_schema.schema_hash or "").strip().lower()
            ):
                raise SemanticDatasetQueryProviderError(
                    "runtime DatasetVersion does not satisfy a semantic mapping schema"
                )
            entity = definition.entities.get(str(mapping.entity_id))
            if entity is None or str(mapping.entity_id) in used_entities:
                raise SemanticDatasetQueryProviderError(
                    "semantic mappings do not resolve to unique active object types"
                )
            relation_key = str(mapping.dataset_relation.relation_key or "")
            runtime_relation = runtime_relations.get(relation_key)
            if runtime_relation is None:
                raise SemanticDatasetQueryProviderError(
                    "runtime DatasetVersion is missing a mapped logical relation"
                )
            runtime_fields = {
                str(field.field_key): field for field in runtime_relation.fields
            }
            entity_properties = {
                str(prop.id): prop for prop in (getattr(entity, "properties", []) or [])
            }
            column_map: dict[str, str] = {}
            for field_mapping in mapping.field_mappings:
                if field_mapping.direction not in {"input", "bidirectional"}:
                    continue
                if field_mapping.transform:
                    raise SemanticDatasetQueryProviderError(
                        "semantic dataset query does not support this field transform"
                    )
                prop = entity_properties.get(str(field_mapping.ontology_property_id))
                authored_field = field_mapping.dataset_field
                runtime_field = runtime_fields.get(str(authored_field.field_key or ""))
                if prop is None or runtime_field is None:
                    raise SemanticDatasetQueryProviderError(
                        "semantic field mapping no longer matches the object or schema"
                    )
                property_name = str(prop.name or "").strip()
                source_name = str(runtime_field.source_name or "").strip()
                if not property_name or not source_name or property_name in column_map:
                    raise SemanticDatasetQueryProviderError(
                        "semantic field mapping is incomplete or ambiguous"
                    )
                column_map[property_name] = source_name
            key_names = {
                str(prop.name or "").strip()
                for prop in entity_properties.values()
                if bool(getattr(prop, "is_key", False))
            }
            if not key_names or not key_names.intersection(column_map):
                raise SemanticDatasetQueryProviderError(
                    "semantic mapping must include the object type primary key"
                )
            synthetic_mapping = SimpleNamespace(
                id=mapping.id,
                scenario_id=deployment.scenario_id,
                entity_id=mapping.entity_id,
                data_source_id=source.id,
                definition_data_source_id=source.id,
                table_name=runtime_relation.relation_key,
                column_map=column_map,
                transform_rules={},
                status="ok",
                entity=entity,
            )
            synthetic[mapping.id] = synthetic_mapping
            used_entities.add(str(mapping.entity_id))
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
        definition, function, mapping_ids, query_template, rule_query = self._resource(
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
        audit_rule: Any | None = None
        audit_spec: Mapping[str, Any] | None = None
        if rule_query is not None:
            selector_input = str(rule_query["selector_input"])
            try:
                audit_rule, audit_spec = semantic_audit_rule_service.resolve_rule(
                    definition,
                    request.inputs.get(selector_input),
                )
            except semantic_audit_rule_service.SemanticAuditRuleError as exc:
                raise SemanticDatasetQueryProviderError(str(exc)) from exc
            query_template = audit_spec.get("query_template")
            if query_template is not None:
                try:
                    input_names = semantic_audit_rule_service.template_input_names(
                        query_template
                    )
                except semantic_audit_rule_service.SemanticAuditRuleError as exc:
                    raise SemanticDatasetQueryProviderError(str(exc)) from exc
                input_schema = getattr(function, "input_schema", None)
                properties = (
                    input_schema.get("properties")
                    if isinstance(input_schema, Mapping)
                    else None
                )
                if not isinstance(properties, Mapping) or not input_names.issubset(properties):
                    raise SemanticDatasetQueryProviderError(
                        "audit rule query inputs must be declared by the function input schema"
                    )
            else:
                mode = str(audit_spec["assessment_mode"])
                state = (
                    "manual_review_required"
                    if mode == "manual"
                    else "additional_evidence_required"
                )
                return self._audit_result(
                    audit_rule,
                    audit_spec,
                    state=state,
                    result=None,
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
            mapping_ids=mapping_ids,
            runtime_schema=runtime_schema,
            source=source,
        )
        query_args = (
            _resolve_query_template(query_template, request.inputs)
            if query_template is not None
            else dict(request.inputs)
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
        if audit_rule is None or audit_spec is None:
            return output
        mode = str(audit_spec["assessment_mode"])
        if result["row_count"]:
            state = (
                "candidate_detected"
                if mode == "automatic"
                else "candidate_detected_pending_review"
            )
        else:
            state = "no_candidate_detected"
        return self._audit_result(
            audit_rule,
            audit_spec,
            state=state,
            result=output,
        )

    @staticmethod
    def _audit_result(
        rule: Any,
        spec: Mapping[str, Any],
        *,
        state: str,
        result: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        query_result = dict(
            result
            or {
                "records": [],
                "columns": [],
                "row_count": 0,
                "truncated": False,
                "offset": 0,
                "next_offset": None,
            }
        )
        return {
            "audit_rule": {
                "id": str(getattr(rule, "id", "")),
                "name": str(getattr(rule, "name", "")),
                "code": str(spec["rule_code"]),
                "domain": str(spec.get("domain") or ""),
                "issue_type": str(spec.get("issue_type") or ""),
                "assessment_mode": str(spec["assessment_mode"]),
            },
            "decision_state": state,
            "basis": str(spec.get("basis") or ""),
            "required_evidence": list(spec.get("required_evidence") or []),
            **query_result,
        }


__all__ = [
    "SemanticDatasetQueryProvider",
    "SemanticDatasetQueryProviderError",
]
