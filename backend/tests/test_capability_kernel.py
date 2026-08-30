from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace

from app.services.capability_contracts import (
    Actor,
    BindingOverride,
    CapabilityContractError,
    CapabilityRef,
    DataPort,
    Receipt,
    Request,
    ResolvedDataHandle,
    RuntimeDataContext,
    canonical_hash,
)
from app.services.capability_registry import (
    CapabilityProviderRegistry,
    CapabilityRegistryError,
    default_provider_registry,
)
from app.services.capability_provider_keys import BUILTIN_PROVIDER_KEYS
from app.services.deployment_service import (
    DeploymentResolutionError,
    build_resolved_deployment,
    require_request_matches_deployment,
    resolve_runtime_data_context,
)


DEFINITION_HASH = "a" * 64
OTHER_DEFINITION_HASH = "b" * 64
BINDING_SIGNATURE_A = "1" * 64
BINDING_SIGNATURE_B = "2" * 64


def definition(
    *,
    definition_hash: str = DEFINITION_HASH,
    source: str = "release",
) -> SimpleNamespace:
    scenario = SimpleNamespace(id="scenario-kernel", tenant_id="tenant-kernel")
    return SimpleNamespace(
        scenario=scenario,
        environment="staging",
        source=source,
        snapshot_id="snapshot-kernel" if source == "release" else None,
        release_id="release-kernel" if source == "release" else None,
        definition_hash=definition_hash,
    )


def port(
    key: str = "primary-input",
    *,
    required: bool = True,
    allow_override: bool = True,
) -> DataPort:
    return DataPort(
        key=key,
        modality="tabular",
        schema={"type": "object", "properties": {"record": {"type": "string"}}},
        required=required,
        binding_kinds=("managed-dataset",),
        override_policy="managed-reference" if allow_override else "forbidden",
    )


def binding(
    key: str = "primary-input",
    *,
    reference_id: str = "managed-reference-a",
    signature: str = BINDING_SIGNATURE_A,
    password: str = "must-never-enter-a-fingerprint",
) -> SimpleNamespace:
    # ``config`` is deliberately present to prove the resolver never reads or
    # serializes it.  Only connector_signature may enter deployment identity.
    return SimpleNamespace(
        binding_key=key,
        connector_kind="managed-dataset",
        connector_id=reference_id,
        connector_signature=signature,
        config={"password": password},
    )


class _Provider:
    provider_key = "trusted.provider"
    provider_version = "1.0.0"

    def contract(self, capability, deployment):
        return {"capability": capability.public_name, "definition": deployment.definition_hash}

    def invoke(self, request, actor, deployment):
        return {
            "principal": actor.principal_id,
            "capability": request.capability.public_name,
            "deployment": deployment.fingerprint,
        }


