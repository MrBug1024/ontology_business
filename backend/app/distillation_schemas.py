"""Bounded business discovery documents, independent of runtime capability definitions."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .distillation_target_schemas import TargetSystem
from .distillation_discovery_schemas import HistoricalCase


Text = Annotated[str, Field(max_length=4000)]
Label = Annotated[str, Field(max_length=200)]
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
ResourceId = Annotated[str, Field(min_length=1, max_length=32)]
Refs = Annotated[list[Key], Field(max_length=40)]


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InvestigationSource(ClosedModel):
    turn_id: ResourceId
    step_id: ResourceId
    content_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class LibraryReadReference(ClosedModel):
    turn_id: ResourceId
    step_id: ResourceId
    identity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class MCPReadReference(ClosedModel):
    turn_id: ResourceId
    step_id: ResourceId
    identity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class InterviewReference(ClosedModel):
    turn_id: ResourceId
    message_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Evidence(ClosedModel):
    key: Key
    title: Label
    kind: Literal["material", "observation", "system_export"] = "observation"
    role: Literal["input", "knowledge", "result", "process", "reference"] = "reference"
    data_source_id: ResourceId | None = None
    bucket_file_id: ResourceId | None = None
    summary: Text = ""
    coverage: Text = ""
    limitations: Text = ""
    investigation_source: InvestigationSource | None = None
    library_read: LibraryReadReference | None = None
    mcp_read: MCPReadReference | None = None
    interview: InterviewReference | None = None

    @model_validator(mode="after")
    def validate_material_reference(self) -> Self:
        if self.interview and (self.data_source_id or self.bucket_file_id or self.investigation_source or self.library_read or self.mcp_read):
            raise ValueError("访谈陈述不能混用其他来源回执")
        if self.mcp_read and (self.data_source_id or self.bucket_file_id or self.investigation_source or self.library_read):
            raise ValueError("MCP 资料回执不能混用其他资料来源")
        if self.library_read and (not self.data_source_id or self.investigation_source):
            raise ValueError("资料库读取回执必须关联资料库，且不能混用网页回执")
        if self.bucket_file_id and not self.data_source_id:
            raise ValueError("文件证据必须关联所属建模资料")
        if self.kind == "material" and not self.data_source_id:
            raise ValueError("建模资料证据必须选择受管资料")
        return self


class Assertion(ClosedModel):
    key: Key
    statement: Text = ""
    status: Literal["fact", "inference", "hypothesis", "conflict"] = "hypothesis"
    evidence_refs: Refs = Field(default_factory=list)


class ProcessNode(ClosedModel):
    key: Key
    name: Label
    owner: Label = ""
    outcome: Text = ""
    trigger: Text = ""
    inputs: Text = ""
    rule: Text = ""
    exceptions: Text = ""
    evidence_refs: Refs = Field(default_factory=list)


class ProcessEdge(ClosedModel):
    source: Key
    target: Key
    label: Label = ""


class ProcessGraph(ClosedModel):
    nodes: list[ProcessNode] = Field(default_factory=list, max_length=80)
    edges: list[ProcessEdge] = Field(default_factory=list, max_length=160)


class Improvement(ClosedModel):
    key: Key
    existing_node_key: Key
    decision: Literal["retain", "remove", "merge", "replace"] = "retain"
    rationale: Text = ""
    expected_benefit: Text = ""


class Entity(ClosedModel):
    key: Key
    name: Label
    description: Text = ""
    attributes: list[Label] = Field(default_factory=list, max_length=60)
    identity: Text = ""
    evidence_refs: Refs = Field(default_factory=list)


class Relation(ClosedModel):
    source: Key
    target: Key
    label: Label = ""
    cardinality: Literal["unconfirmed", "one_to_one", "one_to_many", "many_to_many"] = "unconfirmed"
    rationale: Text = ""
    evidence_refs: Refs = Field(default_factory=list)


class Lineage(ClosedModel):
    source: Key
    target: Key
    transformation: Text = ""
    evidence_refs: Refs = Field(default_factory=list)


def _unique_keys(items: list) -> set[str]:
    keys = [item.key for item in items]
    if len(keys) != len(set(keys)):
        raise ValueError("同一集合中的标识不能重复")
    return set(keys)


class DistillationDocument(ClosedModel):
    beneficiary: Text = ""
    pain: Text = ""
    desired_outcome: Text = ""
    success_metric: Text = ""
    scope: Text = ""
    non_goals: Text = ""
    target_systems: list[TargetSystem] = Field(default_factory=list, max_length=10)
    evidence: list[Evidence] = Field(default_factory=list, max_length=40)
    assertions: list[Assertion] = Field(default_factory=list, max_length=100)
    as_is: ProcessGraph = Field(default_factory=ProcessGraph)
    to_be: ProcessGraph = Field(default_factory=ProcessGraph)
    improvements: list[Improvement] = Field(default_factory=list, max_length=80)
    entities: list[Entity] = Field(default_factory=list, max_length=80)
    relations: list[Relation] = Field(default_factory=list, max_length=160)
    lineage: list[Lineage] = Field(default_factory=list, max_length=160)
    decision: Literal["undecided", "continue", "adjust", "stop"] = "undecided"
    decision_reason: Text = ""
    open_questions: list[Text] = Field(default_factory=list, max_length=40)
    historical_cases: list[HistoricalCase] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        _unique_keys(self.target_systems)
        evidence_keys = _unique_keys(self.evidence)
        entity_keys = _unique_keys(self.entities)
        _unique_keys(self.assertions)
        _unique_keys(self.improvements)
        _unique_keys(self.historical_cases)
        for graph in (self.as_is, self.to_be):
            keys = _unique_keys(graph.nodes)
            if any(edge.source not in keys or edge.target not in keys for edge in graph.edges):
                raise ValueError("流程连线必须引用该流程中的节点")
        refs = [*self.assertions, *self.as_is.nodes, *self.to_be.nodes, *self.lineage,
                *self.entities, *self.relations]
        if any(not set(item.evidence_refs).issubset(evidence_keys) for item in refs):
            raise ValueError("引用了不存在的证据")
        if any(item.status == "fact" and not item.evidence_refs for item in self.assertions):
            raise ValueError("事实必须关联至少一条可核对证据")
        if any(item.source not in entity_keys or item.target not in entity_keys
               for item in [*self.relations, *self.lineage]):
            raise ValueError("关系和血缘必须引用现有业务实体")
        as_is_keys = {node.key for node in self.as_is.nodes}
        for case in self.historical_cases:
            case_refs = [*case.result_refs, *case.input_refs, *case.process_refs, *case.knowledge_refs,
                         *(ref for step in case.steps for ref in step.evidence_refs)]
            if not set(case_refs).issubset(evidence_keys):
                raise ValueError("历史案例引用了不存在的证据")
            if any(step.node_key not in as_is_keys for step in case.steps):
                raise ValueError("历史案例步骤必须引用现状流程节点")
        if any(item.existing_node_key not in as_is_keys for item in self.improvements):
            raise ValueError("流程改进必须引用现状节点")
        if len(self.model_dump_json().encode("utf-8")) > 256_000:
            raise ValueError("蒸馏文档超过 256 KB，请缩小研究范围")
        return self


class ProjectCreate(ClosedModel):
    name: Annotated[str, Field(min_length=1, max_length=200)]
    scenario_id: ResourceId | None = None
    document: DistillationDocument = Field(default_factory=DistillationDocument)


class RevisionRequest(ClosedModel):
    expected_revision: int = Field(ge=1)


class ProjectUpdate(ProjectCreate, RevisionRequest):
    pass


class AnalyzeRequest(RevisionRequest):
    instructions: Text = ""


class ProjectOut(ProjectCreate):
    id: str
    revision: int
    created_at: datetime
    updated_at: datetime
    can_write: bool


class ScenarioStateOut(ClosedModel):
    scenario_id: ResourceId
    revision: int
    document: DistillationDocument
    updated_at: datetime


class AnalysisOut(ClosedModel):
    base_revision: int
    document: DistillationDocument
    limitations: list[Text] = Field(default_factory=list, max_length=50)


class ArtifactOut(ClosedModel):
    key: str
    filename: str
    mime: str
    sha256: str


class PublicationOut(ClosedModel):
    id: str
    project_id: str | None
    scenario_id: str | None
    project_revision: int
    data_source_id: str | None
    created_at: datetime
    artifacts: list[ArtifactOut]
