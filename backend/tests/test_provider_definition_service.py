from __future__ import annotations

import pytest

from app.providers.semantic_audit import SemanticAuditProvider
from app.providers.semantic_dataset_query import SemanticDatasetQueryProvider
from app.services import provider_definition_service, release_service
from app.services.capability_registry import CapabilityProviderRegistry


def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


class _DefinitionProvider:
    provider_key = "trusted.definition-provider"
    provider_version = "1.2.3"

    def contract(self, capability, deployment):
        return {"input_schema": _schema()}

    def invoke(self, request, actor, deployment, data_context):
        return {}

    def definition_manifest(self):
        return {
            "capability_kind": "function",
            "display_name": "Definition Provider",
            "description": "Test-only trusted definition provider.",
            "config_schema": _schema(
                {"mode": {"type": "string", "enum": ["safe"]}},
                ["mode"],
            ),
            "default_config": {"mode": "safe"},
            "input_schema": _schema(),
            "output_schema": _schema(),
            "ui_schema": {},
        }

    def validate_definition(
        self,
        *,
        input_schema,
        output_schema,
        provider_config,
        compatibility_mode=False,
    ):
        if provider_config != {"mode": "safe"}:
            raise ValueError("mode is not supported")
        return {"mode": "safe"}


def _registry() -> CapabilityProviderRegistry:
    registry = CapabilityProviderRegistry()
    registry.register_instance(_DefinitionProvider())
    registry.seal()
    return registry


def _declaration(*, version: str = "1.2.3", config: dict | None = None) -> dict:
    return {
        "name": "Versioned provider function",
        "input_schema": _schema(),
        "output_schema": _schema(),
        "runtime_kind": "provider",
        "runtime_config": {
            "provider_key": _DefinitionProvider.provider_key,
            "provider_version": version,
            "provider_config": config if config is not None else {"mode": "safe"},
        },
    }


def test_definition_validation_resolves_exact_identity_and_provider_owned_config() -> None:
    registry = _registry()

    validated = provider_definition_service.validate_function_definition(
        _declaration(),
        registry=registry,
    )
    assert validated["runtime_config"]["provider_config"] == {"mode": "safe"}

    with pytest.raises(
        provider_definition_service.ProviderDefinitionError,
        match="未注册|not registered",
    ):
        provider_definition_service.validate_function_definition(
            _declaration(version="9.9.9"),
            registry=registry,
        )
    with pytest.raises(
        provider_definition_service.ProviderDefinitionError,
        match="mode is not supported",
    ):
        provider_definition_service.validate_function_definition(
            _declaration(config={"mode": "unsafe"}),
            registry=registry,
        )


def test_manifest_catalog_is_exact_versioned_and_safe_for_generic_ui() -> None:
    manifests = provider_definition_service.list_function_provider_manifests(
        registry=_registry()
    )

    assert len(manifests) == 1
    assert manifests[0]["provider_key"] == _DefinitionProvider.provider_key
    assert manifests[0]["provider_version"] == _DefinitionProvider.provider_version
    assert manifests[0]["config_schema"]["additionalProperties"] is False
    assert "python_path" not in str(manifests[0])


def test_snapshot_normalization_fails_before_publish_for_unknown_provider() -> None:
    content = {
        "scenario": {"name": "Generic", "namespace": "default"},
        "entities": [],
        "relations": [],
        "mappings": [],
        "functions": [
            {
                "id": "function-unknown-provider",
                **_declaration(version="9.9.9"),
            }
        ],
        "actions": [],
        "rules": [],
        "events": [],
        "workflows": [],
    }

    with pytest.raises(release_service.ReleaseValidationError, match="Provider"):
        release_service.normalize_snapshot_content(content)


def test_new_audit_provider_and_legacy_interpreter_have_distinct_exact_identities() -> None:
    manifests = provider_definition_service.list_function_provider_manifests()
    identities = {
        (item["provider_key"], item["provider_version"]): item
        for item in manifests
    }

    assert (
        SemanticDatasetQueryProvider.provider_key,
        SemanticDatasetQueryProvider.provider_version,
    ) in identities
    assert (
        SemanticAuditProvider.provider_key,
        SemanticAuditProvider.provider_version,
    ) in identities
    assert identities[
        (SemanticDatasetQueryProvider.provider_key, SemanticDatasetQueryProvider.provider_version)
    ]["deprecated"] is False


def test_semantic_dataset_provider_rejects_new_audit_config_but_keeps_explicit_legacy_mode() -> None:
    declaration = {
        "name": "Legacy frozen capability",
        "input_schema": _schema({"selector": {"type": "string"}}, ["selector"]),
        "output_schema": _schema(),
        "runtime_kind": "provider",
        "runtime_config": {
            "provider_key": SemanticDatasetQueryProvider.provider_key,
            "provider_version": SemanticDatasetQueryProvider.provider_version,
            "provider_config": {
                "semantic_mapping_ids": ["mapping-a"],
                "rule_query": {
                    "selector_input": "selector",
                    "spec_version": "semantic-audit/v1",
                },
            },
        },
    }

    with pytest.raises(
        provider_definition_service.ProviderDefinitionError,
        match="legacy|迁移",
    ):
        provider_definition_service.validate_function_definition(declaration)

    validated = provider_definition_service.validate_function_definition(
        declaration,
        compatibility_mode=True,
    )
    assert validated["runtime_config"]["provider_config"]["rule_query"][
        "spec_version"
    ] == "semantic-audit/v1"