class CapabilityContractTests(unittest.TestCase):
    def test_canonical_hash_is_order_stable_and_domain_separated(self) -> None:
        left = {"items": {"b", "a"}, "nested": {"z": 2, "a": 1}}
        right = {"nested": {"a": 1, "z": 2}, "items": {"a", "b"}}
        self.assertEqual(canonical_hash(left), canonical_hash(right))
        self.assertNotEqual(
            canonical_hash(left, domain="contract-v1"),
            canonical_hash(left, domain="contract-v2"),
        )
        with self.assertRaises(CapabilityContractError):
            canonical_hash({"invalid": float("nan")})
        with self.assertRaises(CapabilityContractError):
            canonical_hash({"unsafe": object()})

    def test_nested_contract_values_are_immutable_copies(self) -> None:
        raw_schema = {"properties": {"record": {"type": "string"}}}
        data_port = DataPort(
            key="payload",
            modality="document",
            schema=raw_schema,
            override_policy="managed-reference",
        )
        raw_schema["properties"]["record"]["type"] = "integer"
        self.assertEqual(
            data_port.schema["properties"]["record"]["type"],
            "string",
        )
        with self.assertRaises(TypeError):
            data_port.schema["new"] = "value"
        with self.assertRaises(TypeError):
            data_port.schema["properties"]["record"]["type"] = "number"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            data_port.required = False

        actor = Actor(
            actor_type="service",
            principal_id="principal-kernel",
            tenant_id="tenant-kernel",
            roles=("operator", "operator"),
            scopes=("capabilities:invoke", "capabilities:read"),
        )
        self.assertEqual(actor.roles, ("operator",))
        self.assertEqual(
            actor.scopes,
            ("capabilities:invoke", "capabilities:read"),
        )

    def test_request_rejects_duplicate_binding_overrides(self) -> None:
        capability = CapabilityRef(kind="provider", resource_id="capability-a")
        first = BindingOverride(
            port_key="payload",
            binding_kind="managed-dataset",
            reference_id="reference-a",
            signature=BINDING_SIGNATURE_A,
        )
        second = BindingOverride(
            port_key="payload",
            binding_kind="managed-dataset",
            reference_id="reference-b",
            signature=BINDING_SIGNATURE_B,
        )
        with self.assertRaises(CapabilityContractError):
            Request(
                capability=capability,
                inputs={},
                binding_overrides=(first, second),
            )

    def test_managed_override_uses_one_typed_selector_and_optional_expected_signature(self) -> None:
        by_id = BindingOverride(
            port_key="payload",
            binding_kind="dataset_version",
            reference_id="version-a",
        )
        self.assertEqual(by_id.selector, "reference_id")
        self.assertEqual(by_id.selector_value, "version-a")
        self.assertIsNone(by_id.signature)

        by_key = BindingOverride(
            port_key="live-reference",
            binding_kind="connector_binding",
            binding_key="current-system",
        )
        self.assertEqual(by_key.selector, "binding_key")
        self.assertEqual(by_key.selector_value, "current-system")

        with self.assertRaises(CapabilityContractError):
            BindingOverride(
                port_key="payload",
                binding_kind="dataset_version",
            )
        with self.assertRaises(CapabilityContractError):
            BindingOverride(
                port_key="payload",
                binding_kind="dataset_version",
                reference_id="version-a",
                binding_key="also-a-key",
            )
        with self.assertRaises(CapabilityContractError):
            BindingOverride(
                port_key="payload",
                binding_kind="dataset_version",
                binding_key="connector-only-selector",
            )

    def test_receipt_is_deeply_immutable(self) -> None:
        capability = CapabilityRef(kind="function", resource_id="function-a")
        receipt = Receipt(
            invocation_id="invocation-a",
            status="succeeded",
            capability=capability,
            definition_hash=DEFINITION_HASH,
            deployment_fingerprint="c" * 64,
            data_context_fingerprint="d" * 64,
            output={"records": [{"id": "record-a"}]},
            audit_ref={"kind": "function-run", "id": "run-a"},
        )
        with self.assertRaises(TypeError):
            receipt.output["records"][0]["id"] = "changed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            receipt.status = "failed"


