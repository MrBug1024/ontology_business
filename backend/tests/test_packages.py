from __future__ import annotations

import copy
import json
import unittest

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyAction,
    OntologyEntity,
    OntologyEvent,
    OntologyProperty,
    OntologyRelation,
    OntologyRule,
    OntologyWorkflow,
    Tenant,
)
from app.services.package_service import (
    PACKAGE_FORMAT,
    PACKAGE_VERSION,
    canonical_package_json,
    export_scenario_package,
    package_fingerprint,
    plan_package_import,
    validate_package,
)


class PackageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.tenant = Tenant(id="tenant-package", name="资源包租户")
        self.source_scenario = BusinessScenario(
            id="scenario-source",
            tenant_id=self.tenant.id,
            name="采购本体",
            description="采购审批规则",
            industry="制造",
        )
        self.target_scenario = BusinessScenario(
            id="scenario-target",
            tenant_id=self.tenant.id,
            name="采购本体目标",
        )
        self.source = DataSource(
            id="source-db-id",
            tenant_id=self.tenant.id,
            scenario_id=self.source_scenario.id,
            name="采购 ERP",
            type="postgres",
            config={"password": "data-source-password-must-not-export"},
        )
        self.entity = OntologyEntity(
            id="entity-order",
            scenario_id=self.source_scenario.id,
            name="采购单",
            description="待审批采购订单",
            icon="receipt",
            color="#334155",
        )
        self.supplier = OntologyEntity(
            id="entity-supplier",
            scenario_id=self.source_scenario.id,
            name="供应商",
        )
        self.property = OntologyProperty(
            id="property-amount",
            entity_id=self.entity.id,
            name="金额",
            data_type="number",
            is_required=True,
            is_sensitive=True,
        )
        self.relation = OntologyRelation(
            id="relation-order-supplier",
            scenario_id=self.source_scenario.id,
            name="采购自",
            source_entity_id=self.entity.id,
            target_entity_id=self.supplier.id,
            relation_type="N:1",
        )
        self.mapping = DataMapping(
            id="mapping-order",
            scenario_id=self.source_scenario.id,
            entity_id=self.entity.id,
            data_source_id=self.source.id,
            table_name="purchase_orders",
            column_map={"金额": "amount", "供应商": "supplier_name"},
        )
        self.action = OntologyAction(
            id="action-approve",
            scenario_id=self.source_scenario.id,
            entity_id=self.entity.id,
            name="提交审批",
            executor_type="http",
            input_schema={"type": "object", "properties": {"action_id": {"type": "string"}}},
            executor_config={
                "url": "https://approval.example.test?token=url-token-must-not-export",
                "headers": {
                    "Authorization": "Bearer nested-token-must-not-export",
                    # This deliberately neutral field proves redact does not
                    # rely on the map key being called Authorization.
                    "header_value": "Basic Zm9vOmJhcg==",
                    # The scheme can also sit after a neutral text prefix.
                    "header_phrase": "Authorization: Basic dXNlcjpwYXNz",
                    "bearer_phrase": "Authorization: Bearer phrase-token-must-not-export",
                    "nested": {
                        "api_key": "nested-api-key-must-not-export",
                        "password": "nested-password-must-not-export",
                    },
                },
                "data_source_id": self.source.id,
            },
        )
        self.rule = OntologyRule(
            id="rule-limit",
            scenario_id=self.source_scenario.id,
            entity_id=self.entity.id,
            name="大额采购",
            condition={"field": "金额", "op": ">", "value": 10000},
            trigger_action_ids=[self.action.id],
            severity="warning",
        )
        self.event = OntologyEvent(
            id="event-requested",
            scenario_id=self.source_scenario.id,
            name="采购已提交",
            payload_schema={"type": "object", "properties": {"event_id": {"type": "string"}}},
        )
        self.workflow = OntologyWorkflow(
            id="workflow-approval",
            scenario_id=self.source_scenario.id,
            name="采购审批流",
            trigger_type="event",
            trigger_config={"event_id": self.event.id},
            steps=[
                {"step": 1, "type": "action", "action_id": self.action.id},
                {"step": 2, "type": "rule", "rule_id": self.rule.id},
            ],
            nodes=[
                {"id": "start", "type": "start", "data": {}},
                {"id": "approve", "type": "action", "data": {"action_id": self.action.id}},
                {"id": "publish", "type": "event", "data": {"event_id": self.event.id}},
            ],
            edges=[{"id": "edge-1", "source": "start", "target": "approve"}],
            status="draft",
        )
        self.db.add_all(
            [
                self.tenant,
                self.source_scenario,
                self.target_scenario,
                self.source,
                self.entity,
                self.supplier,
                self.property,
                self.relation,
                self.mapping,
                self.action,
                self.rule,
                self.event,
                self.workflow,
            ]
        )
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_export_is_stable_portable_and_recursively_redacted(self) -> None:
        first = export_scenario_package(self.db, self.source_scenario.id)
        second = export_scenario_package(self.db, self.source_scenario)

        self.assertEqual(first["format"], PACKAGE_FORMAT)
        self.assertEqual(first["version"], PACKAGE_VERSION)
        self.assertEqual(canonical_package_json(first), canonical_package_json(second))
        self.assertEqual(first["manifest"]["fingerprint"], package_fingerprint(first))
        self.assertEqual(first["manifest"]["resource_counts"], {
            "entities": 2,
            "properties": 1,
            "relations": 1,
            "mappings": 1,
            "functions": 0,
            "actions": 1,
            "rules": 1,
            "events": 1,
            "workflows": 1,
        })

        action = first["resources"]["actions"][0]
        workflow = first["resources"]["workflows"][0]
        rule = first["resources"]["rules"][0]
        self.assertEqual(action["executor_config"]["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(action["executor_config"]["headers"]["header_value"], "Basic [REDACTED]")
        self.assertEqual(action["executor_config"]["headers"]["nested"]["api_key"], "[REDACTED]")
        self.assertEqual(action["executor_config"]["headers"]["nested"]["password"], "[REDACTED]")
        self.assertEqual(action["executor_config"]["data_source_ref"]["name"], "采购 ERP")
        self.assertNotIn("data_source_id", action["executor_config"])
        self.assertIn("action_id", action["input_schema"]["properties"])
        self.assertIn("event_id", first["resources"]["events"][0]["payload_schema"]["properties"])
        self.assertEqual(rule["trigger_action_refs"], [action["key"]])
        self.assertEqual(workflow["trigger_config"]["event_ref"], first["resources"]["events"][0]["key"])
        self.assertEqual(workflow["steps"][0]["action_ref"], action["key"])
        self.assertEqual(workflow["nodes"][1]["data"]["action_ref"], action["key"])

        payload_text = json.dumps(first, ensure_ascii=False)
        for secret in (
            "data-source-password-must-not-export",
            "nested-token-must-not-export",
            "Zm9vOmJhcg==",
            "dXNlcjpwYXNz",
            "phrase-token-must-not-export",
            "nested-api-key-must-not-export",
            "nested-password-must-not-export",
            "url-token-must-not-export",
            self.source.id,
            self.action.id,
            self.rule.id,
            self.event.id,
            self.source_scenario.id,
        ):
            self.assertNotIn(secret, payload_text)

        validation = validate_package(first)
        self.assertTrue(validation["valid"], validation["errors"])

    def test_signed_legacy_package_without_functions_keeps_its_fingerprint(self) -> None:
        """A pre-function v1 package must remain importable without re-signing."""
        legacy = export_scenario_package(self.db, self.source_scenario.id)
        legacy["resources"].pop("functions")
        legacy["manifest"]["resource_counts"].pop("functions")
        legacy["manifest"]["fingerprint"] = package_fingerprint(legacy)

        explicit_empty = copy.deepcopy(legacy)
        explicit_empty["resources"]["functions"] = []
        self.assertEqual(
            package_fingerprint(legacy),
            package_fingerprint(explicit_empty),
        )

        validation = validate_package(legacy)
        self.assertTrue(validation["valid"], validation["errors"])
        self.assertEqual(validation["fingerprint"], legacy["manifest"]["fingerprint"])
        # The compiler still receives a complete collection map, and its empty
        # function list preserves the old package's no-function-change intent.
        self.assertEqual(validation["normalized"]["resources"]["functions"], [])

    def test_validation_rejects_bad_refs_but_returns_a_safe_preview(self) -> None:
        package = export_scenario_package(self.db, self.source_scenario.id)
        tampered = copy.deepcopy(package)
        tampered["resources"]["properties"][0]["entity_ref"] = "entity/missing"
        tampered["resources"]["actions"][0]["executor_config"]["deep"] = {
            "authorization": "Bearer untrusted-secret-value",
            "layer": {"access_token": "untrusted-access-token"},
        }
        tampered["resources"]["workflows"][0]["steps"][0]["action_id"] = "runtime-id-must-not-import"
        tampered["manifest"]["fingerprint"] = package_fingerprint(tampered)

        result = validate_package(tampered)
        self.assertFalse(result["valid"])
        error_codes = {error["code"] for error in result["errors"]}
        self.assertIn("unknown_reference", error_codes)
        self.assertIn("runtime_identifier_forbidden", error_codes)
        preview_text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("untrusted-secret-value", preview_text)
        self.assertNotIn("untrusted-access-token", preview_text)
        preview_config = result["normalized"]["resources"]["actions"][0]["executor_config"]
        self.assertEqual(preview_config["deep"]["authorization"], "[REDACTED]")
        self.assertEqual(preview_config["deep"]["layer"]["access_token"], "[REDACTED]")

    def test_validation_fails_closed_for_unknown_collections_and_resource_fields(self) -> None:
        package = export_scenario_package(self.db, self.source_scenario.id)
        package["resources"]["permissions"] = [{"role": "owner"}]
        package["resources"]["tests"] = []
        package["resources"]["entities"][0]["lifecycle_policy"] = "unrecognized"
        package["manifest"]["fingerprint"] = package_fingerprint(package)

        result = validate_package(package)

        self.assertFalse(result["valid"])
        issues = {(item["code"], item["path"]) for item in result["errors"]}
        self.assertIn(("unsupported_resource_collection", "resources.permissions"), issues)
        self.assertIn(("unsupported_resource_collection", "resources.tests"), issues)
        self.assertIn(
            ("unsupported_resource_field", "resources.entities[0].lifecycle_policy"),
            issues,
        )
        # The safe normalized preview still contains only compilable resources,
        # but callers can no longer mistake the dropped input for a valid package.
        self.assertNotIn("permissions", result["normalized"]["resources"])
        self.assertNotIn("tests", result["normalized"]["resources"])

    def test_export_does_not_disclose_cross_tenant_mapping_source_metadata(self) -> None:
        foreign_tenant = Tenant(id="tenant-foreign-export", name="外部租户")
        foreign_source = DataSource(
            id="foreign-source-export",
            tenant_id=foreign_tenant.id,
            scenario_id=None,
            name="外部保密数据源",
            type="mysql",
            config={"password": "foreign-data-source-secret"},
        )
        stale_mapping = DataMapping(
            id="mapping-foreign-source",
            scenario_id=self.source_scenario.id,
            entity_id=self.entity.id,
            data_source_id=foreign_source.id,
            table_name="foreign_orders",
            column_map={},
        )
        self.db.add_all([foreign_tenant, foreign_source, stale_mapping])
        self.db.commit()

        package = export_scenario_package(self.db, self.source_scenario)
        mapping = next(
            item for item in package["resources"]["mappings"]
            if item["table_name"] == "foreign_orders"
        )
        self.assertEqual(mapping["data_source_ref"], {"kind": "data_source", "binding_required": True})
        payload_text = json.dumps(package, ensure_ascii=False)
        for forbidden in (foreign_source.id, foreign_source.name, foreign_source.type, "foreign-data-source-secret"):
            self.assertNotIn(forbidden, payload_text)

    def test_mapping_custom_runtime_binding_round_trips_without_sensitive_reference_fields(self) -> None:
        self.mapping.data_source_binding_key = "procurement-orders-db"
        self.mapping.data_source_binding_ref = {
            "adapter": "postgres",
            "required_capabilities": ["sql_read"],
            "endpoint": "postgres://must-not-export.example.test/orders",
            "password": "must-not-export",
        }
        self.db.commit()

        package = export_scenario_package(self.db, self.source_scenario)
        mapping = package["resources"]["mappings"][0]
        self.assertEqual(mapping["data_source_binding_key"], "procurement-orders-db")
        self.assertEqual(
            mapping["data_source_binding_ref"],
            {"adapter": "postgres", "required_capabilities": ["sql_read"]},
        )
        self.assertNotIn("must-not-export", json.dumps(package, ensure_ascii=False))

        validation = validate_package(package)
        self.assertTrue(validation["valid"], validation["errors"])
        normalized = validation["normalized"]["resources"]["mappings"][0]
        self.assertEqual(normalized["data_source_binding_key"], "procurement-orders-db")
        self.assertEqual(
            normalized["data_source_binding_ref"],
            {"adapter": "postgres", "required_capabilities": ["sql_read"]},
        )

    def test_import_plan_is_read_only_and_reports_diff_and_binding_conflict(self) -> None:
        # A matching entity/action in the target creates deterministic update diffs.
        target_entity = OntologyEntity(
            id="target-entity-order",
            scenario_id=self.target_scenario.id,
            name="采购单",
            description="旧描述",
        )
        target_action = OntologyAction(
            id="target-action-approve",
            scenario_id=self.target_scenario.id,
            entity_id=target_entity.id,
            name="提交审批",
            executor_type="http",
            executor_config={"url": "https://old.example.test"},
        )
        # Same name/type in another tenant must not satisfy a package binding.
        foreign_tenant = Tenant(id="tenant-foreign", name="外部租户")
        foreign_source = DataSource(
            id="foreign-source",
            tenant_id=foreign_tenant.id,
            scenario_id=None,
            name="采购 ERP",
            type="postgres",
            config={"api_key": "foreign-secret"},
        )
        self.db.add_all([target_entity, target_action, foreign_tenant, foreign_source])
        self.db.commit()
        before_counts = {
            "entities": self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            "actions": self.db.scalar(select(func.count()).select_from(OntologyAction)),
            "mappings": self.db.scalar(select(func.count()).select_from(DataMapping)),
        }

        package = export_scenario_package(self.db, self.source_scenario.id)
        plan = plan_package_import(self.db, self.target_scenario.id, package)

        self.assertTrue(plan["valid"], plan["errors"])
        self.assertFalse(plan["applicable"])
        self.assertEqual(plan["proposal"]["mode"], "preview")
        self.assertFalse(plan["proposal"]["mutates_target"])
        self.assertFalse(plan["proposal"]["ready_to_apply"])
        changes = {(change["resource_type"], change["key"]): change for change in plan["proposal"]["changes"]}
        entity_change = changes[("entity", "entity/采购单")]
        self.assertEqual(entity_change["operation"], "update")
        self.assertIn("description", entity_change["changed_fields"])
        self.assertTrue(any(
            conflict["code"] == "missing_data_source"
            for conflict in plan["proposal"]["conflicts"]
        ))
        self.assertTrue(any(
            binding["kind"] == "data_source" for binding in plan["proposal"]["required_bindings"]
        ))
        self.assertEqual(before_counts, {
            "entities": self.db.scalar(select(func.count()).select_from(OntologyEntity)),
            "actions": self.db.scalar(select(func.count()).select_from(OntologyAction)),
            "mappings": self.db.scalar(select(func.count()).select_from(DataMapping)),
        })
        self.assertEqual(self.target_scenario.name, "采购本体目标")
        self.assertNotIn("nested-api-key-must-not-export", json.dumps(plan, ensure_ascii=False))

    def test_import_plan_is_applicable_after_safe_data_source_rebinding(self) -> None:
        # The package carries only this safe name/type reference; a target-local
        # source with matching metadata is enough for a fully previewable plan.
        target_source = DataSource(
            id="target-source-id",
            tenant_id=self.tenant.id,
            scenario_id=self.target_scenario.id,
            name="采购 ERP",
            type="postgres",
            config={"password": "target-only-secret"},
        )
        self.db.add(target_source)
        self.db.commit()

        package = export_scenario_package(self.db, self.source_scenario)
        # A portable package never carries credentials.  Make this fixture's
        # newly-created Action genuinely declarative so it is eligible for a
        # governed import after the data-source binding resolves.
        package["resources"]["actions"][0]["executor_config"] = {
            "url": "https://approval.example.test",
            "data_source_ref": package["resources"]["mappings"][0]["data_source_ref"],
        }
        package["manifest"]["fingerprint"] = package_fingerprint(package)
        plan = plan_package_import(self.db, self.target_scenario, package)

        self.assertTrue(plan["valid"], plan["errors"])
        self.assertTrue(plan["applicable"])
        self.assertTrue(plan["proposal"]["ready_to_apply"])
        self.assertEqual(plan["proposal"]["conflicts"], [])
        self.assertEqual(plan["proposal"]["summary"]["create"], 9)
        self.assertNotIn("target-only-secret", json.dumps(plan, ensure_ascii=False))

    def test_import_plan_blocks_new_runtime_resources_with_redacted_config(self) -> None:
        target_source = DataSource(
            id="target-source-redaction",
            tenant_id=self.tenant.id,
            scenario_id=self.target_scenario.id,
            name="采购 ERP",
            type="postgres",
            config={"password": "target-only-secret"},
        )
        self.db.add(target_source)
        self.db.commit()

        plan = plan_package_import(
            self.db,
            self.target_scenario,
            export_scenario_package(self.db, self.source_scenario),
        )

        self.assertFalse(plan["applicable"])
        action_conflicts = [
            conflict for conflict in plan["proposal"]["conflicts"]
            if conflict["resource_type"] == "action"
        ]
        self.assertTrue(any(conflict["code"] == "redacted_configuration" for conflict in action_conflicts))
        self.assertTrue(any(
            binding["kind"] == "secret" for binding in plan["proposal"]["required_bindings"]
        ))


if __name__ == "__main__":
    unittest.main()
