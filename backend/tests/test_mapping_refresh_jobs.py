from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, Response
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import database
from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataMappingRefreshJob,
    DataSource,
    OntologyEntity,
    OntologyInstance,
    OntologyProperty,
    Tenant,
    User,
)
from app.services import (
    connector_service,
    mapping_refresh_service,
    permission_service,
    release_service,
    runtime_connector_service,
)
from app.routers.scenarios import (
    _object_provenance,
    create_mapping,
    get_mapping_refresh_job,
    preview_mapping,
)
from app.schemas import DataMappingIn


class MappingRefreshJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-mapping", name="映射租户")
        self.user = User(
            id="user-mapping",
            tenant_id=self.tenant.id,
            email="mapping.owner@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-mapping", tenant_id=self.tenant.id, name="映射任务测试"
        )
        self.source = DataSource(
            id="source-mapping",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="订单库",
            type="sqlite",
            config={},
            status="ok",
        )
        self.entity = OntologyEntity(
            id="entity-mapping", scenario_id=self.scenario.id, name="订单"
        )
        self.key = OntologyProperty(
            id="property-mapping-id",
            entity_id=self.entity.id,
            name="id",
            is_key=True,
            is_required=True,
        )
        self.amount = OntologyProperty(
            id="property-mapping-amount",
            entity_id=self.entity.id,
            name="amount",
            data_type="number",
        )
        self.mapping = DataMapping(
            id="mapping-job-1",
            scenario_id=self.scenario.id,
            entity_id=self.entity.id,
            data_source_id=self.source.id,
            table_name="orders",
            column_map={"id": "id", "amount": "amount"},
        )
        self.db.add_all(
            [
                self.tenant,
                self.user,
                self.scenario,
                self.source,
                self.entity,
                self.key,
                self.amount,
                self.mapping,
            ]
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

    def _enqueue(self) -> DataMappingRefreshJob:
        job, created = mapping_refresh_service.enqueue_mapping_refresh(self.db, self.mapping)
        self.assertTrue(created)
        self.db.commit()
        return job

    def _publish_mapping_to_staging(self):
        """Create the exact release/binding evidence a non-dev job requires."""
        self.mapping.data_source_binding_key = "orders-refresh-binding"
        self.mapping.data_source_binding_ref = {"adapter": "sqlite"}
        binding = connector_service.upsert_binding(
            self.db,
            self.scenario,
            environment="staging",
            binding_key_value="orders-refresh-binding",
            kind="data_source",
            connector_id=self.source.id,
            created_by_user_id=self.user.id,
        )
        binding.health_status = "healthy"
        binding.health_message = ""
        binding.checked_at = datetime.now(timezone.utc)
        binding.connector_signature = connector_service.connector_signature("data_source", self.source)
        self.db.commit()
        branch = release_service.create_branch(
            self.db,
            self.scenario.id,
            name="mapping-refresh/staging",
        )
        return release_service.publish_snapshot(
            self.db,
            self.scenario.id,
            environment="staging",
            confirmed=True,
            branch_id=branch.id,
        )

    @staticmethod
    def _rows(rows: list[list[object]]) -> dict:
        return {"columns": ["id", "amount"], "rows": rows, "row_count": len(rows), "truncated": False}

    def test_queue_is_deduplicated_then_worker_persists_audited_result(self) -> None:
        first = self._enqueue()
        duplicate, created = mapping_refresh_service.enqueue_mapping_refresh(self.db, self.mapping)
        self.assertFalse(created)
        self.assertEqual(duplicate.id, first.id)
        self.db.commit()

        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-001", 99]]),
        ):
            processed = mapping_refresh_service.process_mapping_refresh_jobs(self.db)

        self.assertEqual([item.id for item in processed], [first.id])
        stored = self.db.get(DataMappingRefreshJob, first.id)
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.status, "succeeded")
        self.assertEqual(stored.rows_scanned, 1)
        self.assertEqual(stored.instances_created, 1)
        self.assertFalse(stored.active_key)
        self.assertEqual(stored.connector_audit[0]["environment"], "dev")
        self.assertFalse(stored.connector_audit[0]["managed"])
        self.assertEqual(self.mapping.status, "ok")
        instance = self.db.scalar(
            select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id)
        )
        self.assertIsNotNone(instance)
        assert instance is not None
        self.assertEqual(instance.attributes, {"id": "ORD-001", "amount": 99})

    def test_empty_source_completes_successfully_without_retry(self) -> None:
        job = self._enqueue()
        with patch("app.services.datasource_service.run_query", return_value=self._rows([])):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(stored.status, "succeeded")
        self.assertEqual(stored.rows_scanned, 0)
        self.assertEqual(stored.instances_created, 0)
        self.assertEqual(self.mapping.status, "ok")

    def test_worker_uses_frozen_job_mapping_when_live_definition_drifts(self) -> None:
        job = self._enqueue()
        self.mapping.column_map = {"id": "id"}
        self.db.commit()
        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-FROZEN", 42]]),
        ) as query:
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(stored.status, "succeeded")
        self.assertEqual(self.mapping.status, "unknown")
        self.assertIn('"orders"', query.call_args.args[1])
        imported = self.db.scalar(
            select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id)
        )
        assert imported is not None
        self.assertEqual(imported.attributes, {"id": "ORD-FROZEN", "amount": 42})

    def test_staging_job_and_preview_use_frozen_release_mapping_and_connector_pin(self) -> None:
        release = self._publish_mapping_to_staging()
        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        ):
            job, created = mapping_refresh_service.enqueue_mapping_refresh(self.db, self.mapping)
            self.assertTrue(created)
            self.db.commit()

        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(stored.definition_source, "release")
        self.assertEqual(stored.release_id, release.id)
        self.assertTrue(stored.definition_snapshot_id)
        self.assertTrue(stored.definition_hash)
        self.assertEqual(stored.mapping_snapshot["table_name"], "orders")
        self.assertEqual(stored.mapping_snapshot["column_map"], {"id": "id", "amount": "amount"})

        # Simulate authoring changing after deployment.  The queued operation
        # and non-dev preview must continue to use the release's table/map.
        self.mapping.table_name = "orders_live_drift"
        self.mapping.column_map = {"id": "id"}
        self.db.commit()
        real_resolver = runtime_connector_service.resolve_connector
        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        ), patch(
            "app.services.mapping_refresh_service.runtime_connector_service.resolve_connector",
            wraps=real_resolver,
        ) as resolve_connector, patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-STAGING", 77]]),
        ) as query:
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
            preview = preview_mapping(self.mapping.id, {"limit": 10}, self.db)

        refreshed = self.db.get(DataMappingRefreshJob, job.id)
        assert refreshed is not None
        self.assertEqual(refreshed.status, "succeeded")
        self.assertEqual(self.mapping.status, "unknown")
        self.assertEqual(resolve_connector.call_args_list[0].kwargs["release_id"], release.id)
        self.assertTrue(all('"orders"' in call.args[1] for call in query.call_args_list))
        self.assertEqual(preview["table_name"], "orders")
        imported = self.db.scalar(
            select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id)
        )
        assert imported is not None
        self.assertEqual(imported.attributes, {"id": "ORD-STAGING", "amount": 77})

    def test_mapping_job_api_hides_jobs_from_another_runtime_environment(self) -> None:
        job = self._enqueue()
        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        ), self.assertRaises(HTTPException) as error:
            get_mapping_refresh_job(job.id, Response(), self.db)
        self.assertEqual(error.exception.status_code, 404)

    def test_legacy_mapping_job_migration_cancels_active_rows_and_labels_history(self) -> None:
        legacy_engine = create_engine("sqlite:///:memory:")
        try:
            with legacy_engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    CREATE TABLE data_mapping_refresh_jobs (
                        id VARCHAR(32) PRIMARY KEY,
                        tenant_id VARCHAR(32),
                        scenario_id VARCHAR(32),
                        mapping_id VARCHAR(32),
                        active_key VARCHAR(260),
                        status VARCHAR(24),
                        error TEXT,
                        next_retry_at DATETIME,
                        completed_at DATETIME
                    )
                    """
                )
                connection.exec_driver_sql(
                    "INSERT INTO data_mapping_refresh_jobs "
                    "(id, tenant_id, scenario_id, mapping_id, active_key, status, error) "
                    "VALUES ('legacy-active', 'tenant', 'scenario', 'mapping', 'mapping:staging', 'queued', '')"
                )
                connection.exec_driver_sql(
                    "INSERT INTO data_mapping_refresh_jobs "
                    "(id, tenant_id, scenario_id, mapping_id, status, error) "
                    "VALUES ('legacy-terminal', 'tenant', 'scenario', 'mapping', 'succeeded', '')"
                )
            original_engine = database.engine
            database.engine = legacy_engine
            try:
                database._migrate_mapping_refresh_provenance()
            finally:
                database.engine = original_engine

            with legacy_engine.connect() as connection:
                columns = {
                    item["name"]
                    for item in database.inspect(connection).get_columns(
                        "data_mapping_refresh_jobs"
                    )
                }
                self.assertTrue(
                    {
                        "mapping_snapshot",
                        "definition_snapshot_id",
                        "release_id",
                        "definition_hash",
                        "definition_source",
                    }.issubset(columns)
                )
                active = connection.exec_driver_sql(
                    "SELECT status, active_key, mapping_snapshot, definition_source "
                    "FROM data_mapping_refresh_jobs WHERE id = 'legacy-active'"
                ).fetchone()
                terminal = connection.exec_driver_sql(
                    "SELECT status, definition_source FROM data_mapping_refresh_jobs "
                    "WHERE id = 'legacy-terminal'"
                ).fetchone()
            self.assertEqual(active[0], "cancelled")
            self.assertIsNone(active[1])
            self.assertEqual(active[2], "{}")
            self.assertEqual(active[3], "legacy")
            self.assertEqual(terminal[0], "succeeded")
            self.assertEqual(terminal[1], "legacy")
        finally:
            legacy_engine.dispose()

    def test_temporary_failure_is_redacted_and_retried(self) -> None:
        job = self._enqueue()
        with patch(
            "app.services.mapping_refresh_service.ontology_service.import_instances_from_mapping",
            side_effect=RuntimeError("postgres://worker:super-secret@db.example/orders unavailable"),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        retrying = self.db.get(DataMappingRefreshJob, job.id)
        assert retrying is not None
        self.assertEqual(retrying.status, "retry_waiting")
        self.assertNotIn("super-secret", retrying.error)
        self.assertIn("[REDACTED]", retrying.error)

        retrying.available_at = mapping_refresh_service.utc_now() - timedelta(seconds=1)
        self.db.commit()
        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-002", 120]]),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        completed = self.db.get(DataMappingRefreshJob, job.id)
        assert completed is not None
        self.assertEqual(completed.status, "succeeded")
        self.assertEqual(completed.attempt, 2)

    def test_staging_worker_does_not_claim_a_dev_refresh_job(self) -> None:
        job = self._enqueue()
        with patch(
            "app.services.runtime_connector_service.get_settings",
            return_value=SimpleNamespace(runtime_environment="staging"),
        ), patch("app.services.datasource_service.run_query") as query:
            processed = mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(processed, [])
        self.assertEqual(stored.status, "queued")
        self.assertEqual(stored.environment, "dev")
        query.assert_not_called()

    def test_timeout_rolls_back_flushed_import_before_scheduling_retry(self) -> None:
        job = self._enqueue()
        claim_started_at = mapping_refresh_service.utc_now() - timedelta(seconds=301)
        job.available_at = claim_started_at
        self.db.commit()

        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-TIMEOUT", 88]]),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db, now=claim_started_at)

        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(stored.status, "retry_waiting")
        self.assertEqual(
            self.db.scalar(
                select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id)
            ),
            None,
        )

    def test_cancelled_job_is_not_claimed_after_mapping_replacement_or_delete(self) -> None:
        job = self._enqueue()
        cancelled = mapping_refresh_service.cancel_active_mapping_refresh_jobs(
            self.db,
            self.mapping.id,
            reason="测试替换映射",
        )
        self.assertEqual(cancelled, 1)
        self.db.commit()

        with patch("app.services.datasource_service.run_query") as query:
            processed = mapping_refresh_service.process_mapping_refresh_jobs(self.db)

        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(processed, [])
        self.assertEqual(stored.status, "cancelled")
        query.assert_not_called()

    def test_mapping_save_keeps_identity_and_refresh_updates_existing_instance(self) -> None:
        first = self._enqueue()
        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-SAVE", 10]]),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        self.assertEqual(self.db.get(DataMappingRefreshJob, first.id).status, "succeeded")

        saved = create_mapping(
            self.scenario.id,
            DataMappingIn(
                entity_id=self.entity.id,
                data_source_id=self.source.id,
                table_name="orders",
                column_map={"id": "id", "amount": "amount"},
            ),
            self.db,
        )
        self.assertEqual(saved.id, self.mapping.id)
        self.assertEqual(
            len(
                self.db.scalars(
                    select(DataMapping).where(DataMapping.scenario_id == self.scenario.id)
                ).all()
            ),
            1,
        )
        refreshed, created = mapping_refresh_service.enqueue_mapping_refresh(self.db, self.mapping)
        self.assertTrue(created)
        self.db.commit()
        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-SAVE", 25]]),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)

        self.assertEqual(self.db.get(DataMappingRefreshJob, refreshed.id).status, "succeeded")
        instances = self.db.scalars(
            select(OntologyInstance).where(OntologyInstance.entity_id == self.entity.id)
        ).all()
        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].attributes, {"id": "ORD-SAVE", "amount": 25})

    def test_mapping_identity_change_creates_new_mapping_and_keeps_old_objects(self) -> None:
        first = self._enqueue()
        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-BOUNDARY", 10]]),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        self.assertEqual(self.db.get(DataMappingRefreshJob, first.id).status, "succeeded")

        original_mapping_id = self.mapping.id
        saved = create_mapping(
            self.scenario.id,
            DataMappingIn(
                entity_id=self.entity.id,
                data_source_id=self.source.id,
                table_name="orders_archive",
                column_map={"id": "id", "amount": "amount"},
            ),
            self.db,
        )
        self.assertNotEqual(saved.id, original_mapping_id)
        self.assertIsNone(self.db.get(DataMapping, original_mapping_id))

        new_mapping = self.db.get(DataMapping, saved.id)
        assert new_mapping is not None
        second, created = mapping_refresh_service.enqueue_mapping_refresh(self.db, new_mapping)
        self.assertTrue(created)
        self.db.commit()
        with patch(
            "app.services.datasource_service.run_query",
            return_value=self._rows([["ORD-BOUNDARY", 25]]),
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)
        self.assertEqual(self.db.get(DataMappingRefreshJob, second.id).status, "succeeded")

        instances = self.db.scalars(
            select(OntologyInstance)
            .where(OntologyInstance.entity_id == self.entity.id)
            .order_by(OntologyInstance.created_at.asc())
        ).all()
        self.assertEqual(len(instances), 2)
        self.assertEqual(instances[0].attributes, {"id": "ORD-BOUNDARY", "amount": 10})
        self.assertEqual(instances[0].source_metadata["mapping_id"], original_mapping_id)
        self.assertEqual(instances[1].attributes, {"id": "ORD-BOUNDARY", "amount": 25})
        self.assertEqual(instances[1].source_metadata["mapping_id"], saved.id)
        self.assertEqual(_object_provenance(self.db, instances[0]).table_name, "orders")
        self.assertEqual(_object_provenance(self.db, instances[1]).table_name, "orders_archive")

    def test_mapping_definition_save_cancels_active_job_and_invalidates_freshness(self) -> None:
        job = self._enqueue()
        self.mapping.status = "ok"
        self.mapping.environment_status = {"dev": {"status": "ok"}, "staging": {"status": "ok"}}
        self.db.commit()

        saved = create_mapping(
            self.scenario.id,
            DataMappingIn(
                entity_id=self.entity.id,
                data_source_id=self.source.id,
                table_name="orders_v2",
                column_map={"id": "id", "amount": "amount"},
            ),
            self.db,
        )
        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertNotEqual(saved.id, self.mapping.id)
        self.assertEqual(saved.table_name, "orders_v2")
        self.assertEqual(stored.status, "cancelled")
        self.assertEqual(saved.status, "unknown")

    def test_post_import_cancellation_rolls_back_tentative_instances(self) -> None:
        job = self._enqueue()

        def write_then_cancel(*_args, **_kwargs):
            self.db.add(
                OntologyInstance(
                    id="instance-cancelled-after-import",
                    scenario_id=self.scenario.id,
                    entity_id=self.entity.id,
                    name="不应提交",
                    attributes={"id": "ORD-CANCELLED"},
                    source="imported",
                )
            )
            self.db.flush()
            mapping_refresh_service.cancel_active_mapping_refresh_jobs(
                self.db,
                self.mapping.id,
                reason="测试中替换映射",
            )
            return {
                "instances_created": 1,
                "instances_updated": 0,
                "relations_created": 0,
                "rows_scanned": 1,
            }

        with patch(
            "app.services.mapping_refresh_service.ontology_service.import_instances_from_mapping",
            side_effect=write_then_cancel,
        ):
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)

        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(stored.status, "cancelled")
        self.assertIsNone(self.db.get(OntologyInstance, "instance-cancelled-after-import"))

    def test_disabled_requester_cancels_job_without_owner_fallback(self) -> None:
        job = self._enqueue()
        self.user.status = "disabled"
        self.db.commit()

        with patch("app.services.datasource_service.run_query") as query:
            mapping_refresh_service.process_mapping_refresh_jobs(self.db)

        stored = self.db.get(DataMappingRefreshJob, job.id)
        assert stored is not None
        self.assertEqual(stored.status, "cancelled")
        self.assertIn("任务发起人已失效", stored.error)
        query.assert_not_called()

    def test_scenario_deletion_cascades_mapping_refresh_history_in_orm(self) -> None:
        job = self._enqueue()
        self.db.delete(self.scenario)
        self.db.commit()
        self.assertIsNone(self.db.get(DataMappingRefreshJob, job.id))


if __name__ == "__main__":
    unittest.main()
