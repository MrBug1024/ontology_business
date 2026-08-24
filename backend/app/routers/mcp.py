"""MCP 配置路由。"""
from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import MCPConfig, normalize_mcp_name_key
from ..schemas import (
    MCPConfigIn,
    MCPConfigOut,
    MCPImportItemOut,
    MCPImportResultOut,
    MCPStandardImportIn,
    MCPToolInfo,
    Msg,
)
from ..services import connector_service, mcp_service, permission_service, tenant_service
from ..services.auth_service import get_tenant_db

router = APIRouter(prefix="/mcp", tags=["mcp"])

def _public_map(values: dict | None) -> dict:
    """MCP env/header values are write-only; every value may be a credential."""
    return {str(key): "" for key in (values or {})}


_OBVIOUS_CLI_SECRET = re.compile(
    r"(?i)(?:bearer\s+|basic\s+|authorization\s*[:=]|cookie\s*[:=]|"
    r"api[_-]?key|access[_-]?token|password|secret|--token(?:\b|=)|token\s*=)"
)


def _public_stdio_fields(command: str | None, args: list | None) -> tuple[str, list[str]]:
    """Fail closed for legacy stdio rows that embedded credentials in argv."""
    if command is not None and not isinstance(command, str):
        return "", []
    if args is not None and (
        not isinstance(args, list) or any(not isinstance(item, str) for item in args)
    ):
        return "", []
    normalized_command = command or ""
    normalized_args = list(args or [])
    if _OBVIOUS_CLI_SECRET.search("\n".join([normalized_command, *normalized_args])):
        return "", []
    return normalized_command, normalized_args


def _merge_map(old: dict | None, new: dict | None) -> dict:
    """Blank submitted values keep the same key; omitted keys are deleted."""
    result = {}
    previous = dict(old or {})
    for key, value in (new or {}).items():
        if value == "" and key in previous:
            result[key] = previous[key]
        elif value != "":
            result[key] = value
    return result


def _public_endpoint(value: str | None) -> str:
    """Return a credential-free HTTP(S) endpoint or fail closed for legacy data."""
    raw = str(value or "").strip()
    if not raw or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    try:
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if port == 0 or "\\" in parsed.netloc or "\\" in parsed.path:
        return ""
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        display_host = f"[{literal.compressed}]" if literal.version == 6 else literal.compressed
    else:
        try:
            ascii_hostname = hostname.rstrip(".").encode("idna").decode("ascii")
        except UnicodeError:
            return ""
        labels = ascii_hostname.split(".")
        if (
            not ascii_hostname
            or len(ascii_hostname) > 253
            or any(
                len(label) > 63
                or re.fullmatch(
                    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
                    label,
                ) is None
                for label in labels
            )
        ):
            return ""
        display_host = ascii_hostname.casefold()
    if re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path):
        return ""
    netloc = f"{display_host}:{port}" if port is not None else display_host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _assert_transport_policy(config: MCPConfigIn) -> None:
    if config.transport == "stdio" and config.enabled and not get_settings().allow_mcp_stdio:
        raise HTTPException(
            403,
            "当前部署未开启服务端 stdio MCP；请使用远程 HTTPS MCP，或由运维显式开启受沙箱保护的 stdio",
        )


def _assert_unique_name(
    db: Session,
    name: str,
    *,
    exclude_id: str = "",
) -> None:
    tenant_id = tenant_service.current_tenant_id(db)
    query = select(MCPConfig.id).where(
        MCPConfig.tenant_id == tenant_id,
        MCPConfig.name_key == normalize_mcp_name_key(name),
    )
    if exclude_id:
        query = query.where(MCPConfig.id != exclude_id)
    if db.execute(query.limit(1)).scalar_one_or_none():
        raise HTTPException(409, f"MCP 服务名称已存在：{name}")


