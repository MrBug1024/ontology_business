"""Read an existing receipt without widening the validation Agent's scope."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from ..models import CapabilityInvocation
from . import capability_application_service
from .capability_contracts import Actor


TOOL_NAME = 'get_capability_receipt'


class ReceiptQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    invocation_id: str = Field(min_length=32, max_length=32, pattern=r'^[a-zA-Z0-9_-]+$')


def tool_definition() -> dict[str, Any]:
    return {'type': 'function', 'function': {'name': TOOL_NAME,
        'description': 'Read the latest authorized receipt for a previous invocation. '
            'Queued workflows are not business completion. Read their receipt again to '
            'obtain terminal status and structured results; never invoke again merely to poll.',
        'parameters': ReceiptQuery.model_json_schema()}}


def read(db: Session, actor: Actor, *, agent_id: str, scenario_id: str,
         capability_refs: Mapping, args: Mapping[str, Any]) -> dict[str, Any]:
    try:
        query = ReceiptQuery.model_validate(args)
    except ValidationError:
        raise capability_application_service.CapabilityApplicationError(
            'invalid_receipt_request', '回执查询参数无效', status_code=400) from None
    invocation = db.get(CapabilityInvocation, query.invocation_id)
    if (invocation is None or invocation.tenant_id != actor.tenant_id
            or invocation.requested_by_user_id != actor.user_id
            or invocation.agent_id != agent_id or invocation.scenario_id != scenario_id
            or (invocation.capability_kind, invocation.capability_key) not in capability_refs):
        raise capability_application_service.CapabilityApplicationError(
            'invocation_not_found', '执行回执不存在或无权访问', status_code=404)
    # The shared reader rechecks principal, current ACL and pinned historical identity.
    return capability_application_service.get_receipt(db, actor, query.invocation_id)
