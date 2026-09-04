from __future__ import annotations

import asyncio
import hashlib
import json
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
from sqlalchemy import create_engine, select, text
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


def test_mcp_conversation_canonicalization_migration_is_chained() -> None:
    from importlib import import_module

    migration = import_module(
        "migrations.versions.20260904_13_canonicalize_agent_mcp_conversations"
    )
    assert migration.down_revision == "20260904_12"
    statement = str(migration._canonicalize_statement())
    assert "ROW_NUMBER() OVER" in statement
    assert "legacy_conversation_id = mapping.conversation_id" in statement
    assert "binding_kind = 'legacy_duplicate'" in statement
    assert "turn_lease_token = ''" in statement
    interrupted = str(migration._fail_interrupted_duplicate_invocations_statement())
    assert "AgentMCPMigrationInterrupted" in interrupted
    assert "invocation.status = 'running'" in interrupted
    restored = str(migration._restore_legacy_conversations_statement())
    assert "conversation_id = legacy_conversation_id" in restored


def test_mcp_conversation_canonicalization_preserves_duplicate_audit_rows() -> None:
    from importlib import import_module

    migration = import_module(
        "migrations.versions.20260904_13_canonicalize_agent_mcp_conversations"
    )
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE agent_mcp_conversations (
                id TEXT PRIMARY KEY,
                service_id TEXT NOT NULL,
                conversation_id TEXT,
                legacy_conversation_id TEXT,
                binding_kind TEXT NOT NULL,
                turn_lease_token TEXT NOT NULL,
                turn_lease_expires_at TEXT,
                turn_lease_deadline_at TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        connection.exec_driver_sql(
            """CREATE TABLE agent_mcp_invocations (
                id TEXT PRIMARY KEY,
                mcp_conversation_id TEXT,
                status TEXT NOT NULL,
                error_code TEXT NOT NULL,
                error_message TEXT NOT NULL,
                completed_at TEXT
            )"""
        )
        connection.execute(
            text(
                """INSERT INTO agent_mcp_conversations
                (id, service_id, conversation_id, legacy_conversation_id,
                 binding_kind, turn_lease_token, created_at)
                VALUES
                ('mapping-a', 'service-a', 'conversation-a', NULL,
                 'legacy_transport', '', '2026-09-04 01:00:00'),
                ('mapping-b', 'service-a', 'conversation-a', NULL,
                 'legacy_transport', 'old-owner', '2026-09-04 02:00:00')"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO agent_mcp_invocations
                (id, mcp_conversation_id, status, error_code, error_message)
                VALUES ('invocation-b', 'mapping-b', 'running', '', '')"""
            )
        )
        connection.execute(
            migration._fail_interrupted_duplicate_invocations_statement()
        )
        connection.execute(migration._canonicalize_statement())

        mappings = connection.execute(
            text(
                """SELECT id, conversation_id, legacy_conversation_id,
                          binding_kind, turn_lease_token
                   FROM agent_mcp_conversations ORDER BY id"""
            )
        ).all()
        invocation = connection.execute(
            text(
                """SELECT status, error_code
                   FROM agent_mcp_invocations WHERE id = 'invocation-b'"""
            )
        ).one()
        assert tuple(mappings[0]) == (
            "mapping-a",
            "conversation-a",
            None,
            "legacy_transport",
            "",
        )
        assert tuple(mappings[1]) == (
            "mapping-b",
            None,
            "conversation-a",
            "legacy_duplicate",
            "",
        )
        assert tuple(invocation) == ("failed", "AgentMCPMigrationInterrupted")

        connection.execute(migration._restore_legacy_conversations_statement())
        restored = connection.execute(
            text(
                """SELECT conversation_id FROM agent_mcp_conversations
                   WHERE id = 'mapping-b'"""
            )
        ).scalar_one()
        assert restored == "conversation-a"
    engine.dispose()


def test_browser_mcp_clients_can_read_session_response_header() -> None:
    from app.main import app

    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls.__name__ == "CORSMiddleware"
    )
    exposed = {str(value).casefold() for value in cors.kwargs.get("expose_headers", [])}
    assert "mcp-session-id" in exposed


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
            self.assertEqual(created.headers["cache-control"], "no-store")
            self.assertEqual(created.headers["pragma"], "no-cache")
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
            self.assertEqual(rotated.headers["cache-control"], "no-store")
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
        received_calls: list[dict] = []
        received_protocol_versions: list[str] = []

        def invoke(_service_id: str, **kwargs):
            received_calls.append(kwargs)
            received_protocol_versions.append(
                agent_mcp_server._negotiated_protocol_version.get()
            )
            return {
                "answer": "审计完成",
                "conversation_id": "conversation-test",
                "trace_id": "trace-test",
                "assistant_message_id": "message-test",
                "citations": [
                    {
                        "citation_id": "C1",
                        "file_id": "file-test",
                        "filename": "依据.txt",
                        "text": "不应在公开 MCP 结果中重复的检索正文" * 1_000,
                    }
                ],
                "tool_calls": [{"id": "tool-1", "name": "execute_action"}],
                "tool_results": [
                    {
                        "id": "tool-1",
                        "name": "execute_action",
                        "result": json.dumps(
                            {
                                "ok": False,
                                "error": {
                                    "code": "TOOL_RESULT_TOO_LARGE",
                                    "message": "结果过大",
                                    "retryable": True,
                                },
                                "internal_rows": ["敏感明细" * 1_000],
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "runtime": {"environment": "prod"},
                "request_id": "request-test",
                "mcp_service_id": "service-agent-mcp",
                "mcp_service_name": "医保违规审计助手",
                "confirmation": {
                    "status": "confirmed",
                    "preview_log_id": "preview-test",
                    "message": "已生成审计文件。",
                    "response": {
                        "status": "success",
                        "log_id": "log-test",
                        "result": {
                            "row_count": 20,
                            "rows": ["不应重复传输的确认结果" * 1_000],
                            "artifact": {
                                "id": "artifact-test",
                                "filename": "审计结果.xlsx",
                                "format": "xlsx",
                                "download_url": "/api/files/artifact-test",
                            },
                        },
                    },
                },
            }

        original_message = (
            '审计 贵阳泰康乐综合医院  开展"电子结肠镜"检查，\n'
            "重复收取电子乙状结肠镜检查费用 的全部违规数据"
        )

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
                            self.assertEqual(
                                initialized.protocolVersion,
                                "2025-03-26",
                            )
                            self.assertEqual(initialized.serverInfo.name, "Ontology Platform Agent Gateway")
                            tools = await session.list_tools()
                            self.assertEqual([tool.name for tool in tools.tools], ["invoke_agent"])
                            invoke_tool = tools.tools[0]
                            self.assertIn("完整 Agent", invoke_tool.description)
                            self.assertIn(
                                "message",
                                invoke_tool.inputSchema["required"],
                            )
                            self.assertIn(
                                "不得只传关键词",
                                invoke_tool.inputSchema["properties"]["message"][
                                    "description"
                                ],
                            )
                            self.assertEqual(
                                set(invoke_tool.inputSchema["properties"]),
                                {"message", "conversation_id"},
                            )
                            self.assertIn(
                                "继续同一终端用户会话",
                                invoke_tool.inputSchema["properties"]["conversation_id"]["description"],
                            )
                            self.assertIsNone(invoke_tool.title)
                            self.assertIsNone(invoke_tool.outputSchema)
                            self.assertFalse(invoke_tool.meta)
                            legacy = await session.call_tool(
                                "invoke_agent",
                                {"original_user_message": "不再接受的私有契约参数"},
                            )
                            self.assertTrue(legacy.isError)
                            self.assertIn("message", legacy.content[0].text)
                            unknown = await session.call_tool(
                                "query_mapped_objects",
                                {"object_type": "Hospital"},
                            )
                            self.assertTrue(unknown.isError)
                            self.assertIn("Unknown tool", unknown.content[0].text)
                            result = await session.call_tool(
                                "invoke_agent",
                                {"message": original_message},
                            )
                            self.assertFalse(result.isError)
                            result_payload = result.model_dump(
                                mode="json",
                                by_alias=True,
                                exclude_none=True,
                            )
                            self.assertEqual(
                                set(result_payload),
                                {"content", "isError"},
                            )
                            self.assertIsNone(result.structuredContent)
                            self.assertEqual(result.content[0].text, "审计完成")
                            self.assertEqual(
                                set(result_payload["content"][0]),
                                {"type", "text"},
                            )
                            self.assertEqual(
                                set(result_payload["content"][1]),
                                {"type", "text"},
                            )
                            self.assertIsNone(result.content[0].annotations)
                            self.assertIsNone(result.content[1].annotations)
                            metadata_envelope = json.loads(result.content[1].text)
                            continuation = metadata_envelope["mcp_result"]
                            self.assertEqual(
                                continuation["continuation"]["conversation_id"],
                                "conversation-test",
                            )
                            self.assertEqual(
                                metadata_envelope["mcp_continuation"],
                                continuation["continuation"],
                            )
                            self.assertEqual(
                                metadata_envelope["input_receipt"],
                                continuation["input_receipt"],
                            )
                            self.assertNotIn("tool_calls", continuation)
                            self.assertNotIn("tool_results", continuation)
                            self.assertEqual(
                                continuation["continuation"]["conversation_id"],
                                "conversation-test",
                            )
                            self.assertEqual(
                                continuation["tool_execution"],
                                {
                                    "call_count": 1,
                                    "result_count": 1,
                                    "failed_count": 1,
                                },
                            )
                            public_json = json.dumps(
                                continuation,
                                ensure_ascii=False,
                            )
                            self.assertNotIn("execute_action", public_json)
                            self.assertNotIn("TOOL_RESULT_TOO_LARGE", public_json)
                            self.assertNotIn("query_mapped_objects", public_json)
                            self.assertNotIn(
                                "text", continuation["citations"][0]
                            )
                            confirmation_result = continuation["confirmation"][
                                "response"
                            ]["result"]
                            self.assertEqual(confirmation_result["row_count"], 20)
                            self.assertEqual(
                                confirmation_result["artifact"]["id"], "artifact-test"
                            )
                            self.assertNotIn("rows", confirmation_result)
                            self.assertLess(
                                len(
                                    json.dumps(
                                        result.model_dump(mode="json"),
                                        ensure_ascii=False,
                                    ).encode("utf-8")
                                ),
                                10_000,
                            )
                            repeated = await session.call_tool(
                                "invoke_agent",
                                {
                                    "message": "确认执行",
                                    "conversation_id": "conversation-test",
                                },
                            )
                            self.assertFalse(repeated.isError)
                            self.assertIsNone(repeated.structuredContent)
                            self.assertEqual(repeated.content[0].text, "审计完成")
                            repeated_metadata = json.loads(repeated.content[1].text)[
                                "mcp_result"
                            ]
                            self.assertEqual(
                                repeated_metadata["input_receipt"]["source"],
                                "tool_argument",
                            )
                            self.assertNotIn(
                                "conversation_binding_hash",
                                repeated_metadata["input_receipt"],
                            )

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(
                agent_mcp_service,
                "invoke_published_agent",
                side_effect=invoke,
            ),
            patch("mcp.types.LATEST_PROTOCOL_VERSION", "2025-03-26"),
        ):
            asyncio.run(exercise())
        self.assertEqual(len(received_calls), 2)
        self.assertEqual(received_protocol_versions, ["2025-03-26", "2025-03-26"])
        self.assertTrue(received_calls[0]["external_session_id"])
        self.assertEqual(
            received_calls[0]["external_session_id"],
            received_calls[1]["external_session_id"],
        )
        self.assertEqual(received_calls[0]["message"], original_message)
        self.assertEqual(received_calls[1]["message"], "确认执行")
        self.assertNotIn("external_conversation_id", received_calls[0])
        self.assertNotIn("external_turn_id", received_calls[1])

    def test_structured_tool_results_are_only_sent_to_supporting_protocols(self) -> None:
        self.assertFalse(agent_mcp_server._supports_structured_tool_results(""))
        self.assertFalse(
            agent_mcp_server._supports_structured_tool_results("2025-03-26")
        )
        self.assertTrue(
            agent_mcp_server._supports_structured_tool_results("2025-06-18")
        )
        self.assertTrue(
            agent_mcp_server._supports_structured_tool_results("2025-11-25")
        )
        self.assertFalse(
            agent_mcp_server._supports_structured_tool_results("latest")
        )

        async def invoke_with_current_protocol():
            service_token = agent_mcp_server._authenticated_service.set(
                "service-agent-mcp"
            )
            session_token = agent_mcp_server._authenticated_external_session.set(
                "mcp1.test.signature"
            )
            protocol_token = agent_mcp_server._negotiated_protocol_version.set(
                "2025-11-25"
            )
            try:
                return await agent_mcp_server.invoke_agent(
                    SimpleNamespace(request_id="rpc-test"),
                    "审计原始请求",
                )
            finally:
                agent_mcp_server._negotiated_protocol_version.reset(protocol_token)
                agent_mcp_server._authenticated_external_session.reset(session_token)
                agent_mcp_server._authenticated_service.reset(service_token)

        with patch.object(
            agent_mcp_service,
            "invoke_published_agent",
            return_value={
                "answer": "审计完成",
                "conversation_id": "conversation-test",
                "request_id": "request-test",
            },
        ):
            result = asyncio.run(invoke_with_current_protocol())
        self.assertEqual(result.structuredContent["answer"], "审计完成")
        self.assertEqual(
            result.structuredContent["continuation"]["conversation_id"],
            "conversation-test",
        )
        metadata = json.loads(result.content[1].text)
        self.assertEqual(
            metadata["mcp_continuation"],
            metadata["mcp_result"]["continuation"],
        )

    def test_delete_revokes_publication_without_erasing_invocation_audit(self) -> None:
        raw_token = self._add_published_service(service_id="service-soft-delete")
        db = self.Session()
        try:
            conversation = Conversation(
                id="conversation-soft-delete",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                title="保留审计",
            )
            db.add(conversation)
            db.flush()
            mapping = AgentMCPConversation(
                id="mapping-soft-delete",
                service_id="service-soft-delete",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                external_session_hash="a" * 64,
                binding_kind="isolated_turn",
                conversation_id=conversation.id,
            )
            db.add(mapping)
            db.flush()
            db.add(AgentMCPInvocation(
                id="invocation-soft-delete",
                service_id="service-soft-delete",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                request_id="request-soft-delete",
                mcp_conversation_id=mapping.id,
                conversation_id=conversation.id,
                input_hash="b" * 64,
                status="succeeded",
            ))
            db.commit()
        finally:
            db.close()

        deleted = self.client.delete(
            "/api/agent-mcp-services/service-soft-delete"
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get("/api/agent-mcp-services").json(), [])

        db = self.Session()
        try:
            service = db.get(AgentMCPService, "service-soft-delete")
            self.assertIsNotNone(service)
            self.assertFalse(service.enabled)
            self.assertIsNotNone(service.deleted_at)
            self.assertIsNotNone(
                db.get(AgentMCPInvocation, "invocation-soft-delete")
            )
            self.assertIsNotNone(
                db.get(AgentMCPConversation, "mapping-soft-delete")
            )
        finally:
            db.close()
        with patch.object(agent_mcp_service, "SessionLocal", self.Session):
            self.assertIsNone(agent_mcp_service.authenticate_token(raw_token))

    def test_transport_session_does_not_define_end_user_conversation(self) -> None:
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
                external_request_id="rpc-1",
            )
            second = agent_mcp_service.invoke_published_agent(
                "service-session-a",
                message="第二轮",
                conversation_id=None,
                external_session_id="third-party-session-1",
                # JSON-RPC ids may be reused after a completed response. The
                # changed payload must still start an isolated transcript.
                external_request_id="rpc-1",
            )
            resumed_after_reconnect = agent_mcp_service.invoke_published_agent(
                "service-session-a",
                message="重连后的确认回复",
                conversation_id=first["conversation_id"],
                external_session_id="third-party-session-2",
                external_request_id="rpc-1",
            )
            other_service = agent_mcp_service.invoke_published_agent(
                "service-session-b",
                message="隔离会话",
                conversation_id=None,
                external_session_id="third-party-session-1",
                external_request_id="rpc-1",
            )

        self.assertNotEqual(first["conversation_id"], second["conversation_id"])
        self.assertEqual(
            first["conversation_id"], resumed_after_reconnect["conversation_id"]
        )
        self.assertNotEqual(first["conversation_id"], other_service["conversation_id"])
        self.assertEqual(
            observed_conversations[:3],
            [
                first["conversation_id"],
                second["conversation_id"],
                first["conversation_id"],
            ],
        )
        db = self.Session()
        try:
            mappings = db.execute(select(AgentMCPConversation)).scalars().all()
            self.assertEqual(len(mappings), 3)
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
                4,
            )
            self.assertEqual(
                len(db.execute(select(Conversation)).scalars().all()),
                3,
            )
        finally:
            db.close()

    def test_external_conversation_id_reuses_across_transport_sessions(self) -> None:
        self._add_published_service(service_id="service-external-conversation")
        observed: list[tuple[str, str, str]] = []

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
            assert conversation_id is not None
            observed.append(
                (
                    message,
                    conversation_id,
                    str(db.info.get("agent_mcp_turn_request_hash") or ""),
                )
            )
            return {
                "answer": message,
                "conversation_id": conversation_id,
                "trace_id": f"trace-{len(observed)}",
                "tool_calls": [],
                "tool_results": [],
            }

        original = '审计 贵阳泰康乐综合医院  刮痧"治疗"收费大于两次\n的全部违规数据'
        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(agent_mcp_service, "service_runtime_status", side_effect=runtime_status),
            patch("app.routers.agents.invoke_agent_once", side_effect=invoke),
        ):
            first = agent_mcp_service.invoke_published_agent(
                "service-external-conversation",
                message=original,
                conversation_id=None,
                external_session_id="transport-1",
                external_request_id="rpc-1",
                external_conversation_id="ui-chat-a",
                external_turn_id="ui-message-a1",
            )
            continued = agent_mcp_service.invoke_published_agent(
                "service-external-conversation",
                message="确认执行",
                conversation_id=None,
                external_session_id="transport-2",
                external_request_id="rpc-1",
                external_conversation_id="ui-chat-a",
                external_turn_id="ui-message-a2",
            )
            separate = agent_mcp_service.invoke_published_agent(
                "service-external-conversation",
                message="另一会话",
                conversation_id=None,
                external_session_id="transport-2",
                external_request_id="rpc-2",
                external_conversation_id="ui-chat-b",
                external_turn_id="ui-message-a1",
            )
            with self.assertRaisesRegex(
                agent_mcp_service.AgentMCPError,
                "CONVERSATION_BINDING_CONFLICT",
            ):
                agent_mcp_service.invoke_published_agent(
                    "service-external-conversation",
                    message="不得串接",
                    conversation_id=first["conversation_id"],
                    external_session_id="transport-2",
                    external_request_id="rpc-3",
                    external_conversation_id="ui-chat-c",
                    external_turn_id="ui-message-c1",
                )

        self.assertEqual(observed[0][0], original)
        self.assertEqual(first["conversation_id"], continued["conversation_id"])
        self.assertNotEqual(first["conversation_id"], separate["conversation_id"])
        self.assertNotEqual(observed[0][2], observed[2][2])
        self.assertEqual(first["mcp_conversation_mode"], "external_conversation_id")
        self.assertEqual(
            first["mcp_input_receipt"]["conversation_binding_hash"],
            continued["mcp_input_receipt"]["conversation_binding_hash"],
        )
        self.assertNotEqual(
            first["mcp_input_receipt"]["conversation_binding_hash"],
            separate["mcp_input_receipt"]["conversation_binding_hash"],
        )
        db = self.Session()
        try:
            self.assertEqual(
                len(db.execute(select(AgentMCPConversation)).scalars().all()),
                2,
            )
            self.assertEqual(
                len(db.execute(select(Conversation)).scalars().all()),
                2,
            )
            invocations = db.execute(select(AgentMCPInvocation)).scalars().all()
            self.assertEqual(len(invocations), 3)
            self.assertTrue(
                all(item.result["mcp_input_receipt"]["message_sha256"] for item in invocations)
            )
            self.assertEqual(
                {mapping.binding_kind for mapping in db.execute(
                    select(AgentMCPConversation)
                ).scalars().all()},
                {"external_conversation_id"},
            )
        finally:
            db.close()

    def test_host_conversation_refuses_ambiguous_legacy_mapping_adoption(self) -> None:
        self._add_published_service(service_id="service-adopt-legacy-mapping")
        observed: list[tuple[str, str]] = []

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
            assert conversation_id is not None
            observed.append((message, conversation_id))
            return {
                "answer": message,
                "conversation_id": conversation_id,
                "trace_id": f"trace-{len(observed)}",
                "tool_calls": [],
                "tool_results": [],
            }

        with (
            patch.object(agent_mcp_service, "SessionLocal", self.Session),
            patch.object(agent_mcp_service, "service_runtime_status", side_effect=runtime_status),
            patch("app.routers.agents.invoke_agent_once", side_effect=invoke),
        ):
            legacy = agent_mcp_service.invoke_published_agent(
                "service-adopt-legacy-mapping",
                message="需要确认的历史请求",
                conversation_id=None,
                external_session_id="legacy-transport",
                external_request_id="rpc-legacy",
            )
            db = self.Session()
            try:
                before = db.execute(select(AgentMCPConversation)).scalar_one()
                before_mapping_id = before.id
                self.assertEqual(before.binding_kind, "isolated_turn")
            finally:
                db.close()

            with self.assertRaisesRegex(
                agent_mcp_service.AgentMCPError,
                "CONVERSATION_BINDING_CONFLICT",
            ):
                agent_mcp_service.invoke_published_agent(
                    "service-adopt-legacy-mapping",
                    message="确认执行",
                    conversation_id=legacy["conversation_id"],
                    external_session_id="new-transport",
                    external_request_id="rpc-confirm",
                    external_conversation_id="host-ui-chat-42",
                    external_turn_id="host-ui-turn-2",
                    input_source="host_context_v1",
                )

        self.assertEqual([item[0] for item in observed], ["需要确认的历史请求"])
        db = self.Session()
        try:
            mappings = db.execute(select(AgentMCPConversation)).scalars().all()
            self.assertEqual(len(mappings), 1)
            self.assertEqual(mappings[0].id, before_mapping_id)
            self.assertEqual(mappings[0].binding_kind, "isolated_turn")
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
            binding = agent_mcp_service._conversation_for_external_binding(
                db,
                service,
                message="外部 MCP 会话初始化",
                conversation_id=None,
                binding_key="isolated-turn\0mcp-session-fk-order\0rpc-1",
                binding_mode="isolated_turn",
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

    def test_same_business_conversation_serializes_turns_and_replays_one_request(self) -> None:
        self._add_published_service(service_id="service-serialized-session")
        external_session_id = "signed-session-test"
        external_conversation_id = "third-party-chat-serialized"
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
                external_session_hash=agent_mcp_service.conversation_binding_hash(
                    f"external-conversation\0{external_conversation_id}"
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
                    external_conversation_id=external_conversation_id,
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
                external_conversation_id=external_conversation_id,
                input_source="host_context_v1",
            )

        self.assertFalse(errors, errors)
        self.assertEqual([item[0] for item in observed], ["first", "second"])
        self.assertEqual(results["first"]["answer"], "first")
        self.assertEqual(results["retry"]["answer"], results["first"]["answer"])
        self.assertEqual(
            results["retry"]["conversation_id"],
            results["first"]["conversation_id"],
        )
        self.assertFalse(results["first"]["mcp_replayed"])
        self.assertTrue(results["retry"]["mcp_replayed"])
        self.assertEqual(results["second"]["answer"], "second")
        self.assertEqual(replay["answer"], results["first"]["answer"])
        self.assertTrue(replay["mcp_replayed"])
        self.assertEqual(
            replay["mcp_input_receipt"]["source"],
            "tool_argument",
        )
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
                == agent_mcp_service.external_request_hash(
                    "transport-request\0"
                    f"{external_session_id}\0rpc-1\0"
                    + hashlib.sha256(b"first").hexdigest()
                )
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
                external_session_hash=agent_mcp_service.conversation_binding_hash(
                    "external-conversation\0expired-session"
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
                binding_key_hash=mapping.external_session_hash,
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

    def test_claim_refreshes_cached_mapping_before_deciding_lease_owner(self) -> None:
        self._add_published_service(service_id="service-refresh-claim-lease")
        db = self.Session()
        other = self.Session()
        try:
            conversation = Conversation(
                id="conversation-refresh-claim-lease",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                title="缓存租约测试",
            )
            mapping = AgentMCPConversation(
                id="mapping-refresh-claim-lease",
                service_id="service-refresh-claim-lease",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                external_session_hash=agent_mcp_service.conversation_binding_hash(
                    "external-conversation\0refresh-claim"
                ),
                binding_kind="external_conversation_id",
                conversation_id=conversation.id,
            )
            db.add_all([conversation, mapping])
            db.commit()
            service = db.get(AgentMCPService, "service-refresh-claim-lease")
            cached_mapping = db.get(AgentMCPConversation, mapping.id)
            assert service is not None
            assert cached_mapping is not None
            db.commit()

            current = other.get(AgentMCPConversation, mapping.id)
            assert current is not None
            current.turn_lease_token = "current-owner"
            current.turn_lease_generation = 7
            current.turn_lease_expires_at = (
                agent_mcp_service.utc_now() + timedelta(seconds=30)
            )
            current.turn_lease_deadline_at = (
                agent_mcp_service.utc_now() + timedelta(seconds=60)
            )
            other.commit()

            claim = agent_mcp_service._claim_mcp_turn_once(
                db,
                service=service,
                binding=agent_mcp_service.MCPConversationBinding(
                    mapping_id=mapping.id,
                    conversation_id=conversation.id,
                    binding_key_hash=mapping.external_session_hash,
                ),
                request_hash=agent_mcp_service.external_request_hash("waiting-request"),
                input_hash="c" * 64,
                request_id="waiting-internal-request",
            )
            self.assertIsNone(claim)
            self.assertEqual(cached_mapping.turn_lease_token, "current-owner")
            self.assertEqual(cached_mapping.turn_lease_generation, 7)
        finally:
            other.close()
            db.close()

    def test_late_failure_refreshes_rows_and_preserves_newer_lease(self) -> None:
        self._add_published_service(service_id="service-refresh-fail-lease")
        db = self.Session()
        other = self.Session()
        try:
            conversation = Conversation(
                id="conversation-refresh-fail-lease",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                title="失败围栏测试",
            )
            mapping = AgentMCPConversation(
                id="mapping-refresh-fail-lease",
                service_id="service-refresh-fail-lease",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                external_session_hash=agent_mcp_service.conversation_binding_hash(
                    "external-conversation\0refresh-fail"
                ),
                binding_kind="external_conversation_id",
                conversation_id=conversation.id,
                turn_lease_token="old-owner",
                turn_lease_generation=1,
                turn_lease_expires_at=agent_mcp_service.utc_now() + timedelta(seconds=30),
                turn_lease_deadline_at=agent_mcp_service.utc_now() + timedelta(seconds=60),
            )
            invocation = AgentMCPInvocation(
                id="invocation-refresh-fail-lease",
                service_id="service-refresh-fail-lease",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                request_id="request-refresh-fail",
                mcp_conversation_id=mapping.id,
                external_request_hash=agent_mcp_service.external_request_hash(
                    "request-refresh-fail"
                ),
                turn_lease_token="old-owner",
                turn_lease_generation=1,
                conversation_id=conversation.id,
                input_hash="d" * 64,
                status="running",
            )
            db.add_all([conversation, mapping, invocation])
            db.commit()
            cached_mapping = db.get(AgentMCPConversation, mapping.id)
            cached_invocation = db.get(AgentMCPInvocation, invocation.id)
            assert cached_mapping is not None
            assert cached_invocation is not None
            db.commit()

            current_mapping = other.get(AgentMCPConversation, mapping.id)
            current_invocation = other.get(AgentMCPInvocation, invocation.id)
            assert current_mapping is not None
            assert current_invocation is not None
            current_mapping.turn_lease_token = "new-owner"
            current_mapping.turn_lease_generation = 2
            current_invocation.turn_lease_token = "new-owner"
            current_invocation.turn_lease_generation = 2
            other.commit()

            agent_mcp_service._fail_mcp_turn(
                db,
                lease=agent_mcp_service.AgentMCPTurnLease(
                    mapping_id=mapping.id,
                    invocation_id=invocation.id,
                    token="old-owner",
                    generation=1,
                    deadline_at=agent_mcp_service.utc_now() + timedelta(seconds=60),
                    session_factory=self.Session,
                ),
                error=RuntimeError("late failure"),
                latency_ms=1,
            )
        finally:
            other.close()
            db.close()

        verify = self.Session()
        try:
            final_mapping = verify.get(AgentMCPConversation, "mapping-refresh-fail-lease")
            final_invocation = verify.get(
                AgentMCPInvocation, "invocation-refresh-fail-lease"
            )
            assert final_mapping is not None
            assert final_invocation is not None
            self.assertEqual(final_mapping.turn_lease_token, "new-owner")
            self.assertEqual(final_mapping.turn_lease_generation, 2)
            self.assertEqual(final_invocation.turn_lease_token, "new-owner")
            self.assertEqual(final_invocation.status, "running")
        finally:
            verify.close()

    def test_completion_refreshes_expiry_extended_by_lease_heartbeat(self) -> None:
        self._add_published_service(service_id="service-refresh-complete-lease")
        db = self.Session()
        heartbeat = self.Session()
        try:
            now = agent_mcp_service.utc_now()
            conversation = Conversation(
                id="conversation-refresh-complete-lease",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
                title="续租完成测试",
            )
            mapping = AgentMCPConversation(
                id="mapping-refresh-complete-lease",
                service_id="service-refresh-complete-lease",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                external_session_hash=agent_mcp_service.conversation_binding_hash(
                    "external-conversation\0refresh-complete"
                ),
                binding_kind="external_conversation_id",
                conversation_id=conversation.id,
                turn_lease_token="heartbeat-owner",
                turn_lease_generation=4,
                # Keep an expired value in the main Session's identity map.
                turn_lease_expires_at=now - timedelta(seconds=1),
                turn_lease_deadline_at=now + timedelta(seconds=120),
            )
            invocation = AgentMCPInvocation(
                id="invocation-refresh-complete-lease",
                service_id="service-refresh-complete-lease",
                tenant_id=self.tenant.id,
                agent_id=self.agent.id,
                execution_user_id=self.owner.id,
                request_id="request-refresh-complete",
                mcp_conversation_id=mapping.id,
                external_request_hash=agent_mcp_service.external_request_hash(
                    "request-refresh-complete"
                ),
                turn_lease_token="heartbeat-owner",
                turn_lease_generation=4,
                conversation_id=conversation.id,
                input_hash="e" * 64,
                status="running",
            )
            db.add_all([conversation, mapping, invocation])
            db.commit()
            cached_mapping = db.get(AgentMCPConversation, mapping.id)
            assert cached_mapping is not None
            db.commit()

            renewed = heartbeat.get(AgentMCPConversation, mapping.id)
            assert renewed is not None
            renewed.turn_lease_expires_at = now + timedelta(seconds=60)
            heartbeat.commit()

            agent_mcp_service._complete_mcp_turn(
                db,
                lease=agent_mcp_service.AgentMCPTurnLease(
                    mapping_id=mapping.id,
                    invocation_id=invocation.id,
                    token="heartbeat-owner",
                    generation=4,
                    deadline_at=now + timedelta(seconds=120),
                    session_factory=self.Session,
                ),
                result={"answer": "completed", "conversation_id": conversation.id},
                latency_ms=1,
            )
            self.assertGreater(cached_mapping.turn_lease_generation, 0)
        finally:
            heartbeat.close()
            db.close()

        verify = self.Session()
        try:
            final_mapping = verify.get(
                AgentMCPConversation, "mapping-refresh-complete-lease"
            )
            final_invocation = verify.get(
                AgentMCPInvocation, "invocation-refresh-complete-lease"
            )
            assert final_mapping is not None
            assert final_invocation is not None
            self.assertEqual(final_mapping.turn_lease_token, "")
            self.assertEqual(final_invocation.status, "succeeded")
            self.assertEqual(final_invocation.result["answer"], "completed")
        finally:
            verify.close()


if __name__ == "__main__":
    unittest.main()