class CapabilityProviderRegistryTests(unittest.TestCase):
    def test_default_registry_is_sealed_with_exact_builtin_identities(self) -> None:
        self.assertTrue(default_provider_registry.sealed)
        for provider_key in BUILTIN_PROVIDER_KEYS.values():
            self.assertIn(
                (provider_key, "1.0.0"),
                default_provider_registry.identities(),
            )
            provider = default_provider_registry.resolve(provider_key, "1.0.0")
            self.assertEqual(provider.provider_version, "1.0.0")
        with self.assertRaises(CapabilityRegistryError):
            default_provider_registry.register_instance(_Provider())

    def test_explicit_instance_and_factory_are_validated_and_cached(self) -> None:
        registry = CapabilityProviderRegistry()
        provider = registry.register_instance(_Provider())
        self.assertIs(registry.resolve("trusted.provider"), provider)

        calls: list[int] = []

        class FactoryProvider(_Provider):
            provider_key = "factory.provider"

        def factory():
            calls.append(1)
            return FactoryProvider()

        registry.register_factory(
            "factory.provider",
            factory,
            provider_version="1.0.0",
        )
        first = registry.resolve("factory.provider")
        second = registry.resolve("factory.provider")
        self.assertIs(first, second)
        self.assertEqual(calls, [1])
        self.assertEqual(registry.keys(), ("factory.provider", "trusted.provider"))

    def test_same_key_versions_require_exact_or_unambiguous_resolution(self) -> None:
        class VersionedProvider(_Provider):
            def __init__(self, version: str) -> None:
                self.provider_version = version

        registry = CapabilityProviderRegistry()
        first = registry.register_instance(VersionedProvider("1.0.0"))
        second = registry.register_instance(VersionedProvider("2.0.0"))

        self.assertIs(registry.resolve("trusted.provider", "1.0.0"), first)
        self.assertIs(registry.resolve("trusted.provider", "2.0.0"), second)
        self.assertEqual(registry.keys(), ("trusted.provider",))
        self.assertEqual(
            registry.identities(),
            (
                ("trusted.provider", "1.0.0"),
                ("trusted.provider", "2.0.0"),
            ),
        )
        with self.assertRaises(CapabilityRegistryError):
            registry.resolve("trusted.provider")
        with self.assertRaises(CapabilityRegistryError):
            registry.resolve("trusted.provider", "9.9.9")

    def test_factory_version_is_explicit_or_declared_on_the_factory(self) -> None:
        registry = CapabilityProviderRegistry()

        class DeclaredFactoryProvider(_Provider):
            provider_key = "declared.factory"

        registry.register_factory("declared.factory", DeclaredFactoryProvider)
        self.assertEqual(
            registry.resolve("declared.factory").provider_version,
            "1.0.0",
        )

        def undeclared_factory():
            return _Provider()

        with self.assertRaisesRegex(
            CapabilityRegistryError,
            "factory version must be declared",
        ):
            registry.register_factory("undeclared.factory", undeclared_factory)

    def test_registry_rejects_import_paths_duplicates_and_post_seal_mutation(self) -> None:
        registry = CapabilityProviderRegistry()
        with self.assertRaises(CapabilityRegistryError):
            registry.register_factory(
                "unsafe.provider",
                "package.module:Provider",  # type: ignore[arg-type]
                provider_version="1.0.0",
            )
        registry.register_instance(_Provider())
        with self.assertRaises(CapabilityRegistryError):
            registry.register_instance(_Provider())

        class WrongProvider(_Provider):
            provider_key = "different.provider"

        with self.assertRaises(CapabilityRegistryError):
            registry.register_instance(
                WrongProvider(), provider_key="expected.provider"
            )
        registry.seal()
        with self.assertRaises(CapabilityRegistryError):
            registry.register_factory(
                "later.provider",
                _Provider,
                provider_version="1.0.0",
            )


