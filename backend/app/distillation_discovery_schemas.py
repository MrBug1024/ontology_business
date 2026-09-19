"""Logical historical cases reconstructed by the investigation agent."""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Text = Annotated[str, Field(max_length=4000)]
Refs = Annotated[list[Key], Field(max_length=40)]


class DiscoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CaseStep(DiscoveryModel):
    node_key: Key
    input_summary: Text = ""
    action: Text = ""
    output_summary: Text = ""
    evidence_refs: Refs = Field(default_factory=list)


class HistoricalCase(DiscoveryModel):
    key: Key
    title: Annotated[str, Field(min_length=1, max_length=200)]
    scope: Text = ""
    result_summary: Text = ""
    result_refs: Refs = Field(default_factory=list)
    input_refs: Refs = Field(default_factory=list)
    process_refs: Refs = Field(default_factory=list)
    knowledge_refs: Refs = Field(default_factory=list)
    association_basis: Text = ""
    steps: list[CaseStep] = Field(default_factory=list, max_length=80)
    discrepancies: Text = ""
    limitations: Text = ""
