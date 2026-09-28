from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import DataSource, LLMConfig, MCPConfig, User
from app.routers import llm_configs, mcp
from app.schemas import LLMConfigIn, MCPConfigIn
from app.services import connector_service
from app.services import permission_service


class ConnectorDb:
    def __init__(self, model, connector):
        self.model = model
        self.connector = connector

    def get(self, model, connector_id):
        assert model is self.model
        assert connector_id == self.connector.id
        return self.connector


@pytest.mark.parametrize(("kind", "model"), [("llm", LLMConfig), ("mcp", MCPConfig)])
def test_shared_runtime_connectors_can_bind_across_tenants(kind, model):
    connector = SimpleNamespace(
        id="shared-config",
        tenant_id="tenant-owner",
        is_public=True,
        enabled=True,
    )
    scenario = SimpleNamespace(id="scenario-consumer", tenant_id="tenant-consumer")

    resolved = connector_service._resolve_connector(
        ConnectorDb(model, connector), kind, connector.id, scenario
    )

    assert resolved is connector


def test_private_mcp_cannot_bind_across_tenants():
    connector = SimpleNamespace(
        id="private-config",
        tenant_id="tenant-owner",
        is_public=False,
        enabled=True,
    )
    scenario = SimpleNamespace(id="scenario-consumer", tenant_id="tenant-consumer")

    with pytest.raises(connector_service.ConnectorBindingConflictError):
        connector_service._resolve_connector(
            ConnectorDb(MCPConfig, connector), "mcp", connector.id, scenario
        )


def test_public_data_source_does_not_become_a_cross_tenant_runtime_connector():
    connector = SimpleNamespace(
        id="public-source",
        tenant_id="tenant-owner",
        is_public=True,
        enabled=True,
        type="postgres",
    )
    scenario = SimpleNamespace(id="scenario-consumer", tenant_id="tenant-consumer")

    with pytest.raises(connector_service.ConnectorBindingConflictError):
        connector_service._resolve_connector(
            ConnectorDb(DataSource, connector), "data_source", connector.id, scenario
        )


class AccountDb:
    def get(self, model, _user_id, **_options):
        assert model is User
        return SimpleNamespace(status="active", system_role="user")


@pytest.mark.parametrize(
    ("create_config", "payload"),
    [
        (llm_configs.create_llm, LLMConfigIn(name="shared-model", is_public=True)),
        (
            mcp.create_mcp,
            MCPConfigIn(name="shared-mcp", transport="stdio", enabled=False, is_public=True),
        ),
    ],
)
def test_workspace_admin_cannot_create_shared_config(create_config, payload, monkeypatch):
    monkeypatch.setattr(permission_service, "require_tenant_permission", lambda *_args: None)
    monkeypatch.setattr(
        permission_service,
        "require_principal",
        lambda *_args: SimpleNamespace(user_id="workspace-admin"),
    )

    with pytest.raises(HTTPException) as error:
        create_config(payload, AccountDb())

    assert error.value.status_code == 403
