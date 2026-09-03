from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BucketFile,
    BusinessScenario,
    DataSource,
    ScenarioModelDraftResource,
    Tenant,
    User,
)
from app.routers import data_sources
from app.schemas import DataSourceIn
from app.services import permission_service, scenario_model_draft_service


class DataSourceDraftAutoRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        self.tenant = Tenant(id="tenant-auto-repair", name="自动修复租户")
        self.user = User(
            id="owner-auto-repair",
            tenant_id=self.tenant.id,
            email="auto-repair@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-auto-repair",
            tenant_id=self.tenant.id,
            name="农名工欠薪预警",
            namespace="farmer-wage-warning",
            status="draft",
        )
        self.db.add_all([self.tenant, self.user, self.scenario])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            self.tenant.id,
            owner_user_id=self.user.id,
        )
        self.db.commit()
        self.db.info.update({"tenant_id": self.tenant.id, "user_id": self.user.id})

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def _draft(
        self,
        suffix: str,
        *,
        payload: dict | None = None,
        scenario_id: str | None = None,
        tenant_id: str | None = None,
    ) -> ScenarioModelDraftResource:
        draft = ScenarioModelDraftResource(
            tenant_id=self.tenant.id,
            scenario_id=scenario_id or self.scenario.id,
            created_by_user_id=self.user.id,
            proposal_id=f"proposal-{suffix}",
            task_id="mapping",
            resource_kind="mapping",
            resource_key=f"mapping.{suffix}",
            resource_identity=f"mapping-{suffix}",
            title="工资台账映射",
            source_payload={"key": f"mapping.{suffix}"},
            payload=payload or {
                "key": f"mapping.{suffix}",
                "name": "工资台账",
                "source_name": "建筑领域工资台账",
                "table_name": "工资台账",
                "column_map": {
                    "姓名": "姓名",
                    "应发工资": "应发工资",
                },
            },
            validation_issues=[{
                "code": "MAPPING_MISSING_DATA_SOURCE",
                "message": "缺少数据源",
                "blocking": True,
            }, {
                "code": "document_reported_issue",
                "message": "[MAPPING_DEFERRED_NO_DATA_SOURCE] 工资台账尚未绑定数据源",
                "blocking": False,
            }],
            draft_status="needs_validation",
            enabled=False,
            publishable=False,
        )
        self.db.add(draft)
        self.db.flush()
        return draft

    def _source(
        self,
        source_id: str,
        *,
        name: str = "建筑领域工资台账",
        source_type: str = "postgres",
        status: str = "ok",
        tenant_id: str | None = None,
        scenario_id: str | None = None,
    ) -> DataSource:
        source = DataSource(
            id=source_id,
            tenant_id=tenant_id or self.tenant.id,
            scenario_id=scenario_id or self.scenario.id,
            name=name,
            type=source_type,
            status=status,
        )
        self.db.add(source)
        self.db.flush()
        return source

    def _assert_missing_dependency_preserved(
        self,
        draft: ScenarioModelDraftResource,
    ) -> None:
        self.db.refresh(draft)
        self.assertIn(
            "MAPPING_MISSING_DATA_SOURCE",
            {item["code"] for item in draft.validation_issues},
        )
        self.assertEqual(draft.draft_status, "needs_validation")

    def test_unverified_or_nonexistent_source_cannot_repair(self) -> None:
        draft = self._draft("unverified")
        source = self._source("source-unverified", status="unknown")

        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
            validated_source_id=source.id,
        )
        self.assertEqual(result["repaired_count"], 0)
        source.status = "ok"
        self.db.flush()
        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
        )
        self.assertEqual(result["repaired_count"], 0)

        nonexistent = DataSource(
            id="source-not-persisted",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="建筑领域工资台账",
            type="postgres",
            status="ok",
        )
        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            nonexistent,
            validated_source_id=nonexistent.id,
        )
        self.assertEqual(result["repaired_count"], 0)
        self._assert_missing_dependency_preserved(draft)

    def test_foreign_tenant_or_scenario_source_cannot_repair(self) -> None:
        draft = self._draft("foreign-scope")
        foreign_tenant = Tenant(id="tenant-auto-repair-foreign", name="其他租户")
        other_scenario = BusinessScenario(
            id="scenario-auto-repair-other",
            tenant_id=self.tenant.id,
            name="其他场景",
            namespace="other-auto-repair",
            status="draft",
        )
        self.db.add_all([foreign_tenant, other_scenario])
        self.db.flush()
        sources = [
            self._source(
                "source-foreign-tenant",
                tenant_id=foreign_tenant.id,
            ),
            self._source(
                "source-foreign-scenario",
                scenario_id=other_scenario.id,
            ),
        ]

        for source in sources:
            result = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                source,
                validated_source_id=source.id,
            )
            self.assertEqual(result["repaired_count"], 0)
        self._assert_missing_dependency_preserved(draft)

    def test_arbitrary_or_conflicting_reference_is_never_cleared(self) -> None:
        source = self._source("source-verified")
        other = self._source("source-other", name="其他数据源")
        drafts = [
            self._draft(
                "missing-reference",
                payload={
                    "source_name": source.name,
                    "data_source_id": "does-not-exist",
                },
            ),
            self._draft(
                "other-reference",
                payload={
                    "source_name": source.name,
                    "data_source_id": other.id,
                },
            ),
            self._draft(
                "conflicting-reference",
                payload={
                    "source_name": source.name,
                    "data_source_id": source.id,
                    "data_source_ref": other.id,
                },
            ),
        ]

        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
            validated_source_id=source.id,
        )

        self.assertEqual(result["repaired_count"], 0)
        for draft in drafts:
            self._assert_missing_dependency_preserved(draft)

    def test_only_the_specific_validated_source_can_be_selected(self) -> None:
        draft = self._draft(
            "specific-source",
            payload={"source_name": "另一个精确匹配的数据源"},
        )
        validated = self._source("source-specific", name="本次验证的数据源")
        self._source("source-exact-other", name="另一个精确匹配的数据源")

        with patch(
            "app.services.scenario_model_draft_service."
            "datasource_service.list_tables",
            return_value=[],
        ):
            result = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                validated,
                validated_source_id=validated.id,
            )

        self.assertEqual(result["repaired_count"], 0)
        self._assert_missing_dependency_preserved(draft)

    def test_file_bucket_requires_successfully_parsed_file_schema(self) -> None:
        draft = self._draft("file-schema")
        source = self._source(
            "source-file-schema",
            source_type="file_bucket",
        )

        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
            validated_source_id=source.id,
        )
        self.assertEqual(result["repaired_count"], 0)
        pending = BucketFile(
            id="file-pending-schema",
            data_source_id=source.id,
            filename="工资台账.xlsx",
            stored_path="test-only/pending.xlsx",
            status="pending",
            parsed_text="",
        )
        self.db.add(pending)
        self.db.flush()
        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
            validated_source_id=source.id,
        )
        self.assertEqual(result["repaired_count"], 0)

        pending.status = "parsed"
        pending.parsed_text = "工作表: 工资台账\n姓名 | 应发工资 | 实发工资"
        pending.error = ""
        self.db.flush()
        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
            validated_source_id=source.id,
        )
        self.db.commit()
        self.db.refresh(draft)

        self.assertEqual(result["repaired_count"], 1)
        self.assertEqual(draft.payload["data_source_id"], source.id)
        self.assertEqual(draft.payload["table_name"], "工资台账")

    def test_successful_connection_test_repairs_exact_source(self) -> None:
        source = self._source("source-connected", status="unknown")
        draft = self._draft(
            "connected",
            payload={
                "source_name": source.name,
                "data_source_id": source.id,
                "table_name": "工资台账",
                "column_map": {"姓名": "employee_name", "应发工资": "gross_pay"},
            },
        )

        with patch(
            "app.routers.data_sources.datasource_service.test_connection",
            return_value=(False, "driver leaked secret"),
        ):
            response = data_sources.test_data_source(source.id, self.db)
        self.assertFalse(response.ok)
        self._assert_missing_dependency_preserved(draft)

        with (
            patch(
                "app.routers.data_sources.datasource_service.test_connection",
                return_value=(True, "连接成功"),
            ),
            patch(
                "app.services.scenario_model_draft_service."
                "datasource_service.list_tables",
                return_value=[{
                    "name": "工资台账",
                    "columns": [
                        {"name": "employee_name", "type": "TEXT", "pk": True},
                        {"name": "gross_pay", "type": "NUMERIC", "pk": False},
                    ],
                }],
            ),
        ):
            response = data_sources.test_data_source(source.id, self.db)
        self.db.refresh(draft)

        self.assertTrue(response.ok)
        self.assertEqual(draft.draft_status, "accepted")
        self.assertNotIn(
            "MAPPING_MISSING_DATA_SOURCE",
            {item["code"] for item in draft.validation_issues},
        )

    def test_empty_database_binds_useful_source_but_keeps_schema_blocker(self) -> None:
        source = self._source("source-empty-sqlite")
        draft = self._draft("empty-sqlite")

        with patch(
            "app.services.scenario_model_draft_service."
            "datasource_service.list_tables",
            return_value=[],
        ):
            result = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                source,
                validated_source_id=source.id,
            )
        self.db.flush()
        self.db.refresh(draft)

        self.assertEqual(result["repaired_count"], 1)
        self.assertEqual(draft.payload["data_source_id"], source.id)
        self.assertEqual(draft.draft_status, "needs_attention")
        issue_codes = {item["code"] for item in draft.validation_issues}
        self.assertIn("MAPPING_MISSING_DATA_SOURCE", issue_codes)
        self.assertIn("AUTO_REPAIR_DATA_SOURCE_SCHEMA_UNVALIDATED", issue_codes)

        with patch(
            "app.services.scenario_model_draft_service."
            "datasource_service.list_tables",
            return_value=[{
                "name": "工资台账",
                "columns": [
                    {"name": "姓名", "type": "TEXT", "pk": True},
                    {"name": "应发工资", "type": "NUMERIC", "pk": False},
                ],
            }],
        ):
            retry = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                source,
                validated_source_id=source.id,
            )
        self.db.flush()
        self.db.refresh(draft)

        self.assertEqual(retry["repaired_count"], 1)
        self.assertEqual(draft.draft_status, "accepted")
        final_codes = [item["code"] for item in draft.validation_issues]
        self.assertNotIn("MAPPING_MISSING_DATA_SOURCE", final_codes)
        self.assertNotIn("AUTO_REPAIR_DATA_SOURCE_SCHEMA_UNVALIDATED", final_codes)
        self.assertEqual(final_codes.count("AUTO_REPAIRED_DATA_SOURCE_BINDING"), 1)

    def test_missing_physical_column_keeps_binding_blocked(self) -> None:
        source = self._source("source-missing-column")
        draft = self._draft("missing-column")

        with patch(
            "app.services.scenario_model_draft_service."
            "datasource_service.list_tables",
            return_value=[{
                "name": "工资台账",
                "columns": [{"name": "姓名", "type": "TEXT", "pk": True}],
            }],
        ):
            result = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                source,
                validated_source_id=source.id,
            )
        self.db.flush()
        self.db.refresh(draft)

        self.assertEqual(result["repaired_count"], 1)
        self.assertEqual(draft.payload["data_source_id"], source.id)
        self.assertEqual(draft.draft_status, "needs_attention")
        schema_issue = next(
            item
            for item in draft.validation_issues
            if item["code"] == "AUTO_REPAIR_DATA_SOURCE_SCHEMA_UNVALIDATED"
        )
        self.assertTrue(schema_issue["blocking"])
        self.assertIn("应发工资", schema_issue["message"])

    def test_equal_substring_matches_remain_ambiguous_in_any_test_order(self) -> None:
        draft = self._draft(
            "ambiguous-order",
            payload={
                "source_name": "工资台账",
                "table_name": "工资台账",
                "column_map": {"姓名": "姓名"},
            },
        )
        first = self._source("source-building-wage", name="建筑工资台账")
        second = self._source("source-labor-wage", name="劳务工资台账")
        schema = [{
            "name": "工资台账",
            "columns": [{"name": "姓名", "type": "TEXT", "pk": True}],
        }]

        with patch(
            "app.services.scenario_model_draft_service."
            "datasource_service.list_tables",
            return_value=schema,
        ):
            first_result = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                first,
                validated_source_id=first.id,
            )
            second_result = scenario_model_draft_service.auto_repair_data_source_drafts(
                self.db,
                second,
                validated_source_id=second.id,
            )
        self.db.flush()
        self.db.refresh(draft)

        self.assertEqual(first_result["repaired_count"], 0)
        self.assertEqual(second_result["repaired_count"], 0)
        self.assertNotIn("data_source_id", draft.payload)
        self.assertEqual(draft.draft_status, "needs_attention")
        issue_codes = {item["code"] for item in draft.validation_issues}
        self.assertIn("MAPPING_MISSING_DATA_SOURCE", issue_codes)
        self.assertIn("AUTO_REPAIR_DATA_SOURCE_AMBIGUOUS", issue_codes)

    def test_create_and_update_do_not_repair_before_validation(self) -> None:
        with patch(
            "app.routers.data_sources.scenario_model_draft_service."
            "auto_repair_data_source_drafts"
        ) as auto_repair:
            created = data_sources.create_data_source(
                DataSourceIn(
                    name="待验证工资库",
                    type="postgres",
                    scenario_id=self.scenario.id,
                    config={
                        "host": "postgres.example.test",
                        "port": 5432,
                        "database": "wages",
                        "user": "readonly",
                        "password": "test-only",
                    },
                ),
                self.db,
            )
            updated = data_sources.update_data_source(
                created.id,
                DataSourceIn(
                    name="待验证工资库（更新）",
                    type="postgres",
                    scenario_id=self.scenario.id,
                    config={
                        "host": "postgres.example.test",
                        "port": 5432,
                        "database": "wages_updated",
                        "user": "readonly",
                        "password": "test-only",
                    },
                ),
                self.db,
            )

        auto_repair.assert_not_called()
        self.assertEqual(created.status, "unknown")
        self.assertEqual(updated.status, "unknown")

    def test_new_scenario_source_repairs_missing_mapping_without_confirmation(self) -> None:
        draft = self._draft("success")

        source = DataSource(
            id="source-auto-repair",
            tenant_id=self.tenant.id,
            scenario_id=self.scenario.id,
            name="建筑领域工资台账",
            type="file_bucket",
            status="ok",
        )
        parsed_file = BucketFile(
            id="file-auto-repair",
            data_source_id=source.id,
            filename="建筑领域工资台账.xlsx",
            stored_path="test-only/wage-ledger.xlsx",
            status="parsed",
            parsed_text="工作表: 工资台账\n姓名 | 应发工资 | 实发工资",
        )
        self.db.add_all([source, parsed_file])
        self.db.flush()

        result = scenario_model_draft_service.auto_repair_data_source_drafts(
            self.db,
            source,
            validated_source_id=source.id,
        )
        self.db.commit()
        self.db.refresh(draft)

        self.assertEqual(result["repaired_count"], 1)
        self.assertEqual(result["draft_ids"], [draft.id])
        self.assertEqual(draft.payload["data_source_id"], source.id)
        self.assertEqual(draft.payload["data_source_name"], source.name)
        self.assertEqual(draft.draft_status, "accepted")
        self.assertFalse(draft.enabled)
        self.assertFalse(draft.publishable)
        issue_codes = {item["code"] for item in draft.validation_issues}
        self.assertNotIn("MAPPING_MISSING_DATA_SOURCE", issue_codes)
        self.assertFalse(
            any("MAPPING_DEFERRED_NO_DATA_SOURCE" in item.get("message", "") for item in draft.validation_issues)
        )
        self.assertIn("AUTO_REPAIRED_DATA_SOURCE_BINDING", issue_codes)


if __name__ == "__main__":
    unittest.main()
