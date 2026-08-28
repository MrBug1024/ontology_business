from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_mcp_server
from app.database import Base
from app.external_api_models import AgentMCPService
from app.models import Agent, Tenant, User
from app.routers import agent_mcp
from app.services import agent_mcp_service, permission_service
from app.services.auth_service import get_tenant_db


def test_runtime_grant_migration_is_chained_after_agent_mcp_tables() -> None:
    from importlib import import_module

    migration = import_module(
        "migrations.versions.20260828_06_grant_agent_mcp_runtime_access"
    )
    assert migration.down_revision == "20260828_05"
    assert migration.AGENT_MCP_TABLES == (
        "agent_mcp_services",
        "agent_mcp_invocations",
    )
    statement = str(migration._runtime_role_statement("GRANT"))
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in statement
    assert "TO ontology_app" in statement
    downgrade_statement = str(migration._runtime_role_statement("REVOKE"))
    assert "REVOKE SELECT, INSERT, UPDATE, DELETE" in downgrade_statement
    assert "FROM ontology_app" in downgrade_statement


class AgentMCPPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        Base.metadata.create_all(self.engine)
        db = self.Session()
        try:
            self.tenant = Tenant(id="tenant-agent-mcp", name="Agent MCP 组织")
            self.owner = User(
                id="owner-agent-mcp",
                tenant_id=self.tenant.id,
                email="owner-agent-mcp@example.test",
                password_hash="test-only",
                status="active",
            )
            self.viewer = User(
                id="viewer-agent-mcp",
                tenant_id=self.tenant.id,
                email="viewer-agent-mcp@example.test",
                password_hash="test-only",
                status="active",
            )
            self.agent = Agent(
                id="agent-mcp-target",
                tenant_id=self.tenant.id,
                name="医保违规审计助手",
                capability_scope={},
            )
            db.add_all([self.tenant, self.owner, self.viewer, self.agent])
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.viewer.id, role_key="viewer"
            )
            db.commit()
        finally:
            db.close()

        self.current_user = self.owner
        self.app = FastAPI()
        self.app.include_router(agent_mcp.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["tenant_id"] = self.tenant.id
            request_db.info["user_id"] = self.current_user.id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)
        definition = SimpleNamespace(
            snapshot_id="snapshot-agent-mcp",
            release_id="release-agent-mcp",
            definition_hash="d" * 64,
            environment="prod",
        )
        self.context = SimpleNamespace(
            runtime_definition=definition,
            scenario=SimpleNamespace(name="医保违规审计"),
        )

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def test_create_lists_only_safe_metadata_and_rotation_revokes_old_token(self) -> None:
        def validate(db, agent_id, writable=False):
            return db.get(Agent, agent_id), self.context, []

        def runtime_status(db, service):
            return db.get(Agent, service.agent_id), self.context, [], False

        with ExitStack() as stack:
            stack.enter_context(patch.object(agent_mcp_service, "validate_agent_runtime", validate))
            stack.enter_context(patch.object(agent_mcp_service, "service_runtime_status", runtime_status))
            created = self.client.post(
                "/api/agent-mcp-services",
                json={"name": "医保违规审计助手", "agent_id": self.agent.id, "expires_in_days": 30},
            )
            self.assertEqual(created.status_code, 201, created.text)
            payload = created.json()
            raw_token = payload["token"]
            self.assertTrue(raw_token.startswith("agt_sk_"))
            self.assertIn(raw_token, payload["config_json"])
            self.assertEqual(payload["config"]["mcpServers"]["医保违规审计助手"]["url"], "http://testserver/mcp")

            listed = self.client.get("/api/agent-mcp-services")
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertNotIn("token", listed.json()[0])
            self.assertNotIn(raw_token, listed.text)

            self.current_user = self.viewer
            viewer_list = self.client.get("/api/agent-mcp-services")
            self.assertEqual(viewer_list.status_code, 200, viewer_list.text)
            denied = self.client.post(
                "/api/agent-mcp-services",
                json={"name": "只读越权", "agent_id": self.agent.id, "expires_in_days": 30},
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.current_user = self.owner

            with patch.object(agent_mcp_service, "SessionLocal", self.Session):
                self.assertIsNotNone(agent_mcp_service.authenticate_token(raw_token))

            rotated = self.client.post(
                f"/api/agent-mcp-services/{payload['id']}/rotate-token",
                json={"expires_in_days": 60},
            )
            self.assertEqual(rotated.status_code, 200, rotated.text)
            new_token = rotated.json()["token"]
            self.assertNotEqual(new_token, raw_token)
            with patch.object(agent_mcp_service, "SessionLocal", self.Session):
                self.assertIsNone(agent_mcp_service.authenticate_token(raw_token))
                self.assertIsNotNone(agent_mcp_service.authenticate_token(new_token))

        db = self.Session()
        try:
            row = db.execute(select(AgentMCPService)).scalar_one()
            self.assertNotEqual(row.token_hash, new_token)
            self.assertNotIn(new_token, row.token_hash)
        finally:
            db.close()

    def test_official_client_can_initialize_list_and_call_with_bound_token(self) -> None:
        raw_token, hashed, prefix, hint = agent_mcp_service.issue_token()
        db = self.Session()
        try:
            db.add(AgentMCPService(
                id="service-agent-mcp",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                execution_user_id=self.owner.id,
                name="医保违规审计助手",
                name_key="医保违规审计助手",
                token_hash=hashed,
                key_prefix=prefix,
                token_hint=hint,
                enabled=True,
                agent_config_hash=agent_mcp_service.agent_config_hash(self.agent),
                definition_hash="d" * 64,
                runtime_environment="prod",
            ))
            db.commit()
        finally:
            db.close()

        async def exercise() -> None:
            transport = httpx.ASGITransport(app=agent_mcp_server.mcp_app)
            async with agent_mcp_server.mcp_server.session_manager.run():
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    headers={"Authorization": f"Bearer {raw_token}"},
                ) as http_client:
                    async with streamable_http_client(
                        "http://testserver/mcp", http_client=http_client
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            initialized = await session.initialize()
                            self.assertEqual(initialized.serverInfo.name, "Ontology Platform Agent Gateway")
                            tools = await session.list_tools()
                            self.assertEqual([tool.name for tool in tools.tools], ["invoke_agent"])
                            result = await session.call_tool(
                                "invoke_agent", {"message": "执行医保违规审计"}
                            )
                            self.assertFalse(result.isError)
                            self.assertEqual(result.structuredContent["answer"], "审计完成")

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(
                agent_mcp_service,
                "invoke_published_agent",
                return_value={"answer": "审计完成", "conversation_id": "conversation-test"},
            ),
        ):
            asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
