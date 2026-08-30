from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyProperty,
    Tenant,
    User,
)
from app.services import connector_service, permission_service, release_service


class MappingReleaseBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-mapping-release", name="映射发布租户")
        self.user = User(
            id="user-mapping-release",
            tenant_id=self.tenant.id,
            email="mapping-release@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-mapping-release",
            tenant_id=self.tenant.id,
            name="映射发布场景",
        )
        self.entity = OntologyEntity(
            id="entity-mapping-release",
            scenario_id=self.scenario.id,
            name="订单",
        )
        self.key = OntologyProperty(
            id="property-mapping-release-key",
            entity_id=self.entity.id,
            name="订单编号",
            is_key=True,
            is_title=True,
            is_required=True,
        )
        self.db.add_all(
            [self.tenant, self.user, self.scenario, self.entity, self.key]
        )
        self.db.commit()
        permission_service.ensure_organization(
            self.db, self.tenant.id, owner_user_id=self.user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = self.tenant.id
        self.db.info["user_id"] = self.user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _branch_snapshot(self):
        branch = release_service.create_branch(
            self.db,
            self.scenario.id,
            name="mapping-runtime/main",
            description="映射发布测试",
        )
        snapshot = self.db.get(release_service.OntologySnapshot, branch.head_snapshot_id)
        assert snapshot is not None
        return snapshot

    def _healthy_binding(self, *, key: str, connector_id: str, environment: str = "staging") -> None:
        binding = connector_service.upsert_binding(
            self.db,
            self.scenario,
            environment=environment,
            binding_key_value=key,
            kind="data_source",
            connector_id=connector_id,
            created_by_user_id=self.user.id,
        )
        connector = self.db.get(DataSource, connector_id)
        assert connector is not None
        binding.health_status = "healthy"
        binding.health_message = ""
        binding.checked_at = datetime.now(timezone.utc)
        binding.connector_signature = connector_service.connector_signature("data_source", connector)
        self.db.commit()

    def _add_mapping(
        self,
        *,
        source: DataSource,
        key: str,
        reference: dict,
    ) -> DataMapping:
        mapping = DataMapping(
            id="mapping-release-binding",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            data_source_id=source.id,
            data_source_binding_key=key,
            data_source_binding_ref=reference,
            table_name="orders",
            column_map={},
        )
        self.db.add(mapping)
        self.db.commit()
        return mapping

    def test_non_dev_rejects_legacy_snapshot_omitting_live_mappings(self) -> None:
        snapshot = self._branch_snapshot()
        source = DataSource(
            id="source-added-after-snapshot",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="后加订单库",
            type="postgres",
            config={},
        )
        self.db.add(source)
        self.db.commit()
        self._add_mapping(source=source, key="orders-binding", reference={"adapter": "postgres"})

        legacy_content = copy.deepcopy(snapshot.content)
        legacy_content.pop("mappings", None)
        with self.assertRaises(release_service.ReleaseConflictError) as error:
            release_service._require_snapshot_connectors(
                self.db,
                self.scenario,
                legacy_content,
                environment="staging",
            )
        self.assertIn("未声明数据映射", str(error.exception))

    def test_publish_derives_missing_mapping_requirement_and_audit_from_snapshot(self) -> None:
        source = DataSource(
            id="source-audited-orders",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="已审计订单库",
            type="postgres",
            config={},
        )
        self.db.add(source)
        self.db.commit()
        self._add_mapping(
            source=source,
            key="audited-orders-binding",
            reference={"adapter": "postgres"},
        )
        self._healthy_binding(key="audited-orders-binding", connector_id=source.id)
        snapshot = self._branch_snapshot()
        content = copy.deepcopy(snapshot.content)
        content.pop("connector_bindings", None)

        audit = release_service._require_snapshot_connectors(
            self.db,
            self.scenario,
            content,
            environment="staging",
        )
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["binding_key"], "audited-orders-binding")
        self.assertEqual(audit[0]["connector_id"], source.id)

    def test_mapping_publish_rejects_healthy_non_sql_connector_target(self) -> None:
        sql_source = DataSource(
            id="source-logical-orders",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="逻辑订单库",
            type="postgres",
            config={},
        )
        bucket = DataSource(
            id="source-non-sql-target",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="错误文件桶",
            type="file_bucket",
            config={},
        )
        self.db.add_all([sql_source, bucket])
        self.db.commit()
        self._add_mapping(source=sql_source, key="orders-must-be-sql", reference={})
        self._healthy_binding(key="orders-must-be-sql", connector_id=bucket.id)
        snapshot = self._branch_snapshot()

        with self.assertRaises(release_service.ReleaseConflictError) as error:
            release_service._require_snapshot_connectors(
                self.db,
                self.scenario,
                snapshot.content,
                environment="staging",
            )
        self.assertIn("能力不满足", str(error.exception))


if __name__ == "__main__":
    unittest.main()
