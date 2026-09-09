"""Resolve semantic query roles without coupling their names to SQL aliases."""
from __future__ import annotations

import re
from typing import Any, Mapping


ROLE_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 80, "pattern": "^[a-z][a-z0-9_]*$"}


def role_key(request: Mapping[str, Any], entity_id: str) -> str:
    role = request.get("role")
    if role is None:
        return entity_id
    if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", role):
        raise ValueError("查询角色必须为 1 到 80 位的小写稳定标识")
    return role


def resolve_role(plans: Mapping[str, Any], entity_id: str, request: Mapping[str, Any]) -> str:
    if "role" in request:
        key = role_key(request, entity_id)
        if key not in plans or str(plans[key].entity.id) != entity_id:
            raise ValueError("查询角色与所引用的对象类型不一致")
        return key
    candidates = [key for key, plan in plans.items() if str(plan.entity.id) == entity_id]
    if len(candidates) != 1:
        raise ValueError("对象未参与查询或存在多个查询角色，请明确指定 role")
    return candidates[0]
