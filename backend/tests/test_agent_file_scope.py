"""Regression tests for Agent-scoped managed file previews and downloads."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import (
    Agent,
    BucketFile,
    BusinessScenario,
    DataAsset,
    DataAssetVersion,
    DataSource,
    Tenant,
    User,
)
from app.routers import data_sources
from app.services import permission_service
from app.services.auth_service import get_current_user, get_tenant_db


class AgentFileScopeTests(unittest.TestCase):
    """A runtime file is readable only in its owning Agent context."""

    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.Session = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )
        Base.metadata.create_all(self.engine)
        db = self.Session()
        try:
            self.tenant = Tenant(id="tenant-file-scope", name="文件隔离租户")
            self.user = User(
                id="user-file-scope",
                tenant_id=self.tenant.id,
                email="file-scope@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-file-scope",
                tenant_id=self.tenant.id,
                name="文件隔离场景",
            )
            self.agent_one = Agent(
                id="agent-file-one",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="Agent 一",
            )
            self.agent_two = Agent(
                id="agent-file-two",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="Agent 二",
            )
            db.add_all(
                [self.tenant, self.user, self.scenario, self.agent_one, self.agent_two]
            )
            db.commit()

            self.runtime_one = DataSource(
                id="source-file-one",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                resource_scope="agent_runtime",
                owner_agent_id=self.agent_one.id,
                name="Agent 一运行时文件桶",
                type="file_bucket",
            )
            self.runtime_two = DataSource(
                id="source-file-two",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                resource_scope="agent_runtime",
                owner_agent_id=self.agent_two.id,
                name="Agent 二运行时文件桶",
                type="file_bucket",
            )
            self.modeling_source = DataSource(
                id="source-file-modeling",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                resource_scope="modeling",
                name="历史建模文件桶",
                type="file_bucket",
            )
            self.file_one = BucketFile(
                id="file-agent-one",
                data_source_id=self.runtime_one.id,
                filename="agent-one.txt",
                stored_path="/tmp/agent-one.txt",
                mime="text/plain",
                status="parsed",
                parsed_text="仅 Agent 一可见",
            )
            self.file_two = BucketFile(
                id="file-agent-two",
                data_source_id=self.runtime_two.id,
                filename="agent-two.txt",
                stored_path="/tmp/agent-two.txt",
                mime="text/plain",
                status="parsed",
                parsed_text="仅 Agent 二可见",
            )
            self.legacy_file = BucketFile(
                id="file-modeling-old",
                data_source_id=self.modeling_source.id,
                filename="legacy.txt",
                stored_path="/tmp/legacy.txt",
                mime="text/plain",
                status="parsed",
                parsed_text="旧建模文件仍可访问",
            )
            db.add_all(
                [
                    self.runtime_one,
                    self.runtime_two,
                    self.modeling_source,
                    self.file_one,
                    self.file_two,
                    self.legacy_file,
                ]
            )
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.user.id
            )
            # ``ensure_organization`` creates the owner membership; retaining
            # this local reference makes the setup explicit for future ACL
            # regressions without depending on a browser session.
            self.organization_id = organization.id
            db.commit()
        finally:
            db.close()

        self.app = FastAPI()
        self.app.include_router(data_sources.router, prefix="/api")

        def override_current_user():
            return SimpleNamespace(id=self.user.id, tenant_id=self.tenant.id)

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.user.id
            request_db.info["tenant_id"] = self.tenant.id
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

    def test_runtime_file_requires_exact_agent_context_for_text_and_download(self) -> None:
        for suffix in ("text", "download"):
            base = f"/api/data-sources/files/{self.file_one.id}/{suffix}"
            self.assertEqual(self.client.get(base).status_code, 404)
            self.assertEqual(
                self.client.get(f"{base}?agent_id={self.agent_two.id}").status_code,
                404,
            )

        text = self.client.get(
            f"/api/data-sources/files/{self.file_one.id}/text?agent_id={self.agent_one.id}"
        )
        self.assertEqual(text.status_code, 200, text.text)
        self.assertEqual(text.json()["text"], self.file_one.parsed_text)

        with patch.object(
            data_sources.datasource_service,
            "read_bucket_file",
            return_value=(b"agent-one", 9, "text/plain"),
        ):
            downloaded = self.client.get(
                f"/api/data-sources/files/{self.file_one.id}/download?agent_id={self.agent_one.id}"
            )
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.content, b"agent-one")

    def test_legacy_modeling_file_keeps_unscoped_link_compatibility(self) -> None:
        response = self.client.get(
            f"/api/data-sources/files/{self.legacy_file.id}/text"
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["text"], self.legacy_file.parsed_text)

        scoped = self.client.get(
            f"/api/data-sources/files/{self.legacy_file.id}/text?agent_id={self.agent_one.id}"
        )
        self.assertEqual(scoped.status_code, 200, scoped.text)

    def test_generic_rag_search_excludes_runtime_attachment_sources(self) -> None:
        """The unscoped modeling search must not become an Agent file oracle."""

        with patch.object(data_sources.rag_service, "search", return_value=[]) as search:
            response = self.client.post(
                "/api/data-sources/search",
                json={
                    "query": "仅 Agent 一可见",
                    "data_source_ids": [self.runtime_one.id],
                },
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["searched_data_source_ids"], [])
        search.assert_called_once()
        _db, source_ids, query, *rest = search.call_args.args
        self.assertEqual(source_ids, [])
        self.assertEqual(query, "仅 Agent 一可见")
        self.assertEqual(search.call_args.kwargs.get("top_k"), 5)

    def test_ownerless_private_runtime_file_is_not_global_readable(self) -> None:
        """A missing legacy owner cannot turn an Agent-purpose file public."""

        with self.Session() as db:
            shared_source = DataSource(
                id="source-file-private-ownerless",
                tenant_id=self.tenant.id,
                scenario_id=None,
                resource_scope="agent_runtime",
                owner_agent_id=None,
                name="历史私有附件桶",
                type="file_bucket",
            )
            private_file = BucketFile(
                id="file-private-ownerless",
                data_source_id=shared_source.id,
                filename="private.txt",
                stored_path="/tmp/private.txt",
                mime="text/plain",
                status="parsed",
                parsed_text="不应进入全局命名空间",
            )
            private_asset = DataAsset(
                id="asset-private-ownerless",
                tenant_id=self.tenant.id,
                owner_agent_id=None,
                key="private.ownerless.asset",
                name="历史私有附件",
                kind="file",
                media_type="text/plain",
                usage_plane="invocation_input",
                lifecycle_status="active",
                labels={"catalog_purpose": "invocation_attachment"},
                created_by_user_id=self.user.id,
            )
            private_version = DataAssetVersion(
                id="version-private-ownerless",
                tenant_id=self.tenant.id,
                asset_id=private_asset.id,
                version_number=1,
                bucket_file_id=private_file.id,
                bucket_data_source_id=shared_source.id,
                status="ready",
                content_sha256="a" * 64,
                byte_size=1,
                created_by_user_id=self.user.id,
            )
            db.add_all([shared_source, private_file, private_asset, private_version])
            db.commit()

        response = self.client.get(
            f"/api/data-sources/files/{private_file.id}/text"
        )
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.get(
            f"/api/data-sources/files/{private_file.id}/download"
        )
        self.assertEqual(response.status_code, 404, response.text)


if __name__ == "__main__":
    unittest.main()
