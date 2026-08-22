from __future__ import annotations

import hashlib
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AssistantMessage,
    AssistantThread,
    AuthorizationGrant,
    BucketFile,
    BusinessScenario,
    DataSource,
    DocumentChunk,
    Tenant,
    User,
)
from app.routers import assistant
from app.services import permission_service, rag_service


class AssistantRagHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-assistant-rag", name="助手资料测试")
        self.user = User(
            id="user-assistant-rag",
            tenant_id=self.tenant.id,
            email="assistant-rag@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-assistant-rag",
            tenant_id=self.tenant.id,
            name="助手资料场景",
        )
        self.source = DataSource(
            id="source-assistant-rag",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="助手资料库",
            type="file_bucket",
            config={},
        )
        self.secret = "仅用于历史回归的受控资料"
        self.file_hash = hashlib.sha256(self.secret.encode("utf-8")).hexdigest()
        self.file = BucketFile(
            id="file-assistant-rag",
            data_source_id=self.source.id,
            filename="受控资料.md",
            stored_path="/tmp/assistant-rag.md",
            status="parsed",
            parsed_text=self.secret,
            index_status="indexed",
            index_version=rag_service.INDEX_VERSION,
            indexed_content_hash=self.file_hash,
        )
        self.chunk = DocumentChunk(
            id="chunk-assistant-rag",
            bucket_file_id=self.file.id,
            data_source_id=self.source.id,
            ordinal=0,
            char_start=0,
            char_end=len(self.secret),
            text=self.secret,
            content_hash="chunk-assistant-rag-v1",
            embedding=[],
        )
        self.thread = AssistantThread(
            id="thread-assistant-rag",
            tenant_id=self.tenant.id,
            created_by_user_id=self.user.id,
            scenario_id=self.scenario.id,
            scope_key=f"scenario:{self.scenario.id}|path:/scenarios/{self.scenario.id}",
        )
        self.answer = AssistantMessage(
            id="message-assistant-rag",
            thread_id=self.thread.id,
            role="assistant",
            content=f"回答包含 {self.secret}",
            attachments=[
                {
                    "id": f"rag:C1:{self.chunk.id}",
                    "kind": "rag",
                    "citation_id": "C1",
                    "filename": "C1 · 受控资料.md",
                    "status": "cited",
                    "data_source_id": self.source.id,
                    "file_id": self.file.id,
                    "chunk_id": self.chunk.id,
                    "content_hash": self.chunk.content_hash,
                    "file_content_hash": self.file_hash,
                    "index_version": rag_service.INDEX_VERSION,
                }
            ],
            proposal={"proposal_id": "proposal-assistant-rag", "kind": "ontology", "payload": {"secret": self.secret}},
            thinking=[{"id": "reason", "detail": self.secret}],
        )
        self.db.add_all(
            [
                self.tenant,
                self.user,
                self.scenario,
                self.source,
                self.file,
                self.chunk,
                self.thread,
                self.answer,
            ]
        )
        self.db.commit()
        self.organization = permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_rag_source_persists_file_hash(self) -> None:
        result = {
            "citation_id": "C1",
            "chunk_id": self.chunk.id,
            "file_id": self.file.id,
            "filename": self.file.filename,
            "data_source_id": self.source.id,
            "data_source_name": self.source.name,
            "content_hash": self.chunk.content_hash,
            "file_content_hash": self.file_hash,
            "index_version": rag_service.INDEX_VERSION,
            "char_start": 0,
            "char_end": len(self.secret),
            "text": self.secret,
        }
        with patch("app.routers.assistant.rag_service.search", return_value=[result]):
            _context, sources = assistant._authorized_rag_context(
                self.db, self.scenario, "受控资料"
            )

        self.assertEqual(sources[0]["file_content_hash"], self.file_hash)
        self.assertEqual(sources[0]["content_hash"], self.chunk.content_hash)
        self.assertEqual(sources[0]["kind"], "rag")

    def test_changed_or_rebound_rag_source_redacts_history_prompt_and_proposal(self) -> None:
        visible = assistant.list_thread_messages(
            self.thread.id,
            scenario_id=self.scenario.id,
            path=f"/scenarios/{self.scenario.id}",
            db=self.db,
        )
        self.assertIn(self.secret, visible[0].content)
        self.assertIn(self.secret, assistant._history_messages(self.db, self.thread, "new-message")[0]["content"])

        # Parsing changed but old index metadata remains: the historic answer
        # must not be displayed or fed to the next assistant prompt.
        self.file.parsed_text = "资料已替换"
        self.db.flush()
        redacted = assistant.list_thread_messages(
            self.thread.id,
            scenario_id=self.scenario.id,
            path=f"/scenarios/{self.scenario.id}",
            db=self.db,
        )[0]
        self.assertNotIn(self.secret, redacted.content)
        self.assertEqual(redacted.attachments, [])
        self.assertEqual(redacted.proposal, {})
        self.assertEqual(redacted.thinking, [])
        prompt_history = assistant._history_messages(self.db, self.thread, "new-message")
        self.assertNotIn(self.secret, prompt_history[0]["content"])
        with self.assertRaises(HTTPException) as raised:
            assistant._find_saved_proposal(self.db, self.thread.id, "proposal-assistant-rag")
        self.assertEqual(raised.exception.status_code, 409)

        # A source that is moved away from the thread scenario is equally
        # invalid even when the document hash itself did not change.
        self.file.parsed_text = self.secret
        self.source.scenario_id = "another-scenario"
        self.db.flush()
        self.assertEqual(
            assistant._assistant_message_out(self.db, self.thread, self.answer).attachments,
            [],
        )

    def test_public_foreign_source_is_not_durable_assistant_context(self) -> None:
        # ``get_visible`` alone would admit this source because it is public.
        # A private assistant thread nevertheless requires source tenancy to
        # match the current tenant before a stored answer can be reused.
        self.source.tenant_id = "foreign-tenant"
        self.source.is_public = True
        self.db.flush()

        output = assistant._assistant_message_out(self.db, self.thread, self.answer)
        self.assertEqual(output.attachments, [])
        self.assertNotIn(self.secret, output.content)

    def test_scenario_acl_revocation_redacts_stored_rag_answer(self) -> None:
        self.db.add(
            AuthorizationGrant(
                organization_id=self.organization.id,
                user_id=self.user.id,
                resource_type="scenario",
                resource_id=self.scenario.id,
                verb="read",
                effect="deny",
                created_by_user_id=self.user.id,
            )
        )
        self.db.commit()

        output = assistant._assistant_message_out(self.db, self.thread, self.answer)
        self.assertNotIn(self.secret, output.content)
        self.assertEqual(output.attachments, [])


if __name__ == "__main__":
    unittest.main()
