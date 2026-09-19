"""Closed browser commands. Credentials and executable code are never arguments."""
from typing import Literal

from pydantic import Field

from .distillation_schemas import ClosedModel


class BrowserTarget(ClosedModel):
    target_key: str = Field(min_length=1, max_length=64)


class BrowserNavigate(BrowserTarget):
    path: str = Field(min_length=1, max_length=1500)


class BrowserInspect(BrowserTarget):
    offset: int = Field(default=0, ge=0, le=5000)


class BrowserElement(BrowserTarget):
    page_id: str = Field(min_length=1, max_length=32)
    element_ref: str = Field(min_length=1, max_length=64)


class BrowserFill(BrowserElement):
    value: str = Field(max_length=500)


class BrowserLogin(BrowserTarget):
    page_id: str = Field(min_length=1, max_length=32)
    username_ref: str = Field(min_length=1, max_length=64)
    masked_input_ref: str = Field(min_length=1, max_length=64)
    submit_ref: str = Field(min_length=1, max_length=64)


class BrowserClick(BrowserElement):
    intent: Literal["navigate", "query"]
