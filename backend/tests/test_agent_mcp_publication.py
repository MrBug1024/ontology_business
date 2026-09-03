from __future__ import annotations

import asyncio
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import ANY, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import agent_mcp_server
from app.database import Base
from app.external_api_models import AgentMCPConversation, AgentMCPInvocation, AgentMCPService
from app.models import Agent, Conversation, Tenant, User
from app.routers import agent_mcp, agents as agents_router
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


def test_mcp_session_mapping_migration_grants_runtime_role() -> None:
    from importlib import import_module

    migration = import_module(
        "migrations.versions.20260903_09_persist_agent_mcp_session_conversations"
    )
    assert migration.down_revision == "20260903_08"
    statement = str(migration._runtime_role_statement("GRANT"))
    assert "agent_mcp_conversations" in statement
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in statement
    assert "TO ontology_app" in statement


class AgentMCPPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sqlite_dir = TemporaryDirectory()
        sqlite_path = Path(self._sqlite_dir.name) / "agent-mcp.sqlite"
        self.engine = create_engine(
            f"sqlite:///{sqlite_path.as_posix()}",
            connect_args={"check_same_thread": False},
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
        self._sqlite_dir.cleanup()

    def _add_published_service(
        self,
        *,
        service_id: str,
        tenant: Tenant | None = None,
        agent: Agent | None = None,
        owner: User | None = None,
        name: str | None = None,
    ) -> str:
        tenant = tenant or self.tenant
        agent = agent or self.agent
        owner = owner or self.owner
        raw_token, hashed, prefix, hint = agent_mcp_service.issue_token()
        db = self.Session()
        try:
            db.add(AgentMCPService(
                id=service_id,
                tenant_id=tenant.id,
                agent_id=agent.id,
                created_by_user_id=owner.id,
                execution_user_id=owner.id,
                name=name or service_id,
                name_key=(name or service_id).casefold(),
                token_hash=hashed,
                key_prefix=prefix,
                token_hint=hint,
                enabled=True,
                agent_config_hash=agent_mcp_service.agent_config_hash(agent),
                definition_hash="d" * 64,
                runtime_environment="prod",
            ))
            db.commit()
        finally:
            db.close()
        return raw_token

    def test_mcp_validation_uses_live_authoring_context_without_release(self) -> None:
        live_definition = SimpleNamespace(
            snapshot_id=None,
            release_id=None,
            definition_hash="l" * 64,
            environment="prod",
        )
        live_context = SimpleNamespace(
            runtime_definition=live_definition,
            scenario=SimpleNamespace(name="医保违规审计"),
        )
        db = self.Session()
        try:
            with (
                patch("app.routers.agents._agent", return_value=self.agent),
                patch(
                    "app.routers.agents._authorization_context",
                    return_value=live_context,
                ) as authorize,
                patch("app.routers.agents._agent_readiness_missing", return_value=[]),
            ):
                agent, context, missing = agent_mcp_service.validate_agent_runtime(
                    db, self.agent.id
                )
        finally:
            db.close()

        self.assertIs(agent, self.agent)
        self.assertIs(context, live_context)
        self.assertEqual(missing, [])
        authorize.assert_called_once_with(
            ANY,
            self.agent,
            definition_mode="authoring",
        )

    def test_mcp_validation_accepts_routable_default_llm_without_explicit_binding(self) -> None:
        candidate = SimpleNamespace(
            id="agent-default-llm",
            scenario_id="scenario-default-llm",
            data_source_ids=["source-default-llm"],
            capability_scope={},
            llm_config_id=None,
        )
        live_context = SimpleNamespace(
            runtime_definition=SimpleNamespace(mappings={"mapping-default-llm": {}}),
            scenario=SimpleNamespace(name="默认模型场景"),
            entities=[SimpleNamespace(id="entity-default-llm")],
            data_sources=[SimpleNamespace(id="source-default-llm")],
            mappings=[SimpleNamespace(id="mapping-default-llm")],
            build_tools=lambda: [{"type": "function"}],
        )
        db = self.Session()
        db.info["tenant_id"] = self.tenant.id
        try:
            with (
                patch("app.routers.agents._agent", return_value=candidate),
                patch(
                    "app.routers.agents._authorization_context",
                    return_value=live_context,
                ),
                patch.object(
                    agents_router.llm_service,
                    "routable_configs",
                    return_value=[SimpleNamespace(id="default-tool-llm")],
                ) as routable,
            ):
                _agent, context, missing = agent_mcp_service.validate_agent_runtime(
                    db,
                    candidate.id,
                )
        finally:
            db.close()

        self.assertIs(context, live_context)
        self.assertEqual(missing, [])
        routable.assert_called_once_with(db, "tool")

    def test_mcp_validation_marks_no_routable_default_llm_as_not_ready(self) -> None:
        candidate = SimpleNamespace(
            id="agent-no-default-llm",
            scenario_id="scenario-no-default-llm",
            data_source_ids=[],
            capability_scope={},
            llm_config_id=None,
        )
        live_context = SimpleNamespace(
            runtime_definition=SimpleNamespace(mappings={"mapping-no-default-llm": {}}),
            scenario=SimpleNamespace(name="无默认模型场景"),
            entities=[SimpleNamespace(id="entity-no-default-llm")],
            data_sources=[SimpleNamespace(id="source-no-default-llm")],
            mappings=[SimpleNamespace(id="mapping-no-default-llm")],
            build_tools=lambda: [],
        )
        db = self.Session()
        db.info["tenant_id"] = self.tenant.id
        try:
            with (
                patch("app.routers.agents._agent", return_value=candidate),
                patch(
                    "app.routers.agents._authorization_context",
                    return_value=live_context,
                ),
                patch.object(
                    agents_router.llm_service,
                    "routable_configs",
                    return_value=[],
                ) as routable,
            ):
                _agent, _context, missing = agent_mcp_service.validate_agent_runtime(
                    db,
                    candidate.id,
                )
        finally:
            db.close()

        self.assertIn("对话模型", missing)
        routable.assert_called_once_with(db, "chat")

    def test_live_mcp_candidate_and_publication_do_not_need_release(self) -> None:
        live_definition = SimpleNamespace(
            snapshot_id=None,
            release_id=None,
            definition_hash="l" * 64,
            environment="prod",
        )
        live_context = SimpleNamespace(
            runtime_definition=live_definition,
            scenario=SimpleNamespace(name="医保违规审计"),
        )

        def validate(db, agent_id, writable=False):
            return db.get(Agent, agent_id), live_context, []

        def runtime_status(db, service):
            return db.get(Agent, service.agent_id), live_context, [], False

        with (
            patch.object(agent_mcp_service, "validate_agent_runtime", validate),
            patch.object(agent_mcp_service, "service_runtime_status", runtime_status),
        ):
            candidates = self.client.get("/api/agent-mcp-services/candidates")
            self.assertEqual(candidates.status_code, 200, candidates.text)
            self.assertEqual(candidates.json(), [{
                "id": self.agent.id,
                "name": self.agent.name,
                "scenario_name": "医保违规审计",
                "ready": True,
                "missing": [],
            }])
            created = self.client.post(
                "/api/agent-mcp-services",
                json={"name": "live-mcp", "agent_id": self.agent.id, "expires_in_days": 30},
            )

        self.assertEqual(created.status_code, 201, created.text)
        self.assertTrue(created.json()["ready"])
        self.assertEqual(created.json()["definition_hash"], "l" * 64)
        db = self.Session()
        try:
            service = db.execute(select(AgentMCPService)).scalar_one()
            self.assertIsNone(service.definition_snapshot_id)
            self.assertIsNone(service.release_id)
        finally:
            db.close()

    def test_live_mcp_status_does_not_block_after_agent_or_definition_edits(self) -> None:
        self._add_published_service(service_id="service-live-status")
        live_context = SimpleNamespace(
            runtime_definition=SimpleNamespace(
                snapshot_id=None,
                release_id=None,
                definition_hash="e" * 64,
                environment="prod",
            ),
            scenario=SimpleNamespace(name="医保违规审计"),
        )
        db = self.Session()
        try:
            service = db.get(AgentMCPService, "service-live-status")
            assert service is not None
            with patch.object(
                agent_mcp_service,
                "validate_agent_runtime",
                return_value=(self.agent, live_context, []),
            ):
                _agent, context, missing, stale = agent_mcp_service.service_runtime_status(
                    db, service
                )
        finally:
            db.close()

        self.assertIs(context, live_context)
        self.assertEqual(missing, [])
        self.assertFalse(stale)

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
        raw_token = self._add_published_service(
            service_id="service-agent-mcp",
            name="医保违规审计助手",
        )
        received_session_ids: list[str] = []

        def invoke(_service_id: str, **kwargs):
            received_session_ids.append(kwargs["external_session_id"])
            return {"answer": "审计完成", "conversation_id": "conversation-test"}

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
                            repeated = await session.call_tool(
                                "invoke_agent", {"message": "继续审计"}
                            )
                            self.assertFalse(repeated.isError)
                            self.assertEqual(repeated.structuredContent["answer"], "审计完成")

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(
                agent_mcp_service,
                "invoke_published_agent",
                side_effect=invoke,
            ),
        ):
            asyncio.run(exercise())
        self.assertEqual(len(received_session_ids), 2)
        self.assertTrue(received_session_ids[0])
        self.assertEqual(received_session_ids[0], received_session_ids[1])

    def test_external_mcp_session_reuses_one_conversation_and_is_service_scoped(self) -> None:
        self._add_published_service(service_id="service-session-a")
        other_tenant = Tenant(id="tenant-agent-mcp-other", name="另一组织")
        other_owner = User(
            id="owner-agent-mcp-other",
            tenant_id=other_tenant.id,
            email="owner-agent-mcp-other@example.test",
            password_hash="test-only",
            status="active",
        )
        other_agent = Agent(
            id="agent-mcp-other",
            tenant_id=other_tenant.id,
            name="隔离 Agent",
            capability_scope={},
        )
        db = self.Session()
        try:
            db.add_all([other_tenant, other_owner, other_agent])
            db.commit()
            permission_service.ensure_organization(
                db, other_tenant.id, owner_user_id=other_owner.id
            )
            db.commit()
        finally:
            db.close()
        self._add_published_service(
            service_id="service-session-b",
            tenant=other_tenant,
            agent=other_agent,
            owner=other_owner,
        )
        observed_conversations: list[str] = []

        def runtime_status(db, service):
            return db.get(Agent, service.agent_id), self.context, [], False

        def invoke(
            _agent_id: str,
            *,
            message: str,
            conversation_id: str | None,
            db,
            runtime_context,
        ):
            self.assertIsNotNone(conversation_id)
            conversation = db.get(Conversation, conversation_id)
            self.assertIsNotNone(conversation)
            self.assertEqual(conversation.created_by_user_id, db.info["user_id"])
            observed_conversations.append(conversation_id)
            return {
                "answer": message,
                "conversation_id": conversation_id,
                "trace_id": "trace-session",
                "tool_calls": [],
                "tool_results": [],
            }

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(agent_mcp_service, "service_runtime_status", side_effect=runtime_status),
            patch("app.routers.agents.invoke_agent_once", side_effect=invoke),
        ):
            first = agent_mcp_service.invoke_published_agent(
                "service-session-a",
                message="第一轮",
                conversation_id=None,
                external_session_id="third-party-session-1",
            )
            second = agent_mcp_service.invoke_published_agent(
                "service-session-a",
                message="第二轮",
                conversation_id=None,
                external_session_id="third-party-session-1",
            )
            other_service = agent_mcp_service.invoke_published_agent(
                "service-session-b",
                message="隔离会话",
                conversation_id=None,
                external_session_id="third-party-session-1",
            )

        self.assertEqual(first["conversation_id"], second["conversation_id"])
        self.assertNotEqual(first["conversation_id"], other_service["conversation_id"])
        self.assertEqual(observed_conversations[:2], [first["conversation_id"], second["conversation_id"]])
        db = self.Session()
        try:
            mappings = db.execute(select(AgentMCPConversation)).scalars().all()
            self.assertEqual(len(mappings), 2)
            self.assertEqual(
                {mapping.service_id for mapping in mappings},
                {"service-session-a", "service-session-b"},
            )
            self.assertEqual(
                {mapping.tenant_id for mapping in mappings},
                {self.tenant.id, other_tenant.id},
            )
            self.assertTrue(
                all(mapping.external_session_hash != "third-party-session-1" for mapping in mappings)
            )
            self.assertEqual(
                len(db.execute(select(AgentMCPInvocation)).scalars().all()),
                3,
            )
            self.assertEqual(
                len(db.execute(select(Conversation)).scalars().all()),
                2,
            )
        finally:
            db.close()

    def test_external_mcp_mapping_persists_conversation_before_foreign_key_insert(self) -> None:
        self._add_published_service(service_id="service-conversation-fk-order")
        db = self.Session()
        try:
            # SQLite leaves FK checks disabled by default.  Enabling them here
            # reproduces PostgreSQL's immediate FK enforcement.
            db.connection().exec_driver_sql("PRAGMA foreign_keys = ON")
            service = db.get(AgentMCPService, "service-conversation-fk-order")
            assert service is not None
            binding = agent_mcp_service._conversation_for_external_session(
                db,
                service,
                message="外部 MCP 会话初始化",
                conversation_id=None,
                external_session_id="mcp-session-fk-order",
                agents=SimpleNamespace(),
            )
            mapping = db.get(AgentMCPConversation, binding.mapping_id)
            conversation = db.get(Conversation, binding.conversation_id)
        finally:
            db.close()

        self.assertIsNotNone(mapping)
        self.assertIsNotNone(conversation)
        assert mapping is not None
        self.assertEqual(mapping.conversation_id, binding.conversation_id)

    def test_mcp_text_confirmation_reuses_mapped_conversation_and_execution_context(self) -> None:
        self._add_published_service(service_id="service-text-confirmation")
        observed_conversations: list[str] = []

        def runtime_status(db, service):
            agent = db.get(Agent, service.agent_id)
            assert agent is not None
            context = SimpleNamespace(
                db=db,
                agent=agent,
                llm=None,
                runtime_definition=self.context.runtime_definition,
                build_tools=lambda: [],
            )
            return agent, context, [], False

        def confirm_text_reply(db, *, agent, conversation, text):
            self.assertEqual(agent.id, self.agent.id)
            self.assertEqual(text, "确认执行")
            observed_conversations.append(conversation.id)
            return {"status": "confirmed", "message": "已完成文本确认。"}

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(agent_mcp_service, "service_runtime_status", side_effect=runtime_status),
            patch("app.routers.agents._agent_readiness_missing", return_value=[]),
            patch("app.routers.agents.llm_service.routable_configs", return_value=[SimpleNamespace()]),
            patch(
                "app.routers.agents.agent_confirmation_service.confirm_text_reply",
                side_effect=confirm_text_reply,
            ),
            patch(
                "app.routers.agents.agent_engine.run_agent",
                side_effect=AssertionError("文本确认不应进入模型工具循环"),
            ),
        ):
            first = agent_mcp_service.invoke_published_agent(
                "service-text-confirmation",
                message="确认执行",
                conversation_id=None,
                external_session_id="third-party-confirmation-session",
                external_request_id="rpc-confirm-1",
            )
            second = agent_mcp_service.invoke_published_agent(
                "service-text-confirmation",
                message="确认执行",
                conversation_id=None,
                external_session_id="third-party-confirmation-session",
                external_request_id="rpc-confirm-1",
            )

        self.assertEqual(first["conversation_id"], second["conversation_id"])
        # A transport retry replays the completed confirmation envelope.  It
        # must not confirm a second time or append another assistant message.
        self.assertEqual(observed_conversations, [first["conversation_id"]])
        for result in (first, second):
            self.assertEqual(result["answer"], "已完成文本确认。")
            self.assertEqual(result["confirmation"]["status"], "confirmed")
            self.assertEqual(result["citations"], [])
            self.assertEqual(result["tool_calls"], [])
            self.assertEqual(result["tool_results"], [])
            self.assertEqual(result["runtime"]["release_id"], "release-agent-mcp")

    def test_server_issued_session_is_bound_to_one_published_service(self) -> None:
        issued = agent_mcp_server.AgentMCPBearerMiddleware._new_session_id(
            "service-session-a"
        )
        self.assertTrue(
            agent_mcp_server.AgentMCPBearerMiddleware._valid_session_id(
                issued, "service-session-a"
            )
        )
        self.assertFalse(
            agent_mcp_server.AgentMCPBearerMiddleware._valid_session_id(
                issued, "service-session-b"
            )
        )
        self.assertFalse(
            agent_mcp_server.AgentMCPBearerMiddleware._valid_session_id(
                "third-party-session-1", "service-session-a"
            )
        )

    def test_same_session_serializes_turns_and_replays_one_request(self) -> None:
        self._add_published_service(service_id="service-serialized-session")
        external_session_id = "signed-session-test"
        db = self.Session()
        try:
            conversation = Conversation(
                id="conversation-serialized-session",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                title="外部会话",
            )
            mapping = AgentMCPConversation(
                id="mapping-serialized-session",
                service_id="service-serialized-session",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                external_session_hash=agent_mcp_service.external_session_hash(
                    external_session_id
                ),
                conversation_id=conversation.id,
            )
            db.add_all([conversation, mapping])
            db.commit()
        finally:
            db.close()

        first_started = threading.Event()
        release_first = threading.Event()
        observed: list[tuple[str, str, str]] = []
        results: dict[str, dict] = {}
        errors: dict[str, BaseException] = {}

        def runtime_status(db, service):
            return db.get(Agent, service.agent_id), self.context, [], False

        def invoke(
            _agent_id: str,
            *,
            message: str,
            conversation_id: str | None,
            db,
            runtime_context,
        ):
            self.assertEqual(conversation_id, "conversation-serialized-session")
            request_hash = str(db.info.get("agent_mcp_turn_request_hash") or "")
            self.assertTrue(request_hash)
            observed.append((message, str(conversation_id), request_hash))
            if message == "first":
                first_started.set()
                self.assertTrue(release_first.wait(timeout=5))
            return {
                "answer": message,
                "conversation_id": conversation_id,
                "trace_id": f"trace-{message}",
                "tool_calls": [],
                "tool_results": [],
            }

        def call(key: str, message: str, request_id: str) -> None:
            try:
                results[key] = agent_mcp_service.invoke_published_agent(
                    "service-serialized-session",
                    message=message,
                    conversation_id=None,
                    external_session_id=external_session_id,
                    external_request_id=request_id,
                )
            except BaseException as exc:  # test thread must report failures.
                errors[key] = exc

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(agent_mcp_service, "service_runtime_status", side_effect=runtime_status),
            patch("app.routers.agents.invoke_agent_once", side_effect=invoke),
        ):
            first = threading.Thread(target=call, args=("first", "first", "rpc-1"))
            first.start()
            self.assertTrue(first_started.wait(timeout=5))
            retry = threading.Thread(target=call, args=("retry", "first", "rpc-1"))
            retry.start()
            second = threading.Thread(target=call, args=("second", "second", "rpc-2"))
            second.start()
            # The second worker may resolve the mapping, but it cannot read
            # history/enter Agent execution while the first lease is active.
            time.sleep(0.2)
            self.assertEqual([item[0] for item in observed], ["first"])
            release_first.set()
            first.join(timeout=10)
            retry.join(timeout=10)
            second.join(timeout=10)
            self.assertFalse(first.is_alive())
            self.assertFalse(retry.is_alive())
            self.assertFalse(second.is_alive())

            replay = agent_mcp_service.invoke_published_agent(
                "service-serialized-session",
                message="first",
                conversation_id=None,
                external_session_id=external_session_id,
                external_request_id="rpc-1",
            )

        self.assertFalse(errors, errors)
        self.assertEqual([item[0] for item in observed], ["first", "second"])
        self.assertEqual(results["first"]["answer"], "first")
        self.assertEqual(results["retry"], results["first"])
        self.assertEqual(results["second"]["answer"], "second")
        self.assertEqual(replay, results["first"])
        # The one model invocation represents the non-confirmed side-effect
        # boundary: a concurrent/transport retry of rpc-1 never enters it.
        self.assertEqual(sum(1 for item in observed if item[0] == "first"), 1)

        db = self.Session()
        try:
            invocations = db.execute(
                select(AgentMCPInvocation).where(
                    AgentMCPInvocation.mcp_conversation_id == "mapping-serialized-session"
                )
            ).scalars().all()
            self.assertEqual(len(invocations), 2)
            first_invocation = next(
                item
                for item in invocations
                if item.external_request_hash
                == agent_mcp_service.external_request_hash("rpc-1")
            )
            self.assertEqual(first_invocation.status, "succeeded")
            self.assertEqual(first_invocation.result["answer"], "first")
        finally:
            db.close()

    def test_expired_turn_lease_is_recovered_and_late_worker_is_fenced(self) -> None:
        self._add_published_service(service_id="service-expired-lease")
        db = self.Session()
        try:
            conversation = Conversation(
                id="conversation-expired-lease",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                title="外部会话",
            )
            mapping = AgentMCPConversation(
                id="mapping-expired-lease",
                service_id="service-expired-lease",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                external_session_hash=agent_mcp_service.external_session_hash(
                    "expired-session"
                ),
                conversation_id=conversation.id,
            )
            db.add_all([conversation, mapping])
            db.commit()
            service = db.get(AgentMCPService, "service-expired-lease")
            assert service is not None
            binding = agent_mcp_service.MCPConversationBinding(
                mapping_id=mapping.id,
                conversation_id=conversation.id,
                external_session_hash=mapping.external_session_hash,
            )
            first = agent_mcp_service._claim_mcp_turn(
                db,
                service=service,
                binding=binding,
                request_hash=agent_mcp_service.external_request_hash("rpc-stale"),
                input_hash="a" * 64,
                request_id="internal-stale",
            )
            assert first.lease is not None
            mapping = db.get(AgentMCPConversation, mapping.id)
            assert mapping is not None
            mapping.turn_lease_expires_at = agent_mcp_service.utc_now() - timedelta(seconds=1)
            db.commit()

            replacement = agent_mcp_service._claim_mcp_turn(
                db,
                service=service,
                binding=binding,
                request_hash=agent_mcp_service.external_request_hash("rpc-replacement"),
                input_hash="b" * 64,
                request_id="internal-replacement",
            )
            assert replacement.lease is not None
            self.assertGreater(
                replacement.lease.generation,
                first.lease.generation,
            )
            with self.assertRaises(agent_mcp_service.AgentMCPTurnLeaseLostError):
                agent_mcp_service._complete_mcp_turn(
                    db,
                    lease=first.lease,
                    result={"answer": "late", "conversation_id": conversation.id},
                    latency_ms=1,
                )
            agent_mcp_service._complete_mcp_turn(
                db,
                lease=replacement.lease,
                result={"answer": "replacement", "conversation_id": conversation.id},
                latency_ms=1,
            )
            db.expire_all()
            final_mapping = db.get(AgentMCPConversation, mapping.id)
            assert final_mapping is not None
            self.assertEqual(final_mapping.turn_lease_token, "")
            replacement_invocation = db.execute(
                select(AgentMCPInvocation).where(
                    AgentMCPInvocation.external_request_hash
                    == agent_mcp_service.external_request_hash("rpc-replacement")
                )
            ).scalar_one()
            self.assertEqual(replacement_invocation.status, "succeeded")
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