def _commit_or_name_conflict(
    db: Session,
    names: list[str],
    *,
    exclude_ids: set[str] | None = None,
) -> None:
    """Commit atomically and translate a concurrent name race into HTTP 409."""
    try:
        db.commit()
        return
    except IntegrityError as exc:
        db.rollback()
        tenant_id = tenant_service.current_tenant_id(db)
        keys = {normalize_mcp_name_key(name) for name in names}
        query = select(MCPConfig.name).where(
            MCPConfig.tenant_id == tenant_id,
            MCPConfig.name_key.in_(keys),
        )
        if exclude_ids:
            query = query.where(MCPConfig.id.not_in(exclude_ids))
        conflicts = sorted(set(db.execute(query).scalars().all()))
        if conflicts:
            raise HTTPException(
                409,
                f"MCP 服务名称已存在：{'、'.join(conflicts)}；请刷新后重试",
            ) from exc
        raise


def _out(c: MCPConfig) -> MCPConfigOut:
    transport = "streamable_http" if c.transport == "http" else c.transport
    if transport == "stdio":
        command, args = _public_stdio_fields(c.command, c.args)
        endpoint = ""
    else:
        command, args = "", []
        endpoint = _public_endpoint(c.url)
    return MCPConfigOut(
        id=c.id,
        name=c.name,
        transport=transport,
        command=command,
        args=args,
        url=endpoint,
        env=_public_map(c.env),
        headers=_public_map(c.headers),
        enabled=c.enabled,
        created_at=c.created_at,
    )


@router.get("", response_model=list[MCPConfigOut])
def list_mcp(db: Session = Depends(get_tenant_db)):
    return [_out(c) for c in db.execute(select(MCPConfig).where(tenant_service.visible_clause(MCPConfig, db))).scalars().all()]


