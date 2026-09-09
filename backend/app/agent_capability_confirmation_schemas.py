"""Browser confirmation contract; confirmation tokens and raw inputs stay server-side."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .channel_interaction_schemas import ChannelDeliveryOut


class AgentCapabilityConfirmationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_id: str = Field(min_length=1, max_length=64)
    confirmed: Literal[True]


class AgentCapabilityReceiptOut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    invocation_id: str
    status: str
    name: str
    can_confirm: bool
    message: str
    workflow_run_id: str | None = None
    artifact_file_id: str | None = None
    artifact_filename: str | None = None
    delivery: ChannelDeliveryOut = Field(default_factory=ChannelDeliveryOut)
