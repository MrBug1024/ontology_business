"""Read-only business understanding, separate from frozen handoffs and runtime inputs."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Text = Annotated[str, Field(max_length=4000)]
Summary = Annotated[str, Field(max_length=600)]
Label = Annotated[str, Field(max_length=200)]
ResourceId = Annotated[str, Field(min_length=1, max_length=32)]


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryScenario(ContextModel):
    id: ResourceId
    name: Label
    description: Text


class DiscoveryBusiness(ContextModel):
    beneficiary: Text
    pain: Text
    desired_outcome: Text
    success_metric: Text
    scope: Text
    non_goals: Text
    decision: Literal["undecided", "continue", "adjust", "stop"]
    decision_reason: Text
    open_questions: list[Text] = Field(max_length=40)


class DiscoveryHandoff(ContextModel):
    status: Literal["missing", "current", "stale"]
    publication_id: ResourceId | None
    publication_revision: int | None = Field(ge=1)


class DiscoveryConstruction(ContextModel):
    can_continue: bool
    reason: Summary


class DiscoveryProcessNode(ContextModel):
    key: Annotated[str, Field(max_length=64)]
    name: Label
    owner: Label
    outcome: Summary
    trigger: Summary
    inputs: Summary
    rule: Summary
    exceptions: Summary


class DiscoveryProcess(ContextModel):
    nodes: list[DiscoveryProcessNode] = Field(max_length=12)
    total_nodes: int = Field(ge=0, le=80)
    has_more: bool


class DiscoveryProcesses(ContextModel):
    as_is: DiscoveryProcess
    to_be: DiscoveryProcess


class DiscoveryHistoricalCase(ContextModel):
    key: Annotated[str, Field(max_length=64)]
    title: Label
    result_summary: Summary
    limitations: Summary


class DiscoveryHistoricalCases(ContextModel):
    items: list[DiscoveryHistoricalCase] = Field(max_length=5)
    total_count: int = Field(ge=0, le=20)
    has_more: bool


class DiscoveryMaterial(ContextModel):
    data_source_id: ResourceId
    name: Label
    type: Annotated[str, Field(max_length=50)]
    scope: Literal["scenario", "shared"]
    resource_scope: Literal["modeling"]
    content_read: Literal[False]


class DiscoveryMaterials(ContextModel):
    sources: list[DiscoveryMaterial] = Field(max_length=20)
    has_more: bool
    next_offset: int | None = Field(ge=0)


class ScenarioDiscoveryContextOut(ContextModel):
    version: Literal["scenario-discovery-context.v1"]
    scenario: DiscoveryScenario
    revision: int | None = Field(ge=1)
    business: DiscoveryBusiness
    handoff: DiscoveryHandoff
    construction: DiscoveryConstruction
    processes: DiscoveryProcesses
    historical_cases: DiscoveryHistoricalCases
    materials: DiscoveryMaterials
    boundaries: list[Summary] = Field(max_length=10)