@router.post("", response_model=MCPConfigOut)
def create_mcp(payload: MCPConfigIn, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    _assert_transport_policy(payload)
    _assert_unique_name(db, payload.name)
    c = MCPConfig(tenant_id=tenant_service.current_tenant_id(db), **payload.model_dump())
    db.add(c)
    _commit_or_name_conflict(db, [payload.name])
    db.refresh(c)
    return _out(c)


@router.post("/import", response_model=MCPImportResultOut)
def import_standard_mcp(
    payload: MCPStandardImportIn,
    dry_run: bool = False,
    conflict_policy: Literal["error", "skip", "replace"] = "error",
    db: Session = Depends(get_tenant_db),
):
    """Validate and atomically import the common ``mcpServers`` JSON shape."""
    permission_service.require_tenant_permission(db, "manage")
    tenant_id = tenant_service.current_tenant_id(db)
    configs = payload.internal_configs()
    for config in configs:
        _assert_transport_policy(config)
    owned = list(
        db.execute(select(MCPConfig).where(MCPConfig.tenant_id == tenant_id)).scalars().all()
    )
    existing_by_name: dict[str, list[MCPConfig]] = {}
    for current in owned:
        existing_by_name.setdefault(normalize_mcp_name_key(current.name), []).append(current)

    conflicts = [
        config.name
        for config in configs
        if existing_by_name.get(normalize_mcp_name_key(config.name))
    ]
    ambiguous = [
        config.name
        for config in configs
        if len(existing_by_name.get(normalize_mcp_name_key(config.name)) or []) > 1
    ]
    if ambiguous:
        raise HTTPException(409, f"存在多个同名 MCP，无法安全导入：{'、'.join(ambiguous)}")
    if conflicts and conflict_policy == "error":
        raise HTTPException(
            409,
            f"以下 MCP 名称已存在：{'、'.join(conflicts)}；请选择跳过或替换策略",
        )

    items: list[MCPImportItemOut] = []
    touched: list[MCPConfig] = []
    created = replaced = skipped = 0
    for config in configs:
        matches = existing_by_name.get(normalize_mcp_name_key(config.name)) or []
        action: Literal["create", "replace", "skip"] = (
            "replace" if matches and conflict_policy == "replace"
            else "skip" if matches
            else "create"
        )
        public_command = _public_stdio_fields(config.command, config.args)[0]
        items.append(MCPImportItemOut(
            name=config.name,
            transport=config.transport,
            endpoint=public_command if config.transport == "stdio" else _public_endpoint(config.url),
            env_keys=sorted(config.env),
            header_keys=sorted(config.headers),
            enabled=config.enabled,
            action=action,
        ))
        if action == "skip":
            skipped += 1
            if not dry_run:
                touched.append(matches[0])
            continue
        if action == "replace":
            replaced += 1
            if dry_run:
                continue
            current = matches[0]
            for key, value in config.model_dump().items():
                setattr(current, key, value)
            connector_service.invalidate_connector_bindings(db, "mcp", current.id)
            touched.append(current)
            continue
        created += 1
        if dry_run:
            continue
        current = MCPConfig(tenant_id=tenant_id, **config.model_dump())
        db.add(current)
        touched.append(current)

    if not dry_run:
        _commit_or_name_conflict(
            db,
            [config.name for config in configs],
            exclude_ids={
                current.id
                for config in configs
                for current in (
                    existing_by_name.get(normalize_mcp_name_key(config.name)) or []
                )
            },
        )
        for current in touched:
            db.refresh(current)
    return MCPImportResultOut(
        dry_run=dry_run,
        created=created,
        replaced=replaced,
        skipped=skipped,
        items=items,
        configs=[] if dry_run else [_out(current) for current in touched],
    )


@router.put("/{mcp_id}", response_model=MCPConfigOut)
def update_mcp(mcp_id: str, payload: MCPConfigIn, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    _assert_transport_policy(payload)
    c = tenant_service.require_owned(db, MCPConfig, mcp_id, "MCP 不存在")
    _assert_unique_name(db, payload.name, exclude_id=c.id)
    values = payload.model_dump()
    if (
        payload.transport != "stdio"
        and c.url
        and payload.url == _public_endpoint(c.url)
    ):
        # Query values are write-only.  Re-submitting the public form preserves
        # a legacy endpoint instead of silently dropping its hidden query.
        values["url"] = c.url
    values["env"] = _merge_map(c.env, values.get("env"))
    values["headers"] = _merge_map(c.headers, values.get("headers"))
    for k, v in values.items():
        setattr(c, k, v)
    connector_service.invalidate_connector_bindings(db, "mcp", c.id)
    _commit_or_name_conflict(db, [payload.name], exclude_ids={c.id})
    db.refresh(c)
    return _out(c)


@router.delete("/{mcp_id}", response_model=Msg)
def delete_mcp(mcp_id: str, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    c = tenant_service.require_owned(db, MCPConfig, mcp_id, "MCP 不存在")
    try:
        connector_service.assert_connector_not_bound(db, "mcp", c.id)
    except connector_service.ConnectorBindingConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.delete(c)
    db.commit()
    return Msg(message="已删除")


@router.post("/{mcp_id}/test", response_model=Msg)
def test_mcp(mcp_id: str, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    c = tenant_service.require_owned(db, MCPConfig, mcp_id, "MCP 不存在")
    if not c.enabled:
        raise HTTPException(409, "MCP 当前已停用")
    if c.transport == "stdio" and not get_settings().allow_mcp_stdio:
        raise HTTPException(403, "当前部署未开启服务端 stdio MCP")
    ok, msg = mcp_service.test_connection(c)
    if not ok:
        raise HTTPException(400, msg)
    return Msg(ok=ok, message=msg)


@router.get("/{mcp_id}/tools", response_model=list[MCPToolInfo])
def mcp_tools(mcp_id: str, db: Session = Depends(get_tenant_db)):
    permission_service.require_tenant_permission(db, "manage")
    c = tenant_service.require_owned(db, MCPConfig, mcp_id, "MCP 不存在")
    if not c.enabled:
        raise HTTPException(409, "MCP 当前已停用")
    if c.transport == "stdio" and not get_settings().allow_mcp_stdio:
        raise HTTPException(403, "当前部署未开启服务端 stdio MCP")
    try:
        return mcp_service.list_tools(c)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"获取工具失败: {mcp_service.public_error(exc, c)}")
