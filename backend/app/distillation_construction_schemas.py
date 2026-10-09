"""Versioned logical property requirements for business construction handoff."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PropertyRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    attribute: str = Field(min_length=1, max_length=200)
    data_type: Literal["string", "text", "integer", "float", "number", "boolean", "date", "datetime", "json"]
    is_required: bool
    is_key: bool = False
    description: str = Field(default="", max_length=2000)
    constraints: dict[str, int | float | str | bool] = Field(default_factory=dict, max_length=12)
    enum_values: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_constraints(self):
        # Same governed constraint vocabulary as the ontology authoring API;
        # type semantics are checked by its deterministic validator as well.
        from .services.ontology_service import normalize_property_constraints

        self.constraints = normalize_property_constraints(self.data_type, self.constraints)
        if self.is_key and not self.is_required:
            raise ValueError("身份属性必须明确为必填")
        if any(len(value) > 200 for value in self.enum_values):
            raise ValueError("枚举值超过长度上限")
        if len(self.enum_values) != len(set(self.enum_values)):
            raise ValueError("枚举值不能重复")
        return self


class ConstructionGap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_key: str = Field(max_length=64)
    name: str = Field(max_length=200)
    missing: list[str] = Field(max_length=10)
    next_step: str = Field(max_length=1000)


class DistillationConstructionQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["distillation-construction-quality.v1"]
    entity_count: int = Field(ge=0, le=80)
    complete_entity_keys: list[str] = Field(max_length=80)
    complete_entity_count: int = Field(ge=0, le=80)
    issues: list[ConstructionGap] = Field(max_length=80)
    construction_complete: bool
