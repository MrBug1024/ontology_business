"""Write-only secrets and explicit authorization for bounded system research."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from .distillation_target_schemas import TargetSystem


class SystemCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    auth_type: Literal["basic", "bearer", "browser"]
    username: str = Field(default="", max_length=200, repr=False)
    secret: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    expires_at: datetime
    authorization_basis: str = Field(min_length=1, max_length=4000)
    authorized_readonly: Literal[True]

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if self.auth_type in {"basic", "browser"} and (not self.username.strip() or (self.auth_type == "basic" and ":" in self.username)):
            raise ValueError("专用账号必填且不能包含冒号")
        if self.auth_type == "bearer" and self.username:
            raise ValueError("令牌访问不填写账号")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.username + self.secret.get_secret_value()):
            raise ValueError("访问凭据格式无效")
        if self.expires_at.tzinfo is None:
            raise ValueError("授权到期时间必须包含时区")
        if not self.authorization_basis.strip():
            raise ValueError("请记录授权依据")
        return self


class SystemAccessRequest(SystemCredentials):
    expected_revision: int = Field(ge=1)


class SystemConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    expected_revision: int = Field(ge=1)
    target: TargetSystem
    credentials: SystemCredentials | None = Field(default=None, repr=False)


class SystemAccessOut(BaseModel):
    target_key: str
    status: Literal["active", "expired", "revoked", "scope_changed", "missing"]
    expires_at: datetime | None = None
    authorization_basis: str = ""
