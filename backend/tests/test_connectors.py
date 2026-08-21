"""P2 connector registry / environment-binding regression coverage."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    LLMConfig,
    MCPConfig,
    OntologyAction,
    OntologyEntity,
    OntologyRelease,
    OntologySnapshot,
    Tenant,
    User,
)
from app.routers import connectors
from app.services import (
    connector_service,
    package_service,
    permission_service,
    release_service,
    runtime_connector_service,
    workflow_service,
)
from app.services.auth_service import get_tenant_db


class ConnectorGovernanceTests(unittest.TestCase):
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
            self.tenant = Tenant(id="tenant-connectors", name="连接器组织")
            self.other_tenant = Tenant(id="tenant-connectors-other", name="外部组织")
            self.owner = User(
                id="owner-connectors", tenant_id=self.tenant.id,
                email="owner-connectors@example.test", password_hash="test", status="active",
            )
            self.reviewer = User(
                id="reviewer-connectors", tenant_id=self.tenant.id,
                email="reviewer-connectors@example.test", password_hash="test", status="active",
            )
            self.outsider = User(
                id="outsider-connectors", tenant_id=self.other_tenant.id,
                email="outsider-connectors@example.test", password_hash="test", status="active",
            )
            self.scenario = BusinessScenario(
                id="scenario-connectors", tenant_id=self.tenant.id, name="连接器治理场景"
            )
            self.bucket = DataSource(
                id="connector-bucket", tenant_id=self.tenant.id, scenario_id=self.scenario.id,
                name="导入资料桶", type="file_bucket", config={"token": "must-not-leak"},
            )
            self.foreign = DataSource(
                id="connector-foreign", tenant_id=self.other_tenant.id,
                name="外部数据源", type="file_bucket", config={"token": "foreign-secret"},
            )
            self.mcp = MCPConfig(
                id="connector-mcp", tenant_id=self.tenant.id, name="审计工具",
                transport="stdio", command="mock-mcp", enabled=True,
            )
            self.staging_mcp = MCPConfig(
                id="connector-mcp-staging", tenant_id=self.tenant.id, name="审计工具-预发",
                transport="stdio", command="mock-mcp-staging", enabled=True,
            )
            self.url_mcp = MCPConfig(
                id="connector-mcp-url", tenant_id=self.tenant.id, name="HTTP 审计工具",
                transport="streamable_http", url="https://mcp-one.example.test/api", enabled=True,
            )
            self.llm = LLMConfig(
                id="connector-llm", tenant_id=self.tenant.id, name="运营模型",
                provider="openai", model="test-model", capabilities=["chat", "tool"], enabled=True,
                base_url="https://llm-one.example.test/v1", api_key="never-return-this",
            )
            db.add_all([
                self.tenant, self.other_tenant, self.owner, self.reviewer, self.outsider,
                self.scenario, self.bucket, self.foreign, self.mcp, self.staging_mcp,
                self.url_mcp, self.llm,
            ])
            db.commit()
            organization = permission_service.ensure_organization(
                db, self.tenant.id, owner_user_id=self.owner.id
            )
            permission_service.assign_member_role(
                db, organization, user_id=self.reviewer.id, role_key="admin"
            )
            permission_service.ensure_organization(
                db, self.other_tenant.id, owner_user_id=self.outsider.id
            )
            db.commit()
        finally:
            db.close()

        self.current_user = self.owner
        self.app = FastAPI()
        self.app.include_router(connectors.router, prefix="/api")

        def override_db():
            request_db = self.Session()
            request_db.info["user_id"] = self.current_user.id
            request_db.info["tenant_id"] = self.current_user.tenant_id
            try:
                yield request_db
            finally:
                request_db.close()

        self.app.dependency_overrides[get_tenant_db] = override_db
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    def _db(self, user: User | None = None) -> Session:
        db = self.Session()
        principal = user or self.owner
        db.info["user_id"] = principal.id
        db.info["tenant_id"] = principal.tenant_id
        return db

    def _healthy_binding(
        self,
        db: Session,
        *,
        kind: str,
        connector_id: str,
        key: str,
        environment: str = "dev",
    ):
        binding = connector_service.upsert_binding(
            db,
            self.scenario,
            environment=environment,
            binding_key_value=key,
            kind=kind,
            connector_id=connector_id,
            reference_label=f"测试 {kind}",
            created_by_user_id=self.owner.id,
        )
        connector = db.get({"mcp": MCPConfig, "llm": LLMConfig, "data_source": DataSource}[kind], connector_id)
        binding.health_status = "healthy"
        binding.health_message = ""
        binding.checked_at = datetime.now(timezone.utc)
        binding.connector_signature = connector_service.connector_signature(kind, connector)
        db.commit()
        return binding

    def _recheck_binding(
        self,
        db: Session,
        *,
        kind: str,
        connector_id: str,
        key: str,
        environment: str = "staging",
    ):
        """Exercise the real binding health-check path (with caller mocks)."""
        binding = connector_service.upsert_binding(
            db,
            self.scenario,
            environment=environment,
            binding_key_value=key,
            kind=kind,
            connector_id=connector_id,
            reference_label=f"复检 {kind}",
            check=True,
            created_by_user_id=self.owner.id,
        )
        db.commit()
        return binding

    def _published_staging_release_for_binding(
        self,
        db: Session,
        *,
        kind: str,
        connector_id: str,
        binding_key_value: str,
        reference: dict,
    ) -> tuple[OntologyRelease, dict]:
        """Publish a real release with one declarative connector requirement."""
        self._healthy_binding(
            db,
            kind=kind,
            connector_id=connector_id,
            key=binding_key_value,
            environment="dev",
        )
        self._healthy_binding(
            db,
            kind=kind,
            connector_id=connector_id,
            key=binding_key_value,
            environment="staging",
        )
        key_field, reference_field = connector_service.runtime_binding_fields(kind)
        runtime_config = {key_field: binding_key_value, reference_field: reference}
        content = release_service.capture_snapshot_content(db, self.scenario)
        content["connector_bindings"] = [{
            "binding_key": binding_key_value,
            "kind": kind,
            "environment": "dev",
            "reference_label": f"revision test {kind}",
        }]
        branch = release_service.create_branch(
            db,
            self.scenario.id,
            name=f"revision-{kind}/main",
            description="连接器修订版本回归",
        )
        proposal = release_service.create_proposal(
            db,
            branch.id,
            title=f"固定 {kind} 连接器",
            description="连接器修订版本回归",
            content=content,
        )
        db.info["user_id"] = self.reviewer.id
        release_service.create_review(db, proposal.id, decision="approve", comment="独立评审")
        db.info["user_id"] = self.owner.id
        release_service.merge_proposal(db, proposal.id, confirmed=True)
        release = release_service.publish_snapshot(
            db,
            self.scenario.id,
            environment="staging",
            confirmed=True,
            branch_id=branch.id,
        )
        return release, runtime_config

    def test_catalog_and_binding_route_are_credential_free_and_tenant_scoped(self) -> None:
        catalog = self.client.get(f"/api/connectors?scenario_id={self.scenario.id}")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        listed = catalog.json()
        bucket = next(item for item in listed if item["id"] == self.bucket.id)
        self.assertEqual(bucket["kind"], "data_source")
        self.assertNotIn("config", bucket)
        self.assertNotIn("must-not-leak", str(listed))

        key = connector_service.binding_key(
            "data_source", {"name": self.bucket.name, "type": self.bucket.type}
        )
        created = self.client.put(
            f"/api/connectors/scenarios/{self.scenario.id}/bindings",
            json={
                "environment": "dev",
                "binding_key": key,
                "kind": "data_source",
                "connector_id": self.bucket.id,
                "reference_label": "资源包导入资料桶",
                "check": True,
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        binding = created.json()
        self.assertTrue(binding["ready"])
        self.assertEqual(binding["health"], "healthy")
        self.assertEqual(binding["secret_state"], "not_required")
        self.assertNotIn("must-not-leak", str(binding))

        cross_tenant = self.client.put(
            f"/api/connectors/scenarios/{self.scenario.id}/bindings",
            json={
                "environment": "dev",
                "binding_key": "data_source:foreign:file_bucket",
                "kind": "data_source",
                "connector_id": self.foreign.id,
                "check": False,
            },
        )
        self.assertEqual(cross_tenant.status_code, 409, cross_tenant.text)

    def test_runtime_environment_is_fixed_by_the_server_deployment(self) -> None:
        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        ):
            self.assertEqual(runtime_connector_service.runtime_environment(), "staging")
            with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as error:
                runtime_connector_service.runtime_environment("dev")
        self.assertIn("当前部署环境 staging", str(error.exception))

    def _published_staging_mcp_release(self, db: Session) -> tuple[OntologyRelease, dict]:
        """Create a real release/audit pair used by pinned-runtime tests."""
        config = {
            "tool_name": "create_task",
            "mcp_binding_key": connector_service.binding_key(
                "mcp",
                {
                    "name": self.staging_mcp.name,
                    "adapter": "stdio",
                    "required_capabilities": ["tool"],
                },
            ),
            "mcp_binding_ref": {"adapter": "stdio", "required_capabilities": ["tool"]},
        }
        self._healthy_binding(
            db,
            kind="mcp",
            connector_id=self.staging_mcp.id,
            key=config["mcp_binding_key"],
            environment="staging",
        )
        entity = OntologyEntity(
            id="entity-pinned-release",
            scenario_id=self.scenario.id,
            name="固定发布对象",
        )
        action = OntologyAction(
            id="action-pinned-release",
            scenario_id=self.scenario.id,
            entity_id=entity.id,
            name="固定发布 MCP 操作",
            executor_type="mcp",
            executor_config=config,
        )
        db.add_all([entity, action])
        db.commit()
        branch = release_service.create_branch(
            db,
            self.scenario.id,
            name="pinned-release/main",
            description="固定运行时发布测试",
        )
        release = release_service.publish_snapshot(
            db,
            self.scenario.id,
            environment="staging",
            confirmed=True,
            branch_id=branch.id,
        )
        return release, config

    def test_explicit_release_pin_bypasses_only_live_hash_and_keeps_audit_strict(self) -> None:
        db = self._db()
        try:
            release, config = self._published_staging_mcp_release(db)
            scenario = db.get(BusinessScenario, self.scenario.id)
            self.assertIsNotNone(scenario)
            assert scenario is not None

            # A later dev-only definition edit invalidates the established
            # live-resource path, but a durable caller pinned to this release
            # still resolves the connector audited for this release.
            scenario.description = "仅开发环境的新定义"
            db.commit()
            with patch(
                "app.services.runtime_connector_service.get_settings",
                return_value=SimpleNamespace(runtime_environment="staging"),
            ):
                with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as live_error:
                    runtime_connector_service.resolve_connector(
                        db,
                        scenario,
                        kind="mcp",
                        config=config,
                        environment="staging",
                    )
                self.assertIn("当前本体定义", str(live_error.exception))

                connector, audit = runtime_connector_service.resolve_connector(
                    db,
                    scenario,
                    kind="mcp",
                    config=config,
                    environment="staging",
                    release_id=release.id,
                )
                self.assertEqual(connector.id, self.staging_mcp.id)
                self.assertEqual(audit["environment"], "staging")
                self.assertEqual(audit["binding_key"], config["mcp_binding_key"])

                # Pinning skips only the live-definition comparison.  A target
                # changed and rechecked after release is still rejected against
                # the immutable release audit.
                staging_mcp = db.get(MCPConfig, self.staging_mcp.id)
                self.assertIsNotNone(staging_mcp)
                assert staging_mcp is not None
                staging_mcp.command = "changed-after-release"
                db.commit()
                self._healthy_binding(
                    db,
                    kind="mcp",
                    connector_id=self.staging_mcp.id,
                    key=config["mcp_binding_key"],
                    environment="staging",
                )
                with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as audit_error:
                    runtime_connector_service.resolve_connector(
                        db,
                        scenario,
                        kind="mcp",
                        config=config,
                        environment="staging",
                        release_id=release.id,
                    )
                self.assertIn("发布后已变更", str(audit_error.exception))
        finally:
            db.close()

    def test_connector_revision_is_monotonic_for_configuration_updates(self) -> None:
        db = self._db()
        try:
            source = db.get(DataSource, self.bucket.id)
            self.assertIsNotNone(source)
            assert source is not None
            first_revision = source.connector_revision

            source.config = {"token": "rotated-but-same-key"}
            db.commit()
            self.assertEqual(source.connector_revision, first_revision + 1)

            # Returning to a prior value must create a new revision, and a
            # caller cannot manually restore the old release-compatible value.
            source.config = {"token": "must-not-leak"}
            source.connector_revision = first_revision
            db.commit()
            self.assertEqual(source.connector_revision, first_revision + 2)
        finally:
            db.close()

    def test_connector_revision_rejects_concurrent_stale_configuration_update(self) -> None:
        first = self._db()
        second = self._db()
        try:
            current = first.get(DataSource, self.bucket.id)
            stale = second.get(DataSource, self.bucket.id)
            self.assertIsNotNone(current)
            self.assertIsNotNone(stale)
            assert current is not None and stale is not None

            current.config = {"token": "first-writer"}
            first.commit()
            stale.config = {"token": "stale-writer"}
            with self.assertRaises(StaleDataError):
                second.commit()
        finally:
            first.close()
            second.rollback()
            second.close()

    def test_pinned_release_rejects_rechecked_same_shape_connector_changes(self) -> None:
        """A successful recheck cannot make an old release point to a new target.

        These were the three gaps in the old public-shape signature: MCP only
        recorded whether a URL existed, LLM only recorded whether base_url
        existed, and data sources recorded configuration keys but not values.
        """
        db = self._db()
        try:
            cases = [
                {
                    "kind": "data_source",
                    "connector_id": self.bucket.id,
                    "key": "revision-data-source",
                    "reference": {"adapter": "file_bucket"},
                    "model": DataSource,
                    "mutate": lambda connector: setattr(
                        connector, "config", {"token": "changed-same-key"}
                    ),
                },
                {
                    "kind": "mcp",
                    "connector_id": self.url_mcp.id,
                    "key": "revision-mcp-url",
                    "reference": {"adapter": "streamable_http"},
                    "model": MCPConfig,
                    "mutate": lambda connector: setattr(
                        connector, "url", "https://mcp-two.example.test/api"
                    ),
                },
                {
                    "kind": "llm",
                    "connector_id": self.llm.id,
                    "key": "revision-llm-base-url",
                    "reference": {"adapter": "openai"},
                    "model": LLMConfig,
                    "mutate": lambda connector: setattr(
                        connector, "base_url", "https://llm-two.example.test/v1"
                    ),
                },
            ]
            for case in cases:
                release, runtime_config = self._published_staging_release_for_binding(
                    db,
                    kind=case["kind"],
                    connector_id=case["connector_id"],
                    binding_key_value=case["key"],
                    reference=case["reference"],
                )
                audit = next(
                    item for item in release.connector_audit
                    if item["kind"] == case["kind"] and item["binding_key"] == case["key"]
                )
                connector = db.get(case["model"], case["connector_id"])
                self.assertIsNotNone(connector)
                assert connector is not None
                released_revision = connector.connector_revision
                self.assertEqual(audit["connector_revision"], released_revision)

                case["mutate"](connector)
                db.commit()
                self.assertEqual(connector.connector_revision, released_revision + 1)

                # Each physical connector can be made healthy again.  The
                # immutable release pin must nevertheless reject the old audit.
                with patch(
                    "app.services.connector_service.mcp_service.test_connection",
                    return_value=(True, "ok"),
                ), patch(
                    "app.services.connector_service.llm_service.test_connection",
                    return_value=(True, "ok"),
                ):
                    binding = self._recheck_binding(
                        db,
                        kind=case["kind"],
                        connector_id=case["connector_id"],
                        key=case["key"],
                        environment="staging",
                    )
                self.assertEqual(binding.health_status, "healthy")

                scenario = db.get(BusinessScenario, self.scenario.id)
                self.assertIsNotNone(scenario)
                assert scenario is not None
                with patch(
                    "app.services.runtime_connector_service.get_settings",
                    return_value=SimpleNamespace(runtime_environment="staging"),
                ):
                    with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as error:
                        runtime_connector_service.resolve_connector(
                            db,
                            scenario,
                            kind=case["kind"],
                            config=runtime_config,
                            environment="staging",
                            release_id=release.id,
                        )
                self.assertIn("发布后已变更", str(error.exception))
        finally:
            db.close()

    def test_explicit_release_pin_validates_scope_environment_and_snapshot(self) -> None:
        db = self._db()
        try:
            release, config = self._published_staging_mcp_release(db)
            scenario = db.get(BusinessScenario, self.scenario.id)
            self.assertIsNotNone(scenario)
            assert scenario is not None
            other_scenario = BusinessScenario(
                id="scenario-pinned-release-other",
                tenant_id=self.tenant.id,
                name="其他场景",
            )
            db.add(other_scenario)
            db.commit()

            with patch(
                "app.services.runtime_connector_service.get_settings",
                return_value=SimpleNamespace(runtime_environment="staging"),
            ):
                with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as scope_error:
                    runtime_connector_service.resolve_connector(
                        db,
                        other_scenario,
                        kind="mcp",
                        config=config,
                        environment="staging",
                        release_id=release.id,
                    )
                self.assertIn("不属于当前业务场景", str(scope_error.exception))

            # Use a matching server environment so this reaches the pinned
            # release's own environment assertion rather than the outer
            # deployment-environment assertion.
            with patch(
                "app.services.runtime_connector_service.get_settings",
                return_value=SimpleNamespace(runtime_environment="dev"),
            ):
                with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as environment_error:
                    runtime_connector_service.resolve_connector(
                        db,
                        scenario,
                        kind="mcp",
                        config=config,
                        environment="dev",
                        release_id=release.id,
                    )
                self.assertIn("不能用于 dev 运行时", str(environment_error.exception))

            snapshot = db.get(OntologySnapshot, release.snapshot_id)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            snapshot.content_hash = "tampered-snapshot-hash"
            db.commit()
            with patch(
                "app.services.runtime_connector_service.get_settings",
                return_value=SimpleNamespace(runtime_environment="staging"),
            ):
                with self.assertRaises(runtime_connector_service.RuntimeConnectorError) as snapshot_error:
                    runtime_connector_service.resolve_connector(
                        db,
                        scenario,
                        kind="mcp",
                        config=config,
                        environment="staging",
                        release_id=release.id,
                    )
                self.assertIn("快照完整性", str(snapshot_error.exception))
        finally:
            db.close()

    def test_mcp_llm_package_binding_is_rechecked_for_merge_and_each_environment(self) -> None:
        package = {
            "format": package_service.PACKAGE_FORMAT,
            "version": package_service.PACKAGE_VERSION,
            "manifest": {"name": "带连接器的动作包"},
            "resources": {
                "entities": [{
                    "key": "entity/工单", "name": "工单", "description": "",
                    "icon": "ticket", "color": "#2563eb", "is_abstract": False,
                }],
                "properties": [], "relations": [], "mappings": [],
                "actions": [{
                    "key": "action/entity/工单/创建外部任务",
                    "entity_ref": "entity/工单", "name": "创建外部任务", "description": "",
                    "input_schema": {}, "executor_type": "mcp",
                    "executor_config": {
                        "tool_name": "create_task",
                        "mcp_ref": {
                            "kind": "mcp", "name": "审计工具", "adapter": "stdio",
                            "required_capabilities": ["tool"],
                        },
                        "llm_ref": {
                            "kind": "llm", "name": "运营模型", "adapter": "openai",
                            "required_capabilities": ["chat"],
                        },
                    },
                    "precondition": "", "postcondition": "", "enabled": True,
                    "requires_confirmation": True, "idempotency_required": True,
                    "permission_scope": "scenario", "access_scope": "tenant",
                }],
                "rules": [], "events": [], "workflows": [],
            },
        }
        package["manifest"]["fingerprint"] = package_service.package_fingerprint(package)
        db = self._db()
        try:
            mcp_key = connector_service.binding_key("mcp", package["resources"]["actions"][0]["executor_config"]["mcp_ref"])
            llm_key = connector_service.binding_key("llm", package["resources"]["actions"][0]["executor_config"]["llm_ref"])
            self._healthy_binding(db, kind="mcp", connector_id=self.mcp.id, key=mcp_key)
            self._healthy_binding(db, kind="llm", connector_id=self.llm.id, key=llm_key)

            preview = package_service.plan_package_import(db, self.scenario, package, environment="dev")
            self.assertTrue(preview["applicable"], preview)
            self.assertEqual({item["kind"] for item in preview["proposal"]["resolved_bindings"]}, {"mcp", "llm"})

            branch = release_service.create_branch(
                db, self.scenario.id, name="connectors/main", description="连接器导入"
            )
            proposal, _fingerprint, _summary = package_service.create_governed_import_proposal(
                db,
                self.scenario,
                branch_id=branch.id,
                package=package,
                environment="dev",
                title="导入带连接器动作",
            )
            snapshot = db.get(OntologySnapshot, proposal.proposed_snapshot_id)
            self.assertEqual({item["kind"] for item in snapshot.content["connector_bindings"]}, {"mcp", "llm"})
            action = snapshot.content["actions"][0]
            self.assertEqual(action["executor_config"]["mcp_id"], self.mcp.id)
            self.assertEqual(action["executor_config"]["llm_config_id"], self.llm.id)
            self.assertEqual(action["executor_config"]["mcp_binding_key"], mcp_key)
            self.assertEqual(
                action["executor_config"]["mcp_binding_ref"],
                {"adapter": "stdio", "required_capabilities": ["tool"]},
            )

            db.info["user_id"] = self.reviewer.id
            review = release_service.create_review(db, proposal.id, decision="approve", comment="独立评审")
            self.assertEqual(review.decision, "approve")
            db.info["user_id"] = self.owner.id
            merged = release_service.merge_proposal(db, proposal.id, confirmed=True)
            self.assertEqual(merged.status, "merged")

            dev_release = release_service.publish_snapshot(
                db, self.scenario.id, environment="dev", confirmed=True, branch_id=branch.id
            )
            self.assertEqual(len(dev_release.connector_audit), 2)
            with self.assertRaises(release_service.ReleaseConflictError):
                release_service.publish_snapshot(
                    db, self.scenario.id, environment="staging", confirmed=True, branch_id=branch.id
                )
            self.assertEqual(
                db.execute(
                    select(OntologyRelease).where(
                        OntologyRelease.scenario_id == self.scenario.id,
                        OntologyRelease.environment == "staging",
                    )
                ).scalars().all(),
                [],
            )
            self._healthy_binding(
                db, kind="mcp", connector_id=self.staging_mcp.id, key=mcp_key, environment="staging"
            )
            self._healthy_binding(db, kind="llm", connector_id=self.llm.id, key=llm_key, environment="staging")
            staged = release_service.publish_snapshot(
                db, self.scenario.id, environment="staging", confirmed=True, branch_id=branch.id
            )
            self.assertEqual(len(staged.connector_audit), 2)

            live_action = db.execute(
                select(OntologyAction).where(OntologyAction.scenario_id == self.scenario.id)
            ).scalars().one()
            self.assertEqual(live_action.executor_config["mcp_id"], self.mcp.id)
            self.assertEqual(live_action.executor_config["llm_config_id"], self.llm.id)

            # The same live Action uses the physical target appropriate for the
            # server environment.  No action JSON or credential was copied when
            # staging selected its own MCP binding.
            with patch(
                "app.services.runtime_connector_service.get_settings",
                return_value=SimpleNamespace(runtime_environment="staging"),
            ):
                with patch(
                    "app.services.workflow_service.mcp_service.call_tool",
                    return_value={"status": "success", "text": "staging"},
                ) as call_tool:
                    runtime_result = workflow_service.execute_action(
                        db,
                        live_action,
                        {},
                        confirm=True,
                        idempotency_key="runtime-staging-connector",
                        runtime_environment="staging",
                    )
                self.assertEqual(runtime_result["status"], "success", runtime_result)
                self.assertEqual(call_tool.call_args.args[0].id, self.staging_mcp.id)
                self.assertEqual(runtime_result["connector_audit"][0]["environment"], "staging")
                self.assertEqual(runtime_result["connector_audit"][0]["binding_key"], mcp_key)

                # Rebinding a healthy key after publication does not silently change
                # a staging runtime: the immutable release audit blocks it.
                self._healthy_binding(
                    db, kind="mcp", connector_id=self.mcp.id, key=mcp_key, environment="staging"
                )
                blocked = workflow_service.execute_action(
                    db,
                    live_action,
                    {},
                    confirm=True,
                    idempotency_key="runtime-staging-rebound",
                    runtime_environment="staging",
                )
                self.assertEqual(blocked["status"], "failed", blocked)
                self.assertIn("发布后已变更", blocked["error"])
        finally:
            db.close()

    def test_explicit_data_source_binding_recovers_a_binding_required_package(self) -> None:
        """A safe opaque source reference becomes usable only after a binding.

        Exporters use ``binding_required`` when they cannot disclose a portable
        source identity.  The import compiler must accept the revalidated
        explicit binding rather than falling back to an unavailable name/type
        match (or permanently rejecting the package after the UI recovery).
        """
        source_ref = {
            "kind": "data_source",
            "binding_required": True,
            "name": "受控资料桶",
            "type": "file_bucket",
            "required_capabilities": ["document_search"],
        }
        package = {
            "format": package_service.PACKAGE_FORMAT,
            "version": package_service.PACKAGE_VERSION,
            "manifest": {"name": "受控资料映射包"},
            "resources": {
                "entities": [{
                    "key": "entity/资料", "name": "资料", "description": "",
                    "icon": "document", "color": "#2563eb", "is_abstract": False,
                }],
                "properties": [], "relations": [],
                "mappings": [{
                    "key": "mapping/entity/资料/data-source/受控资料桶/file_bucket/documents",
                    "entity_ref": "entity/资料",
                    "data_source_ref": source_ref,
                    "table_name": "documents",
                    "column_map": {},
                }],
                "actions": [], "rules": [], "events": [], "workflows": [],
            },
        }
        package["manifest"]["fingerprint"] = package_service.package_fingerprint(package)
        db = self._db()
        try:
            key = connector_service.binding_key("data_source", source_ref)
            self._healthy_binding(db, kind="data_source", connector_id=self.bucket.id, key=key)
            plan = package_service.plan_package_import(db, self.scenario, package, environment="dev")
            self.assertTrue(plan["applicable"], plan)
            self.assertFalse(plan["proposal"]["required_bindings"])

            branch = release_service.create_branch(
                db, self.scenario.id, name="binding-required/main", description="显式数据源绑定"
            )
            proposal, _fingerprint, _summary = package_service.create_governed_import_proposal(
                db,
                self.scenario,
                branch_id=branch.id,
                package=package,
                environment="dev",
                title="导入受控资料映射",
            )
            snapshot = db.get(OntologySnapshot, proposal.proposed_snapshot_id)
            self.assertEqual(snapshot.content["mappings"][0]["data_source_id"], self.bucket.id)
            self.assertEqual(snapshot.content["connector_bindings"][0]["binding_key"], key)
        finally:
            db.close()

    def test_custom_mapping_binding_key_is_rechecked_and_preserved_in_governed_import(self) -> None:
        db = self._db()
        try:
            source = DataSource(
                id="connector-sqlite-orders",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="订单 SQLite",
                type="sqlite",
                config={"path": "./test-orders.db"},
            )
            db.add(source)
            db.commit()
            key = "orders-runtime-primary"
            self._healthy_binding(
                db,
                kind="data_source",
                connector_id=source.id,
                key=key,
            )
            package = {
                "format": package_service.PACKAGE_FORMAT,
                "version": package_service.PACKAGE_VERSION,
                "manifest": {"name": "自定义映射绑定包"},
                "resources": {
                    "entities": [{
                        "key": "entity/订单", "name": "订单", "description": "",
                        "icon": "receipt", "color": "#2563eb", "is_abstract": False,
                    }],
                    "properties": [], "relations": [],
                    "mappings": [{
                        "key": "mapping/entity/订单/data-source/订单-sqlite/sqlite/orders",
                        "entity_ref": "entity/订单",
                        "data_source_ref": {"name": "订单 SQLite", "type": "sqlite", "scope": "scenario"},
                        "data_source_binding_key": key,
                        "data_source_binding_ref": {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
                        "table_name": "orders",
                        "column_map": {},
                    }],
                    "actions": [], "rules": [], "events": [], "workflows": [],
                },
            }
            package["manifest"]["fingerprint"] = package_service.package_fingerprint(package)

            plan = package_service.plan_package_import(db, self.scenario, package, environment="dev")
            self.assertTrue(plan["applicable"], plan)
            self.assertTrue(any(item["binding_key"] == key for item in plan["proposal"]["resolved_bindings"]))

            branch = release_service.create_branch(
                db, self.scenario.id, name="custom-mapping-key/main", description="自定义映射绑定"
            )
            proposal, _fingerprint, _summary = package_service.create_governed_import_proposal(
                db,
                self.scenario,
                branch_id=branch.id,
                package=package,
                environment="dev",
                title="导入自定义映射绑定",
            )
            snapshot = db.get(OntologySnapshot, proposal.proposed_snapshot_id)
            assert snapshot is not None
            mapping = snapshot.content["mappings"][0]
            self.assertEqual(mapping["data_source_id"], source.id)
            self.assertEqual(mapping["data_source_binding_key"], key)
            self.assertEqual(mapping["data_source_binding_ref"], {"adapter": "sqlite", "required_capabilities": ["sql_read"]})
        finally:
            db.close()

    def test_legacy_mapping_package_update_keeps_existing_custom_binding(self) -> None:
        db = self._db()
        try:
            source = DataSource(
                id="connector-sqlite-existing",
                tenant_id=self.tenant.id,
                scenario_id=self.scenario.id,
                name="现有订单库",
                type="sqlite",
                config={"path": "./existing-orders.db"},
            )
            entity = OntologyEntity(
                id="connector-existing-entity",
                scenario_id=self.scenario.id,
                name="现有订单",
            )
            mapping = DataMapping(
                id="connector-existing-mapping",
                scenario_id=self.scenario.id,
                entity_id=entity.id,
                data_source_id=source.id,
                data_source_binding_key="existing-orders-binding",
                data_source_binding_ref={"adapter": "sqlite", "required_capabilities": ["sql_read"]},
                table_name="orders",
                column_map={"id": "id"},
            )
            db.add_all([source, entity, mapping])
            db.commit()
            self._healthy_binding(
                db,
                kind="data_source",
                connector_id=source.id,
                key="existing-orders-binding",
            )
            branch = release_service.create_branch(
                db, self.scenario.id, name="legacy-mapping-key/main", description="保留已有绑定"
            )
            package = package_service.export_scenario_package(db, self.scenario)
            package_mapping = package["resources"]["mappings"][0]
            package_mapping.pop("data_source_binding_key")
            package_mapping.pop("data_source_binding_ref")
            package_mapping["column_map"] = {"id": "order_id"}
            package["manifest"]["fingerprint"] = package_service.package_fingerprint(package)

            plan = package_service.plan_package_import(db, self.scenario, package, environment="dev")
            self.assertTrue(plan["applicable"], plan)
            mapping_change = next(
                item for item in plan["proposal"]["changes"] if item["resource_type"] == "mapping"
            )
            self.assertEqual(mapping_change["after"]["data_source_binding_key"], "existing-orders-binding")

            proposal, _fingerprint, _summary = package_service.create_governed_import_proposal(
                db,
                self.scenario,
                branch_id=branch.id,
                package=package,
                environment="dev",
                title="旧包更新映射",
            )
            snapshot = db.get(OntologySnapshot, proposal.proposed_snapshot_id)
            assert snapshot is not None
            imported_mapping = next(
                item for item in snapshot.content["mappings"] if item["id"] == mapping.id
            )
            self.assertEqual(imported_mapping["data_source_binding_key"], "existing-orders-binding")
            self.assertEqual(
                imported_mapping["data_source_binding_ref"],
                {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
            )
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
