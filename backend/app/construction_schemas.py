"""Bounded human instructions for re-evaluating a saved construction result."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConstructionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    answer: str = Field(min_length=1, max_length=1500)


class ConstructionResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    proposal_id: str = Field(min_length=1, max_length=64)
    expected_revision: int = Field(ge=1)
    action: Literal["clarify", "replan"]
    answers: list[ConstructionAnswer] = Field(default_factory=list, max_length=3)
    rationale: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_instruction(self):
        if self.action == "clarify" and not self.answers:
            raise ValueError("请至少回答一个具体阻塞问题")
        if self.action == "replan" and not self.rationale:
            raise ValueError("重新规划需要说明原方案的问题与可接受的取舍")
        if len({item.question_id for item in self.answers}) != len(self.answers):
            raise ValueError("同一问题只能提交一次回答")
        return self
