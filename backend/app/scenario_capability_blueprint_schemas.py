"""Bounded public semantic projections; these fields never become invocation inputs."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Key = Annotated[str, Field(min_length=1, max_length=240)]
Text = Annotated[str, Field(max_length=20_000)]
CapabilityKind = Literal['function', 'action', 'rule', 'workflow', 'event']


class BlueprintModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class BlueprintReference(BlueprintModel):
    kind: CapabilityKind
    key: Key


class BlueprintScenario(BlueprintModel):
    id: Key
    name: str = Field(max_length=1000)
    description: Text


class BlueprintDeployment(BlueprintModel):
    definition_source: Literal['release']
    release_id: Key
    snapshot_id: Key
    definition_hash: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]{64}$')


class BlueprintStage(BlueprintModel):
    key: Key
    label: str = Field(max_length=100)
    contribution: str = Field(max_length=2000)
    boundary: str = Field(max_length=2000)


class BlueprintProperty(BlueprintModel):
    key: Key
    api_name: str = Field(max_length=240)
    name: str = Field(max_length=1000)
    data_type: str = Field(max_length=50)
    description: Text
    is_key: bool
    is_required: bool
    is_title: bool
    is_enum: bool
    enum_values: list[Annotated[str, Field(max_length=2000)]] = Field(max_length=1000)


class BlueprintObject(BlueprintModel):
    key: Key
    api_name: str = Field(max_length=240)
    name: str = Field(max_length=1000)
    description: Text
    properties: list[BlueprintProperty] = Field(max_length=1000)


class BlueprintRelation(BlueprintModel):
    key: Key
    api_name: str = Field(max_length=240)
    name: str = Field(max_length=1000)
    description: Text
    source_object_key: Key
    target_object_key: Key
    cardinality: Literal['1:1', '1:N', 'N:1', 'N:M']


class BlueprintOntology(BlueprintModel):
    objects: list[BlueprintObject] = Field(max_length=1000)
    relations: list[BlueprintRelation] = Field(max_length=2000)


class BlueprintNodeCount(BlueprintModel):
    kind: str = Field(min_length=1, max_length=50)
    count: int = Field(ge=1, le=1000)


class BlueprintInputBinding(BlueprintModel):
    path: str = Field(min_length=1, max_length=300)
    object_key: Key
    many: bool
    partial: bool


class BlueprintSemantic(BlueprintModel):
    role: str = Field(max_length=2000)
    runtime_kind: str | None = Field(default=None, max_length=50)
    trigger_type: Literal['manual', 'scheduled', 'event'] | None = None
    node_counts: list[BlueprintNodeCount] = Field(max_length=30)
    requires_approval: bool
    object_keys: list[Key] = Field(max_length=1000)
    dependencies: list[BlueprintReference] = Field(max_length=1000)
    input_bindings: list[BlueprintInputBinding] = Field(max_length=32)
    output_node_keys: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(max_length=32)


class BlueprintCapability(BlueprintReference):
    name: str = Field(max_length=1000)
    description: Text
    selected: bool
    available: bool
    dependency: bool
    invocation_supported: bool
    invocation_authorized: bool
    enabled: bool
    ready: bool
    semantic: BlueprintSemantic


class BlueprintCoverage(BlueprintModel):
    selected: list[BlueprintReference] = Field(max_length=200)
    available: list[BlueprintReference] = Field(max_length=1000)
    dependencies: list[BlueprintReference] = Field(max_length=1000)
    unselected_available: list[BlueprintReference] = Field(max_length=1000)


class ScenarioCapabilityBlueprintOut(BlueprintModel):
    version: Literal['scenario-capability-blueprint.v1']
    completeness: Literal['complete_authorized_projection']
    scenario: BlueprintScenario
    deployment: BlueprintDeployment
    stages: list[BlueprintStage] = Field(max_length=10)
    ontology: BlueprintOntology
    capabilities: list[BlueprintCapability] = Field(max_length=2000)
    coverage: BlueprintCoverage


class DeliveryProfileItem(BlueprintModel):
    key: Key
    label: str = Field(max_length=1000)
    purpose: str = Field(max_length=4000)


class DeliveryProfileStandard(DeliveryProfileItem):
    url: str = Field(max_length=2000)


class DeliveryProfileComponent(DeliveryProfileItem):
    required: bool
    supported: bool


class DeliveryProfileHost(BlueprintModel):
    key: Literal['claude_code', 'codex']
    label: Literal['Claude Code', 'Codex']
    scope: Literal['host_specific']


class PluginDeliveryProfileOut(BlueprintModel):
    version: Literal['scenario-plugin-delivery-profile.v1', 'scenario-plugin-delivery-profile.v2']
    host: DeliveryProfileHost
    standards: list[DeliveryProfileStandard] = Field(max_length=10)
    components: list[DeliveryProfileComponent] = Field(max_length=20)
    boundaries: list[DeliveryProfileItem] = Field(max_length=20)
    platform_rules: list[DeliveryProfileItem] = Field(max_length=30)
    protected_references: list[Annotated[str, Field(max_length=240)]] = Field(max_length=10)
