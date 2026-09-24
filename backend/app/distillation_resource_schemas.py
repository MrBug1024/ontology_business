"""Public, credential-free resource catalog for business investigation."""
from __future__ import annotations

from typing import Literal

from pydantic import Field

from .distillation_conversation_schemas import InvestigationToolCatalogOut
from .distillation_schemas import ClosedModel


class InvestigationModelOut(ClosedModel):
    id: str = Field(max_length=32)
    name: str = Field(max_length=200)
    model: str = Field(max_length=200)
    capabilities: list[str] = Field(max_length=20)


class InvestigationSkillOut(ClosedModel):
    id: str = Field(max_length=32)
    name: str = Field(max_length=200)
    description: str = Field(max_length=2000)
    mode: Literal["instructions"] = "instructions"


class InvestigationMCPOut(ClosedModel):
    id: str = Field(max_length=32)
    name: str = Field(max_length=200)
    transport: Literal["sse", "streamable_http", "http"]
    mode: Literal["capability"] = "capability"


class InvestigationResourceCatalogOut(ClosedModel):
    models: list[InvestigationModelOut] = Field(max_length=200)
    skills: list[InvestigationSkillOut] = Field(max_length=200)
    mcps: list[InvestigationMCPOut] = Field(max_length=200)
    investigation_tools: InvestigationToolCatalogOut
