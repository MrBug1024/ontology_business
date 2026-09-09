"""Bounded message replies and channel-neutral delivery contracts."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_version_id: str | None = Field(default=None, min_length=1, max_length=32)
    dataset_version_id: str | None = Field(default=None, min_length=1, max_length=32)
    expected_signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def one_reference(self) -> "EvidenceReference":
        if sum(value is not None for value in (self.asset_version_id, self.dataset_version_id)) != 1:
            raise ValueError("证据必须引用一个不可变的受管文件或数据版本")
        return self


class ChannelReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=5000)
    message_id: str = Field(min_length=1, max_length=160)
    expected_revision: int = Field(ge=1)
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=20)


class ChannelInteractionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["workflow_approval", "capability_confirmation"]
    id: str
    code: str
    revision: int
    status: str = "pending"
    title: str
    text: str
    reply_texts: list[str]
    expires_at: str | None = None
    recipient_user_ids: list[str] = Field(default_factory=list)
    recipient_roles: list[str] = Field(default_factory=list)
    requires_evidence: bool = False


class ChannelAttachmentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    filename: str


class ChannelDeliveryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: Literal["channel-delivery/v1"] = "channel-delivery/v1"
    format: Literal["text/plain"] = "text/plain"
    text: str = ""
    revision: str = ""
    interactions: list[ChannelInteractionOut] = Field(default_factory=list)
    attachments: list[ChannelAttachmentOut] = Field(default_factory=list)


class ChannelReplyOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_id: str
    status: str
    text: str
    invocation_id: str | None = None
    workflow_run_id: str | None = None


class ApprovalAudience(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approver_user_ids: list[str] = Field(default_factory=list, max_length=100)
    approver_roles: list[Literal["owner", "admin", "operator", "viewer"]] = Field(default_factory=list, max_length=4)
    requires_evidence: bool = False

    @model_validator(mode="after")
    def bounded_users(self) -> "ApprovalAudience":
        if any(not user_id or len(user_id) > 32 for user_id in self.approver_user_ids):
            raise ValueError("审批人员引用无效")
        if len(set(self.approver_user_ids)) != len(self.approver_user_ids):
            raise ValueError("审批人员不能重复")
        return self
