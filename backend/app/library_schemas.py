"""Closed contracts for human-configured research libraries."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RemoteLibraryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    user: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", max_length=4096, repr=False)

    @field_validator("host", "database", "user")
    @classmethod
    def clean_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("连接字段无效")
        return value

    @field_validator("host")
    @classmethod
    def hostname_only(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.:-]+", value):
            raise ValueError("主机必须是域名或 IP，不能是连接地址或路径")
        return value

    @field_validator("password")
    @classmethod
    def safe_password(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("密码格式无效")
        return value


LibraryType = Literal["postgres", "mysql", "sqlite3", "file_bucket", "dataset"]
