from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.routers import assistant
from app.schemas import AssistantChatRequest
from app.models import MCPConfig, Skill
from isolated_postgresql import isolated_postgresql, seed_workspace, tenant_session


class SelectedResourceSession:
    def __init__(self):
        self.info = {"tenant_id": "tenant"}

    def execute(self, _statement):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(
                all=lambda: [SimpleNamespace(id="available", name="Available resource")]
            )
        )


@pytest.mark.parametrize("field_name", ("skill_ids", "mcp_ids"))
def test_explicit_assistant_reference_selection_fails_closed_when_any_id_is_unavailable(field_name, monkeypatch):
    monkeypatch.setattr(assistant.permission_service, "require_principal", lambda _db: object())
    monkeypatch.setattr(assistant.permission_service, "require_tenant_permission", lambda *_args: None)
    payload = AssistantChatRequest(
        message="Use the selected reference resource",
        **{field_name: ["available", "unavailable"]},
    )

    with pytest.raises(HTTPException) as error:
        assistant._assistant_capability_context(SelectedResourceSession(), payload)

    assert error.value.status_code == 409
    assert error.value.detail == assistant._SELECTED_ASSISTANT_REFERENCE_UNAVAILABLE


def _reference_resource(model, *, tenant_id, name, enabled):
    if model is Skill:
        return Skill(
            tenant_id=tenant_id,
            name=name,
            path="/trusted/assistant-resource-selection-test",
            enabled=enabled,
        )
    return MCPConfig(
        tenant_id=tenant_id,
        name=name,
        transport="streamable_http",
        url="https://assistant-resource-selection.invalid/mcp",
        enabled=enabled,
    )


@pytest.mark.parametrize(("field_name", "model"), (("skill_ids", Skill), ("mcp_ids", MCPConfig)))
@pytest.mark.parametrize("state", ("disabled", "foreign"))
def test_explicit_assistant_reference_selection_rejects_disabled_or_foreign_rows(
    isolated_postgresql, field_name, model, state
):
    workspace = seed_workspace(isolated_postgresql.admin_engine)
    foreign = seed_workspace(isolated_postgresql.admin_engine)
    tenant_id = foreign["tenant_id"] if state == "foreign" else workspace["tenant_id"]
    enabled = state != "disabled"
    with Session(isolated_postgresql.admin_engine) as db:
        resource = _reference_resource(
            model,
            tenant_id=tenant_id,
            name=f"Assistant resource {field_name} {state} {workspace['tenant_id']}",
            enabled=enabled,
        )
        db.add(resource)
        db.commit()
        resource_id = resource.id

    with tenant_session(isolated_postgresql.runtime_engine, workspace) as db:
        payload = AssistantChatRequest(
            message="Use the selected reference resource",
            **{field_name: [resource_id]},
        )
        with pytest.raises(HTTPException) as error:
            assistant._assistant_capability_context(db, payload)

    assert error.value.status_code == 409
    assert error.value.detail == assistant._SELECTED_ASSISTANT_REFERENCE_UNAVAILABLE