class DeploymentResolutionTests(unittest.TestCase):
    def test_zero_data_port_deployment_uses_no_orm_or_database(self) -> None:
        resolved = build_resolved_deployment(definition())
        self.assertEqual(resolved.scenario_id, "scenario-kernel")
        self.assertEqual(resolved.tenant_id, "tenant-kernel")
        self.assertEqual(resolved.definition_hash, DEFINITION_HASH)
        self.assertEqual(resolved.data_ports, ())
        self.assertEqual(resolved.data_context.handles, ())
        self.assertRegex(resolved.fingerprint, r"^[0-9a-f]{64}$")

    def test_deployment_hash_uses_only_sanitized_binding_signatures(self) -> None:
        data_port = port()
        first = build_resolved_deployment(
            definition(),
            data_ports=(data_port,),
            bindings=(
                binding(
                    reference_id="physical-reference-a",
                    password="first-secret",
                ),
            ),
        )
        same_signature = build_resolved_deployment(
            definition(),
            data_ports=(data_port,),
            bindings=(
                binding(
                    reference_id="physical-reference-b",
                    password="second-secret",
                ),
            ),
        )
        changed_signature = build_resolved_deployment(
            definition(),
            data_ports=(data_port,),
            bindings=(binding(signature=BINDING_SIGNATURE_B),),
        )

        self.assertEqual(first.definition_hash, same_signature.definition_hash)
        self.assertEqual(first.definition_hash, changed_signature.definition_hash)
        self.assertEqual(first.fingerprint, same_signature.fingerprint)
        self.assertNotEqual(first.fingerprint, changed_signature.fingerprint)
        self.assertEqual(
            first.data_context.get("primary-input").reference_id,
            "physical-reference-a",
        )
        self.assertEqual(
            same_signature.data_context.get("primary-input").reference_id,
            "physical-reference-b",
        )

    def test_definition_hash_and_deployment_fingerprint_are_distinct_pins(self) -> None:
        first = build_resolved_deployment(
            definition(), data_ports=(port(),), bindings=(binding(),)
        )
        changed_definition = build_resolved_deployment(
            definition(definition_hash=OTHER_DEFINITION_HASH),
            data_ports=(port(),),
            bindings=(binding(),),
        )
        self.assertEqual(first.definition_hash, DEFINITION_HASH)
        self.assertEqual(changed_definition.definition_hash, OTHER_DEFINITION_HASH)
        self.assertNotEqual(first.fingerprint, changed_definition.fingerprint)
        self.assertNotEqual(first.definition_hash, first.fingerprint)

        capability = CapabilityRef(kind="function", resource_id="function-a")
        matching = Request(
            capability=capability,
            expected_definition_hash=first.definition_hash,
            expected_deployment_fingerprint=first.fingerprint,
        )
        require_request_matches_deployment(matching, first)
        with self.assertRaises(DeploymentResolutionError):
            require_request_matches_deployment(matching, changed_definition)

    def test_binding_resolution_enforces_required_override_and_kind_contracts(self) -> None:
        with self.assertRaises(DeploymentResolutionError):
            resolve_runtime_data_context((port(),))

        override = BindingOverride(
            port_key="primary-input",
            binding_kind="managed-dataset",
            reference_id="managed-reference-b",
            version_id="version-b",
            signature=BINDING_SIGNATURE_B,
        )
        context = resolve_runtime_data_context(
            (port(),),
            (binding(),),
            overrides=(override,),
        )
        selected = context.get("primary-input")
        self.assertIsNotNone(selected)
        self.assertEqual(selected.reference_id, "managed-reference-b")
        self.assertEqual(selected.signature, BINDING_SIGNATURE_B)

        with self.assertRaises(DeploymentResolutionError):
            resolve_runtime_data_context(
                (port(allow_override=False),),
                (binding(),),
                overrides=(override,),
            )
        with self.assertRaises(DeploymentResolutionError):
            resolve_runtime_data_context(
                (port(),),
                (
                    SimpleNamespace(
                        binding_key="primary-input",
                        connector_kind="unsupported-kind",
                        connector_id="reference-a",
                        connector_signature=BINDING_SIGNATURE_A,
                    ),
                ),
            )
        with self.assertRaises(DeploymentResolutionError):
            resolve_runtime_data_context(
                (port(required=False),),
                (binding(key="undeclared-port"),),
            )
        with self.assertRaises(DeploymentResolutionError):
            resolve_runtime_data_context(
                (port(),),
                (binding(),),
                overrides=(
                    BindingOverride(
                        port_key="primary-input",
                        binding_kind="managed-dataset",
                        reference_id="unsigned-reference",
                    ),
                ),
            )

    def test_binding_order_does_not_change_runtime_or_deployment_fingerprint(self) -> None:
        ports = (
            port("first", required=True),
            port("second", required=True),
        )
        bindings = (
            binding("first", signature=BINDING_SIGNATURE_A),
            binding("second", signature=BINDING_SIGNATURE_B),
        )
        left = build_resolved_deployment(
            definition(), data_ports=ports, bindings=bindings
        )
        right = build_resolved_deployment(
            definition(),
            data_ports=reversed(ports),
            bindings=reversed(bindings),
        )
        self.assertEqual(left.data_context.fingerprint, right.data_context.fingerprint)
        self.assertEqual(left.fingerprint, right.fingerprint)

    def test_handle_requires_a_precomputed_signature_and_never_hashes_config(self) -> None:
        unsafe = SimpleNamespace(
            binding_key="primary-input",
            connector_kind="managed-dataset",
            connector_id="reference-a",
            connector_signature="",
            config={"authorization": "Bearer never-hash-me"},
        )
        with self.assertRaises(DeploymentResolutionError):
            build_resolved_deployment(
                definition(),
                data_ports=(port(),),
                bindings=(unsafe,),
            )

        handle = ResolvedDataHandle(
            port_key="primary-input",
            binding_kind="managed-dataset",
            reference_id="reference-a",
            signature=BINDING_SIGNATURE_A,
        )
        context = RuntimeDataContext((handle,))
        self.assertEqual(
            dict(context.signature_facts()[0]),
            {
                "binding_kind": "managed-dataset",
                "port_key": "primary-input",
                "signature": BINDING_SIGNATURE_A,
            },
        )


if __name__ == "__main__":
    unittest.main()
