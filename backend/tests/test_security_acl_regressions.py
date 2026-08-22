from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.config import get_settings
from app import database
from app.database import Base, get_db
from app.models import (
    Agent,
    AssistantAttachment,
    AssistantThread,
    AuthSession,
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    Conversation,
    DataSource,
    DocumentChunk,
    LLMConfig,
    MCPConfig,
    Message,
    OrganizationMember,
    Skill,
    Tenant,
    User,
)
from app.routers import agents, assistant, data_sources, llm_configs, mcp, permissions, scenarios, skills
from app.services import datasource_service, permission_service
from app.services import auth_service
from app.services.auth_service import get_current_user, get_tenant_db


class SecurityAclRegressionTests(unittest.TestCase):
    """Regression coverage for scenario ACL alternate-route bypasses."""

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
            self.tenant = Tenant(id="tenant-security-a", name="甲方")
            self.foreign_tenant = Tenant(id="tenant-security-b", name="乙方")
            self.owner = User(
                id="owner-security-a",
                tenant_id=self.tenant.id,
                email="owner-security@example.test",
                password_hash="test-only",
                status="active",
            )
            self.viewer = User(
                id="viewer-security-a",
                tenant_id=self.tenant.id,
                email="viewer-security@example.test",
                password_hash="test-only",
                status="active",
            )
            self.foreign_owner = User(
                id="owner-security-b",
                tenant_id=self.foreign_tenant.id,
                email="owner-security-b@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-security-private",
                tenant_id=self.tenant.id,
                name="受限审计场景",
            )
            self.scoped_source = DataSource(
                id="source-security-scoped",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="受限资料库",
                type="file_bucket",
            )
            self.sql_source = DataSource(
                id="source-security-connection-test",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="连接测试数据库",
                type="postgres",
                config={
                    "user": "db-user",
                    "password": "stored-db-password",
                    "host": "db.example.test",
                    "database": "orders",
                },
            )
            self.scoped_file = BucketFile(
                id="file-security-scoped",
                data_source_id=self.scoped_source.id,
                filename="受限资料.md",
                stored_path="/tmp/secure.md",
                status="parsed",
                parsed_text="场景机密资料。",
            )
            # A legacy Agent may have been bound while a foreign source was
            # public.  Once revoked, its historical cited answer must redact.
            self.foreign_public_source = DataSource(
                id="source-security-public",
                tenant_id=self.foreign_tenant.id,
                is_public=True,
                name="临时公开资料库",
                type="file_bucket",
            )
            self.foreign_file = BucketFile(
                id="file-security-public",
                data_source_id=self.foreign_public_source.id,
                filename="公开后撤销.md",
                stored_path="/tmp/public.md",
                status="parsed",
                parsed_text="REVOKED_SECRET_EXCERPT",
            )
            self.foreign_chunk = DocumentChunk(
                id="chunk-security-public",
                bucket_file_id=self.foreign_file.id,
                data_source_id=self.foreign_public_source.id,
                ordinal=0,
                char_start=0,
                char_end=22,
                text="REVOKED_SECRET_EXCERPT",
                content_hash="citation-hash",
                embedding=[],
            )
            self.agent = Agent(
                id="agent-security",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="受限场景助手",
                data_source_ids=[self.foreign_public_source.id],
            )
            self.embedding_only_llm = LLMConfig(
                id="llm-security-embedding-only",
                tenant_id=self.tenant.id,
                name="仅嵌入模型",
                capabilities=["embedding"],
                enabled=True,
            )
            self.disabled_chat_llm = LLMConfig(
                id="llm-security-disabled",
                tenant_id=self.tenant.id,
                name="已停用聊天模型",
                capabilities=["chat", "tool"],
                enabled=False,
            )
            self.chat_only_llm = LLMConfig(
                id="llm-security-chat-only",
                tenant_id=self.tenant.id,
                name="无工具聊天模型",
                capabilities=["chat"],
                enabled=True,
            )
            self.skill = Skill(
                id="skill-security",
                tenant_id=self.tenant.id,
                name="security-test-skill",
                path="/not/executed/by-viewer",
                source="builtin",
                enabled=True,
            )
            self.public_skill = Skill(
                id="skill-security-public",
                is_public=True,
                name="security-test-public-skill",
                path="/not/executed-by-owner",
                source="builtin",
                enabled=False,
            )
            self.mcp = MCPConfig(
                id="mcp-security",
                tenant_id=self.tenant.id,
                name="security-test-mcp",
                transport="stdio",
                command="not-executed-by-viewer",
                enabled=True,
            )
            self.conversation = Conversation(
                id="conversation-security",
                agent_id=self.agent.id,
                created_by_user_id=self.owner.id,
            )
            self.legacy_conversation = Conversation(
                id="conversation-security-legacy",
                agent_id=self.agent.id,
            )
            self.viewer_conversation = Conversation(
                id="conversation-security-viewer",
                agent_id=self.agent.id,
                created_by_user_id=self.viewer.id,
            )
            self.cited_message = Message(
                id="message-security-cited",
                conversation_id=self.conversation.id,
                role="assistant",
                content="历史回答包含 REVOKED_SECRET_EXCERPT。",
                tool_results=[{"result": "REVOKED_SECRET_EXCERPT"}],
                citations=[
                    {
                        "citation_id": "C1",
                        "data_source_id": self.foreign_public_source.id,
                        "file_id": self.foreign_file.id,
                        "chunk_id": self.foreign_chunk.id,
                        "content_hash": "citation-hash",
                        "text": "REVOKED_SECRET_EXCERPT",
                    }
                ],
            )
            self.legacy_read_message = Message(
                id="message-security-legacy-read",
                conversation_id=self.conversation.id,
                role="assistant",
                content="旧工具输出包含 REVOKED_LEGACY_SECRET。",
                tool_calls=[{"id": "legacy-read", "name": "read_document"}],
                tool_results=[{"id": "legacy-read", "result": "REVOKED_LEGACY_SECRET"}],
                citations=[],
            )
            self.owner_thread = AssistantThread(
                id="assistant-thread-owner",
                tenant_id=self.tenant.id,
                created_by_user_id=self.owner.id,
                scope_key="global|path:/dashboard",
                title="所有者私有会话",
            )
            self.owner_attachment = AssistantAttachment(
                id="assistant-attachment-owner",
                tenant_id=self.tenant.id,
                created_by_user_id=self.owner.id,
                filename="所有者附件.md",
                status="parsed",
                parsed_text="OWNER_ATTACHMENT_SECRET",
            )
            db.add_all(
                [
                    self.tenant,
                    self.foreign_tenant,
                    self.owner,
                    self.viewer,
                    self.foreign_owner,
                    self.scenario,
                    self.scoped_source,
                    self.sql_source,
                    self.scoped_file,
                    self.foreign_public_source,
                    self.foreign_file,
                    self.foreign_chunk,
                    self.agent,
                    self.embedding_only_llm,
                    self.disabled_chat_llm,
                    self.chat_only_llm,
                    self.skill,
                    self.public_skill,
                    self.mcp,
                    self.conversation,
                    self.legacy_conversation,
                    self.viewer_conversation,
                    self.cited_message,
                    self.legacy_read_message,
                    self.owner_thread,
                    self.owner_attachment,
                ]
            )
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.ensure_organization(
                db, self.foreign_tenant.id, owner_user_id=self.foreign_owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.viewer.id, role_key="viewer"
            )
            db.commit()
            self.organization_id = organization.id
        finally:
            db.close()

        self.current_user_id = self.owner.id
        self.current_tenant_id = self.tenant.id
        self.app = FastAPI()
        self.app.include_router(scenarios.router, prefix="/api")
        self.app.include_router(data_sources.router, prefix="/api")
        self.app.include_router(agents.router, prefix="/api")
        self.app.include_router(assistant.router, prefix="/api")
        self.app.include_router(permissions.router, prefix="/api")
        self.app.include_router(skills.router, prefix="/api")
        self.app.include_router(mcp.router, prefix="/api")
        self.app.include_router(llm_configs.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.current_user_id, tenant_id=self.current_tenant_id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.current_user_id
            request_db.info["tenant_id"] = self.current_tenant_id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_current_user] = override_current_user
        self.app.dependency_overrides[get_db] = override_db
        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _as_viewer(self) -> None:
        self.current_user_id = self.viewer.id
        self.current_tenant_id = self.tenant.id

    def _deny_viewer_scenario_read(self) -> None:
        db = self.Session()
        try:
            db.add(
                AuthorizationGrant(
                    organization_id=self.organization_id,
                    user_id=self.viewer.id,
                    resource_type="scenario",
                    resource_id=self.scenario.id,
                    verb="read",
                    effect="deny",
                    created_by_user_id=self.owner.id,
                )
            )
            db.commit()
        finally:
            db.close()

    def test_explicit_scenario_deny_closes_list_data_source_rag_agent_and_assistant_paths(self) -> None:
        self._deny_viewer_scenario_read()
        self._as_viewer()

        self.assertEqual(self.client.get("/api/scenarios").json(), [])
        source_ids = {item["id"] for item in self.client.get("/api/data-sources").json()}
        self.assertNotIn(self.scoped_source.id, source_ids)
        self.assertEqual(self.client.get("/api/agents").json(), [])

        for url in (
            f"/api/data-sources?scenario_id={self.scenario.id}",
            f"/api/data-sources/{self.scoped_source.id}/files",
            f"/api/data-sources/{self.scoped_source.id}/tables",
            f"/api/data-sources/files/{self.scoped_file.id}/text",
            f"/api/agents/{self.agent.id}",
            f"/api/agents/conversations/{self.viewer_conversation.id}/messages",
            f"/api/assistant/threads?scenario_id={self.scenario.id}&path=/scenarios/{self.scenario.id}",
        ):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403, response.text)

        search = self.client.post(
            "/api/data-sources/search",
            json={"query": "机密", "scenario_id": self.scenario.id},
        )
        self.assertEqual(search.status_code, 403, search.text)

        for url, payload in (
            (f"/api/data-sources/{self.scoped_source.id}/query", {"sql": "SELECT 1"}),
            (f"/api/data-sources/{self.scoped_source.id}/test", None),
            (f"/api/data-sources/{self.scoped_source.id}/reindex", None),
            (f"/api/agents/{self.agent.id}/chat", {"message": "无权不能对话"}),
        ):
            response = self.client.post(url, json=payload)
            self.assertEqual(response.status_code, 403, response.text)

    def test_data_source_connection_test_never_returns_or_persists_driver_secrets(self) -> None:
        leaked_driver_error = (
            "OperationalError: postgresql+psycopg2://db-user:db-password@db.example.test:5432/"
            "orders?token=api-token-value; Bearer bearer-token-value; password=another-password"
        )

        # The service itself must not return raw driver text, which frequently
        # contains a DSN and credentials.
        with patch(
            "app.services.datasource_service.get_engine",
            side_effect=RuntimeError(leaked_driver_error),
        ):
            ok, service_message = datasource_service.test_connection(self.sql_source)
        self.assertFalse(ok)
        self.assertEqual(
            service_message,
            datasource_service.CONNECTION_TEST_FAILURE_MESSAGE,
        )

        # The route also protects persisted state and API output if a future
        # adapter accidentally returns an unsafe diagnostic.
        with patch(
            "app.routers.data_sources.datasource_service.test_connection",
            return_value=(False, leaked_driver_error),
        ):
            response = self.client.post(f"/api/data-sources/{self.sql_source.id}/test")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ok"])
        self.assertEqual(
            response.json()["message"],
            datasource_service.CONNECTION_TEST_FAILURE_MESSAGE,
        )
        db = self.Session()
        try:
            stored = db.get(DataSource, self.sql_source.id)
            assert stored is not None
            self.assertEqual(
                stored.last_error,
                datasource_service.CONNECTION_TEST_FAILURE_MESSAGE,
            )
            public_text = f"{response.text} {stored.last_error}"
            for unsafe_fragment in (
                "postgresql+psycopg2://",
                "db-password",
                "api-token-value",
                "bearer-token-value",
                "another-password",
            ):
                self.assertNotIn(unsafe_fragment, public_text)
        finally:
            db.close()

    def test_historic_citation_is_redacted_after_source_revocation(self) -> None:
        before = self.client.get(f"/api/agents/conversations/{self.conversation.id}/messages")
        self.assertEqual(before.status_code, 200, before.text)
        self.assertIn("REVOKED_SECRET_EXCERPT", before.json()[0]["content"])

        db = self.Session()
        try:
            source = db.get(DataSource, self.foreign_public_source.id)
            source.is_public = False
            db.commit()
        finally:
            db.close()

        after = self.client.get(f"/api/agents/conversations/{self.conversation.id}/messages")
        self.assertEqual(after.status_code, 200, after.text)
        message = after.json()[0]
        self.assertNotIn("REVOKED_SECRET_EXCERPT", after.text)
        self.assertNotIn("REVOKED_SECRET_EXCERPT", message["content"])
        self.assertEqual(message["citations"], [])
        self.assertEqual(message["tool_results"], [])

    def test_legacy_uncited_document_tool_result_is_fail_closed(self) -> None:
        response = self.client.get(f"/api/agents/conversations/{self.conversation.id}/messages")
        self.assertEqual(response.status_code, 200, response.text)
        message = next(item for item in response.json() if item["id"] == self.legacy_read_message.id)
        self.assertNotIn("REVOKED_LEGACY_SECRET", response.text)
        self.assertEqual(message["citations"], [])
        self.assertEqual(message["tool_results"], [])

    def test_agent_rejects_incompatible_model_bindings_and_disabled_bound_chat_model(self) -> None:
        create = self.client.post(
            "/api/agents",
            json={
                "name": "不能绑定嵌入模型的助手",
                "scenario_id": self.scenario.id,
                "llm_config_id": self.embedding_only_llm.id,
            },
        )
        self.assertEqual(create.status_code, 400, create.text)

        no_tool = self.client.post(
            "/api/agents",
            json={
                "name": "不能用无工具模型读取资料的助手",
                "scenario_id": self.scenario.id,
                "llm_config_id": self.chat_only_llm.id,
                "data_source_ids": [self.foreign_public_source.id],
            },
        )
        self.assertEqual(no_tool.status_code, 400, no_tool.text)

        db = self.Session()
        try:
            agent = db.get(Agent, self.agent.id)
            agent.llm_config_id = self.disabled_chat_llm.id
            db.commit()
        finally:
            db.close()
        chat = self.client.post(
            f"/api/agents/{self.agent.id}/chat",
            json={"message": "不应静默回退到默认模型"},
        )
        self.assertEqual(chat.status_code, 409, chat.text)

    def test_viewer_cannot_mutate_or_execute_tenant_technical_configuration(self) -> None:
        self._as_viewer()
        # Read-only catalog/config discovery remains available to ordinary
        # members, but it must not trigger a filesystem rescan.
        for url in ("/api/skills", "/api/mcp", "/api/llm-configs"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, response.text)

        protected = [
            ("post", "/api/skills/rescan", None),
            ("put", f"/api/skills/{self.skill.id}", {"enabled": False}),
            ("post", f"/api/skills/{self.skill.id}/execute", {"args": []}),
            ("post", f"/api/skills/{self.public_skill.id}/execute", {"args": []}),
            ("post", "/api/mcp", {"name": "viewer-mcp", "command": "never"}),
            ("put", f"/api/mcp/{self.mcp.id}", {"name": "viewer-mcp", "command": "never"}),
            ("delete", f"/api/mcp/{self.mcp.id}", None),
            ("post", f"/api/mcp/{self.mcp.id}/test", None),
            ("get", f"/api/mcp/{self.mcp.id}/tools", None),
            ("post", "/api/llm-configs", {"name": "viewer-llm"}),
            ("put", f"/api/llm-configs/{self.embedding_only_llm.id}", {"name": "viewer-llm"}),
            ("delete", f"/api/llm-configs/{self.embedding_only_llm.id}", None),
            ("post", f"/api/llm-configs/{self.embedding_only_llm.id}/test", None),
            (
                "post",
                f"/api/llm-configs/{self.embedding_only_llm.id}/evaluations",
                {"name": "viewer-write", "capability": "embedding", "passed": True},
            ),
        ]
        for method, url, payload in protected:
            kwargs = {"json": payload} if method in {"post", "put"} else {}
            response = getattr(self.client, method)(url, **kwargs)
            self.assertEqual(response.status_code, 403, f"{method} {url}: {response.text}")

    def test_direct_technical_execution_requires_enabled_resource(self) -> None:
        db = self.Session()
        try:
            db.get(Skill, self.skill.id).enabled = False
            db.get(MCPConfig, self.mcp.id).enabled = False
            db.commit()
        finally:
            db.close()

        skill = self.client.post(f"/api/skills/{self.skill.id}/execute", json={"args": []})
        self.assertEqual(skill.status_code, 409, skill.text)
        public_skill = self.client.post(
            f"/api/skills/{self.public_skill.id}/execute", json={"args": []}
        )
        self.assertEqual(public_skill.status_code, 409, public_skill.text)
        mcp_tools = self.client.get(f"/api/mcp/{self.mcp.id}/tools")
        self.assertEqual(mcp_tools.status_code, 409, mcp_tools.text)
        llm = self.client.post(f"/api/llm-configs/{self.disabled_chat_llm.id}/test")
        self.assertEqual(llm.status_code, 409, llm.text)

    def test_agent_conversations_are_creator_scoped_and_legacy_rows_fail_closed(self) -> None:
        owner_conversations = self.client.get(f"/api/agents/{self.agent.id}/conversations")
        self.assertEqual(owner_conversations.status_code, 200, owner_conversations.text)
        self.assertEqual([item["id"] for item in owner_conversations.json()], [self.conversation.id])

        legacy = self.client.get(
            f"/api/agents/conversations/{self.legacy_conversation.id}/messages"
        )
        self.assertEqual(legacy.status_code, 404, legacy.text)

        self._as_viewer()
        viewer_conversations = self.client.get(f"/api/agents/{self.agent.id}/conversations")
        self.assertEqual(viewer_conversations.status_code, 200, viewer_conversations.text)
        self.assertEqual(
            [item["id"] for item in viewer_conversations.json()],
            [self.viewer_conversation.id],
        )
        foreign_messages = self.client.get(
            f"/api/agents/conversations/{self.conversation.id}/messages"
        )
        self.assertEqual(foreign_messages.status_code, 404, foreign_messages.text)
        foreign_chat = self.client.post(
            f"/api/agents/{self.agent.id}/chat",
            json={"message": "不能复用其他人的对话", "conversation_id": self.conversation.id},
        )
        self.assertEqual(foreign_chat.status_code, 404, foreign_chat.text)

        own_delete = self.client.delete(
            f"/api/agents/conversations/{self.viewer_conversation.id}"
        )
        self.assertEqual(own_delete.status_code, 200, own_delete.text)

        created = self.client.post(f"/api/agents/{self.agent.id}/conversations")
        self.assertEqual(created.status_code, 200, created.text)
        db = self.Session()
        try:
            conversation = db.get(Conversation, created.json()["id"])
            self.assertEqual(conversation.created_by_user_id, self.viewer.id)
        finally:
            db.close()

    def test_assistant_threads_are_owner_scoped(self) -> None:
        self._as_viewer()
        listed = self.client.get("/api/assistant/threads?path=/dashboard")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), [])

        foreign = self.client.get(
            f"/api/assistant/threads/{self.owner_thread.id}/messages?path=/dashboard"
        )
        self.assertEqual(foreign.status_code, 404, foreign.text)

        created = self.client.post("/api/assistant/threads?path=/dashboard")
        self.assertEqual(created.status_code, 200, created.text)
        db = self.Session()
        try:
            thread = db.get(AssistantThread, created.json()["id"])
            self.assertEqual(thread.created_by_user_id, self.viewer.id)
        finally:
            db.close()

    def test_assistant_attachments_are_owner_scoped(self) -> None:
        self._as_viewer()
        foreign = self.client.post(
            "/api/assistant/chat",
            json={
                "message": "不能读取其他成员附件",
                "path": "/dashboard",
                "attachment_ids": [self.owner_attachment.id],
            },
        )
        self.assertEqual(foreign.status_code, 409, foreign.text)
        self.assertNotIn(self.owner_attachment.id, foreign.text)
        self.assertNotIn(self.owner_attachment.parsed_text, foreign.text)
        deleted = self.client.delete(f"/api/assistant/attachments/{self.owner_attachment.id}")
        self.assertEqual(deleted.status_code, 404, deleted.text)

        self.current_user_id = self.owner.id
        own = self.client.post(
            "/api/assistant/chat",
            json={
                "message": "读取自己的附件",
                "path": "/dashboard",
                "attachment_ids": [self.owner_attachment.id],
            },
        )
        self.assertEqual(own.status_code, 200, own.text)
        self.assertEqual([item["id"] for item in own.json()["sources"]], [self.owner_attachment.id])

    def test_removed_member_is_not_recreated_by_bootstrap_or_membership_fallback(self) -> None:
        members = self.client.get("/api/permissions/members")
        self.assertEqual(members.status_code, 200, members.text)
        member_id = next(item["id"] for item in members.json() if item["user_id"] == self.viewer.id)
        removed = self.client.delete(f"/api/permissions/members/{member_id}")
        self.assertEqual(removed.status_code, 200, removed.text)

        db = self.Session()
        try:
            permission_service.ensure_organization(db, self.tenant.id)
            db.commit()
            member = db.scalars(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == self.organization_id,
                    OrganizationMember.user_id == self.viewer.id,
                )
            ).one()
            self.assertEqual(member.status, "removed")
            self.assertFalse(permission_service.ensure_user_membership(db, self.viewer))

            token = "removed-member-test-token"
            db.add(
                AuthSession(
                    user_id=self.viewer.id,
                    token_hash=auth_service._token_hash(token),
                    expires_at=auth_service.utc_now() + timedelta(hours=1),
                )
            )
            db.commit()
            cookie_name = get_settings().auth_cookie_name.encode("utf-8")
            request = Request(
                {
                    "type": "http",
                    "headers": [(b"cookie", cookie_name + b"=" + token.encode("utf-8"))],
                }
            )
            with self.assertRaises(HTTPException) as error:
                auth_service.get_current_user(request, db)
            self.assertEqual(error.exception.status_code, 403)
        finally:
            db.close()

    def test_conversation_ownership_migration_preserves_legacy_rows_as_unowned(self) -> None:
        """An old transcript gets the field/index but no guessed owner backfill."""
        migration_engine = create_engine("sqlite:///:memory:")
        try:
            with migration_engine.begin() as conn:
                conn.exec_driver_sql(
                    "CREATE TABLE conversations ("
                    "id VARCHAR(32) PRIMARY KEY, agent_id VARCHAR(32), title VARCHAR(300)"
                    ")"
                )
                conn.exec_driver_sql(
                    "INSERT INTO conversations (id, agent_id, title) "
                    "VALUES ('legacy-conversation', 'agent-legacy', '旧对话')"
                )
            original_engine = database.engine
            database.engine = migration_engine
            try:
                database._migrate_conversation_ownership()
            finally:
                database.engine = original_engine

            with migration_engine.connect() as conn:
                owner = conn.exec_driver_sql(
                    "SELECT created_by_user_id FROM conversations WHERE id = 'legacy-conversation'"
                ).scalar_one()
            self.assertIsNone(owner)
            index_names = {index["name"] for index in inspect(migration_engine).get_indexes("conversations")}
            self.assertIn("ix_conversations_created_by_user_id", index_names)
        finally:
            migration_engine.dispose()


if __name__ == "__main__":
    unittest.main()
