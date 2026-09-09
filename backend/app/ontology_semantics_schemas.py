"""Closed authoring contracts for object lifecycle and workflow semantics."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StateTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_state: str = Field(min_length=1, max_length=120)
    to_state: str = Field(min_length=1, max_length=120)


class StatePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    initial_states: list[Annotated[str, Field(min_length=1, max_length=120)]] = Field(default_factory=list, max_length=100)
    transitions: list[StateTransition] = Field(default_factory=list, max_length=500)


class OntologyInputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=300, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$")
    entity_id: str = Field(min_length=1, max_length=32)
    many: bool = False
    partial: bool = False


class WorkflowOntologyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    entity_ids: list[Annotated[str, Field(min_length=1, max_length=32)]] = Field(default_factory=list, max_length=64)
    input_bindings: list[OntologyInputBinding] = Field(default_factory=list, max_length=32)
    output_node_id: str = Field(default="", max_length=100)
    output_schema: dict[str, Any] = Field(default_factory=dict)


class InstanceIntegrity(BaseModel):
    status: Literal["valid", "incomplete", "invalid"]
    issues: list[str] = Field(default_factory=list)
