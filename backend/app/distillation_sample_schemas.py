"""Logical tool handles for bounded source inspection; no SQL or physical names."""
from typing import Annotated, Literal

from pydantic import Field

from .distillation_schemas import ClosedModel


Handle = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


class SampleFilter(ClosedModel):
    field_key: Handle
    operator: Literal["eq", "gte", "lte"] = "eq"
    value: str = Field(max_length=500)


class DatabaseSampleArguments(ClosedModel):
    data_source_id: str = Field(min_length=1, max_length=32)
    bucket_file_id: str | None = Field(default=None, min_length=1, max_length=32)
    table_key: Handle | None = None
    field_keys: list[Handle] = Field(default_factory=list, max_length=20)
    filters: list[SampleFilter] = Field(default_factory=list, max_length=5)
    limit: int = Field(default=30, ge=1, le=100)


class MatchFields(ClosedModel):
    left: Handle
    right: Handle


class CompareSamplesArguments(ClosedModel):
    left_evidence_key: str = Field(min_length=1, max_length=64)
    right_evidence_key: str = Field(min_length=1, max_length=64)
    fields: list[MatchFields] = Field(min_length=1, max_length=5)
