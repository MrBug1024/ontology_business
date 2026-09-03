"""P2 external API / SDK contract and security regressions."""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.external_api_models import ExternalApiKey, ExternalApiKeyAuditEvent
from app.models import (
    BusinessScenario,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    Tenant,
    User,
)
from app.routers import external_api
from app.services import permission_service
from app.services.auth_service import get_tenant_db
from sdk import ExternalApiError, OntologyPlatformClient
from tests.postgresql_migration_contracts import baseline_table_ddl


class ExternalApiTests(unittest.TestCase):
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
            self.tenant = Tenant(id="tenant-external", name="外部 API 组织")
            self.other_tenant = Tenant(id="tenant-external-other", name="其他组织")
            self.owner = User(
                id="owner-external",
                tenant_id=self.tenant.id,
                email="owner-external@example.test",
                password_hash="test-only",
                status="active",
            )
            self.viewer = User(
                id="viewer-external",
                tenant_id=self.tenant.id,
                email="viewer-external@example.test",
                password_hash="test-only",
                status="active",
            )
            self.admin = User(
                id="admin-external",
                tenant_id=self.tenant.id,
                email="admin-external@example.test",
                password_hash="test-only",
                status="active",
            )
            self.outsider = User(
                id="outsider-external",
                tenant_id=self.other_tenant.id,
                email="outsider-external@example.test",
                password_hash="test-only",
                status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-external",
                tenant_id=self.tenant.id,
                name="外部对象场景",
                description="可由集成读取",
                status="active",
            )
            self.other_scenario = BusinessScenario(
                id="scenario-external-other",
                tenant_id=self.other_tenant.id,
                name="隔离场景",
                status="active",
            )
            self.entity = OntologyEntity(
                id="entity-external",
                scenario_id=self.scenario.id,
                name="客户",
            )
            self.public_property = OntologyProperty(
                id="property-external-public",
                entity_id=self.entity.id,
                name="名称",
                data_type="string",
            )
            self.secret_property = OntologyProperty(
                id="property-external-secret",
                entity_id=self.entity.id,
                name="内部备注",
                data_type="string",
                is_sensitive=True,
            )
            self.instance = OntologyInstance(
                id="object-external",
                scenario_id=self.scenario.id,
                entity_id=self.entity.id,
                name="客户 A",
                attributes={"名称": "客户 A", "内部备注": "classified-code"},
            )
            db.add_all([
                self.tenant,
                self.other_tenant,
                self.owner,
                self.viewer,
                self.admin,
                self.outsider,
                self.scenario,
                self.other_scenario,
                self.entity,
                self.public_property,
                self.secret_property,
                self.instance,
            ])
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.viewer.id, role_key="viewer"
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.admin.id, role_key="admin"
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.outsider.id
            )
            db.commit()
        finally:
            db.close()

        self.current_user = self.owner
        self.app = FastAPI()
        self.app.include_router(external_api.management_router, prefix="/api")
        self.app.include_router(external_api.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.current_user.id
            request_db.info["tenant_id"] = self.current_user.tenant_id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.app.dependency_overrides[get_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _issue(
        self,
        *,
        name: str = "集成读取",
        scopes: list[str] | None = None,
        user_id: str | None = None,
    ) -> dict:
        payload: dict = {
            "name": name,
            "scopes": scopes or ["scenarios:read", "objects:read"],
            "expires_in_days": 30,
        }
        if user_id:
            payload["user_id"] = user_id
        response = self.client.post("/api/developer/api-keys", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_key_secret_is_one_time_hashed_and_revocable(self) -> None:
        created = self._issue()
        raw_token = created["token"]
        self.assertTrue(raw_token.startswith("ont_sk_"))
        self.assertNotIn("token_hash", created)
        self.assertEqual(created["issued_by_user_id"], self.owner.id)
        self.assertIsNone(created["revoked_by_user_id"])

        listed = self.client.get("/api/developer/api-keys")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertNotIn("token", listed.json()[0])
        self.assertNotIn(raw_token, str(listed.json()))

        db = self.Session()
        try:
            stored = db.get(ExternalApiKey, created["id"])
            self.assertIsNotNone(stored)
            self.assertNotEqual(stored.token_hash, raw_token)
            self.assertEqual(len(stored.token_hash), 64)
            self.assertEqual(stored.scopes, ["objects:read", "scenarios:read"])
            self.assertEqual(stored.issued_by_user_id, self.owner.id)
            self.assertIsNone(stored.revoked_by_user_id)
            issue_events = db.execute(
                select(ExternalApiKeyAuditEvent).where(
                    ExternalApiKeyAuditEvent.api_key_id == created["id"]
                )
            ).scalars().all()
            self.assertEqual(len(issue_events), 1)
            self.assertEqual(issue_events[0].event_type, "issued")
            self.assertEqual(issue_events[0].actor_user_id, self.owner.id)
            self.assertEqual(issue_events[0].subject_user_id, self.owner.id)
        finally:
            db.close()

        self.assertEqual(
            self.client.get("/api/external/v1/identity", headers={"X-API-Key": raw_token}).status_code,
            200,
        )
        revoked = self.client.delete(f"/api/developer/api-keys/{created['id']}")
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()["status"], "revoked")
        self.assertEqual(revoked.json()["revoked_by_user_id"], self.owner.id)
        self.assertNotIn("token", revoked.json())
        self.assertEqual(
            self.client.get("/api/external/v1/identity", headers={"X-API-Key": raw_token}).status_code,
            401,
        )

        db = self.Session()
        try:
            events = db.execute(
                select(ExternalApiKeyAuditEvent)
                .where(ExternalApiKeyAuditEvent.api_key_id == created["id"])
                .order_by(ExternalApiKeyAuditEvent.created_at.asc())
            ).scalars().all()
            self.assertEqual([event.event_type for event in events], ["issued", "revoked"])
            self.assertEqual(events[-1].actor_user_id, self.owner.id)
        finally:
            db.close()

    def test_external_v1_applies_scope_tenant_rbac_and_sensitive_filtering(self) -> None:
        # An owner may create a service key for a viewer, but the API call must
        # still execute as that constrained live identity.
        created = self._issue(name="viewer integration", user_id=self.viewer.id)
        headers = {"X-API-Key": created["token"]}

        identity = self.client.get("/api/external/v1/identity", headers=headers)
        self.assertEqual(identity.status_code, 200, identity.text)
        self.assertEqual(identity.json()["user_id"], self.viewer.id)
        self.assertNotIn("token", identity.text)

        scenarios = self.client.get("/api/external/v1/scenarios", headers=headers)
        self.assertEqual(scenarios.status_code, 200, scenarios.text)
        self.assertEqual([item["id"] for item in scenarios.json()], [self.scenario.id])

        entities = self.client.get(
            f"/api/external/v1/scenarios/{self.scenario.id}/entities", headers=headers
        )
        self.assertEqual(entities.status_code, 200, entities.text)
        self.assertEqual([prop["name"] for prop in entities.json()[0]["properties"]], ["名称"])

        objects = self.client.get(
            f"/api/external/v1/scenarios/{self.scenario.id}/objects", headers=headers
        )
        self.assertEqual(objects.status_code, 200, objects.text)
        self.assertEqual(objects.json()["total"], 1)
        self.assertEqual(objects.json()["items"][0]["attributes"], {"名称": "客户 A"})
        self.assertNotIn("classified-code", objects.text)

        # A hidden value may not influence a search match or pagination total.
        hidden_search = self.client.get(
            f"/api/external/v1/scenarios/{self.scenario.id}/objects?q=classified-code",
            headers=headers,
        )
        self.assertEqual(hidden_search.status_code, 200, hidden_search.text)
        self.assertEqual(hidden_search.json()["total"], 0)

        detail = self.client.get(
            f"/api/external/v1/scenarios/{self.scenario.id}/objects/{self.instance.id}",
            headers=headers,
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["attributes"], {"名称": "客户 A"})

        # External credentials are tenant-bound even if a foreign scenario is
        # later made public in the first-party application.
        foreign = self.client.get(
            f"/api/external/v1/scenarios/{self.other_scenario.id}/objects", headers=headers
        )
        self.assertEqual(foreign.status_code, 404)

    def test_scope_cookie_fallback_and_sdk_contract(self) -> None:
        narrow = self._issue(scopes=["scenarios:read"])
        headers = {"X-API-Key": narrow["token"]}
        self.assertEqual(
            self.client.get("/api/external/v1/scenarios", headers=headers).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                f"/api/external/v1/scenarios/{self.scenario.id}/objects", headers=headers
            ).status_code,
            403,
        )
        # A browser cookie alone must never grant an external API request.
        no_key = self.client.get(
            "/api/external/v1/identity", cookies={"ontology_session": "browser-only-token"}
        )
        self.assertEqual(no_key.status_code, 401)

        broad = self._issue(name="sdk integration")
        client = OntologyPlatformClient(
            "https://testserver/api/external/v1",
            broad["token"],
            http_client=self.client,
        )
        self.assertEqual(client.identity()["key_id"], broad["id"])
        self.assertEqual(client.list_scenarios()[0]["id"], self.scenario.id)
        self.assertEqual(client.list_entities(self.scenario.id)[0]["id"], self.entity.id)
        self.assertEqual(client.list_objects(self.scenario.id)["items"][0]["id"], self.instance.id)
        self.assertEqual(
            client.get_object(self.scenario.id, self.instance.id)["id"], self.instance.id
        )

        restricted_sdk = OntologyPlatformClient(
            "https://testserver/api/external/v1",
            narrow["token"],
            http_client=self.client,
        )
        with self.assertRaises(ExternalApiError) as raised:
            restricted_sdk.list_objects(self.scenario.id)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertNotIn(narrow["token"], str(raised.exception))

    def test_sdk_requires_https_and_refuses_redirects(self) -> None:
        with self.assertRaises(ValueError):
            OntologyPlatformClient("http://platform.example.test/api/external/v1", "ont_sk_test")
        with self.assertRaises(ValueError):
            OntologyPlatformClient(
                "http://platform.example.test/api/external/v1",
                "ont_sk_test",
                allow_insecure_http=True,
            )
        # Explicit local-only HTTP remains available for controlled development
        # and test adapters, including IPv6 loopback.
        OntologyPlatformClient(
            "http://localhost:8000/api/external/v1",
            "ont_sk_test",
            allow_insecure_http=True,
        ).close()
        OntologyPlatformClient(
            "http://[::1]:8000/api/external/v1",
            "ont_sk_test",
            allow_insecure_http=True,
        ).close()

        received_urls: list[str] = []

        def redirecting_transport(request: httpx.Request) -> httpx.Response:
            received_urls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"location": "https://credential-sink.example.test/collect"},
                request=request,
            )

        with httpx.Client(
            transport=httpx.MockTransport(redirecting_transport),
            follow_redirects=True,
        ) as redirecting_client:
            sdk_client = OntologyPlatformClient(
                "https://platform.example.test/api/external/v1",
                "ont_sk_test",
                http_client=redirecting_client,
            )
            with self.assertRaises(ExternalApiError) as raised:
                sdk_client.identity()
        self.assertEqual(raised.exception.status_code, 302)
        self.assertEqual(received_urls, ["https://platform.example.test/api/external/v1/identity"])

    def test_owner_issuer_and_admin_revoker_are_durably_audited(self) -> None:
        created = self._issue(name="owner-issued viewer key", user_id=self.viewer.id)
        self.assertEqual(created["user_id"], self.viewer.id)
        self.assertEqual(created["issued_by_user_id"], self.owner.id)

        self.current_user = self.admin
        revoked = self.client.delete(f"/api/developer/api-keys/{created['id']}")
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertEqual(revoked.json()["revoked_by_user_id"], self.admin.id)
        # A later management call is idempotent and must not rewrite the
        # original revoker or append a misleading second revocation event.
        self.current_user = self.owner
        repeated = self.client.delete(f"/api/developer/api-keys/{created['id']}")
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertEqual(repeated.json()["revoked_by_user_id"], self.admin.id)

        db = self.Session()
        try:
            key = db.get(ExternalApiKey, created["id"])
            self.assertIsNotNone(key)
            self.assertEqual(key.issued_by_user_id, self.owner.id)
            self.assertEqual(key.revoked_by_user_id, self.admin.id)
            events = db.execute(
                select(ExternalApiKeyAuditEvent)
                .where(ExternalApiKeyAuditEvent.api_key_id == created["id"])
                .order_by(ExternalApiKeyAuditEvent.created_at.asc())
            ).scalars().all()
            self.assertEqual(
                [(event.event_type, event.actor_user_id, event.subject_user_id) for event in events],
                [
                    ("issued", self.owner.id, self.viewer.id),
                    ("revoked", self.admin.id, self.viewer.id),
                ],
            )
        finally:
            db.close()

    def test_postgresql_schema_keeps_external_api_actors_nullable(self) -> None:
        key_ddl = baseline_table_ddl("external_api_keys")
        audit_ddl = baseline_table_ddl("external_api_key_audit_events")

        self.assertIn("issued_by_user_id VARCHAR(32),", key_ddl)
        self.assertIn("revoked_by_user_id VARCHAR(32),", key_ddl)
        self.assertIn(
            "FOREIGN KEY(issued_by_user_id) REFERENCES users (id) ON DELETE SET NULL",
            key_ddl,
        )
        self.assertIn(
            "FOREIGN KEY(revoked_by_user_id) REFERENCES users (id) ON DELETE SET NULL",
            key_ddl,
        )
        self.assertIn("subject_user_id VARCHAR(32),", audit_ddl)
        self.assertIn("actor_user_id VARCHAR(32),", audit_ddl)
        self.assertIn(
            "FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE SET NULL",
            audit_ddl,
        )

    def test_object_listing_is_bounded_with_limit_one_and_acl_safe(self) -> None:
        # More rows than a single external page must not force a full ACL walk.
        # The response reports that its compatibility ``total`` is incomplete
        # and provides a source continuation that progresses even with ACLs.
        db = self.Session()
        try:
            db.add_all(
                [
                    OntologyInstance(
                        id=f"object-external-bound-{index:04d}",
                        scenario_id=self.scenario.id,
                        entity_id=self.entity.id,
                        name=f"边界客户 {index}",
                        attributes={"名称": f"边界客户 {index}"},
                    )
                    for index in range(1_005)
                ]
            )
            db.add(
                OntologyInstance(
                    id="object-external-restricted",
                    scenario_id=self.scenario.id,
                    entity_id=self.entity.id,
                    name="仅受限对象",
                    attributes={"名称": "仅受限对象"},
                    access_scope="restricted",
                )
            )
            db.commit()
        finally:
            db.close()

        created = self._issue(name="bounded objects integration")
        headers = {"X-API-Key": created["token"]}
        object_sql: list[tuple[str, object]] = []

        def capture_object_query(
            _connection, _cursor, statement, parameters, _context, _executemany
        ) -> None:
            if "FROM ontology_instances" in statement:
                object_sql.append((statement, parameters))

        event.listen(self.engine, "before_cursor_execute", capture_object_query)
        try:
            first = self.client.get(
                f"/api/external/v1/scenarios/{self.scenario.id}/objects?limit=1", headers=headers
            )
        finally:
            event.remove(self.engine, "before_cursor_execute", capture_object_query)
        self.assertEqual(first.status_code, 200, first.text)
        first_payload = first.json()
        self.assertEqual(len(first_payload["items"]), 1)
        self.assertTrue(first_payload["has_more"])
        self.assertFalse(first_payload["total_is_exact"])
        # The bounded query only counts its fixed candidate window rather than
        # loading all 1,006 rows before applying the one-item page.
        self.assertEqual(first_payload["total"], 1_000)
        self.assertTrue(object_sql)
        self.assertTrue(any("LIMIT" in statement.upper() for statement, _ in object_sql))
        bound = external_api.MAX_EXTERNAL_OBJECT_CANDIDATES + 1
        flattened_parameters = [
            value
            for _, parameters in object_sql
            for value in (parameters.values() if isinstance(parameters, dict) else parameters)
        ]
        self.assertIn(bound, flattened_parameters)

        viewer_key = self._issue(name="viewer acl listing", user_id=self.viewer.id)
        denied = self.client.get(
            f"/api/external/v1/scenarios/{self.scenario.id}/objects",
            params={"q": "仅受限对象", "limit": 1},
            headers={"X-API-Key": viewer_key["token"]},
        )
        self.assertEqual(denied.status_code, 200, denied.text)
        self.assertEqual(denied.json()["items"], [])
        self.assertEqual(denied.json()["total"], 0)
        self.assertTrue(denied.json()["total_is_exact"])

    def test_non_manager_cannot_issue_keys(self) -> None:
        self.current_user = self.viewer
        response = self.client.post(
            "/api/developer/api-keys",
            json={"name": "viewer cannot issue", "scopes": ["scenarios:read"]},
        )
        self.assertEqual(response.status_code, 403)

    def test_malformed_persisted_scope_fails_closed(self) -> None:
        created = self._issue(name="corrupt scope regression")
        db = self.Session()
        try:
            key = db.get(ExternalApiKey, created["id"])
            key.scopes = ["future:unreviewed"]
            db.commit()
        finally:
            db.close()
        denied = self.client.get(
            "/api/external/v1/identity", headers={"X-API-Key": created["token"]}
        )
        self.assertEqual(denied.status_code, 401)
        listed = self.client.get("/api/developer/api-keys")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()[0]["status"], "revoked")
        self.assertEqual(listed.json()[0]["scopes"], [])

    def test_admin_cannot_mint_a_key_for_another_identity(self) -> None:
        self.current_user = self.admin
        own_key = self.client.post(
            "/api/developer/api-keys",
            json={"name": "admin own integration", "scopes": ["scenarios:read"]},
        )
        self.assertEqual(own_key.status_code, 201, own_key.text)
        escalation = self.client.post(
            "/api/developer/api-keys",
            json={
                "name": "admin cannot impersonate owner",
                "user_id": self.owner.id,
                "scopes": ["scenarios:read", "objects:read"],
            },
        )
        self.assertEqual(escalation.status_code, 403)


if __name__ == "__main__":
    unittest.main()
