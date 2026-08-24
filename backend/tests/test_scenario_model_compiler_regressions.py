from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import (
    BusinessScenario,
    DataMapping,
    DataSource,
    FunctionDefinition,
    OntologyEntity,
    OntologyEvent,
    OntologyInstance,
    OntologyProperty,
    OntologyWorkflow,
    Tenant,
    User,
)
from app.services import permission_service, scenario_model_compiler
from app.services.policies import PolicyViolation


def _schema(properties: dict | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }


class ScenarioModelCompilerRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-compiler-regressions", name="编译器回归租户")
        user = User(
            id="user-compiler-regressions",
            tenant_id=tenant.id,
            email="compiler-regressions@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-compiler-regressions",
            tenant_id=tenant.id,
            name="建设项目履约",
            namespace="construction",
            status="draft",
        )
        self.db.add_all([tenant, user, self.scenario])
        self.db.commit()
        permission_service.ensure_organization(self.db, tenant.id, owner_user_id=user.id)
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @property
    def source_ref(self) -> str:
        return "construction-brief:p0001"

    def _bundle(self) -> dict:
        return scenario_model_compiler.build_source_bundle(
            "请编译附件",
            [{
                "id": "construction-brief",
                "filename": "建设项目规则.md",
                "status": "parsed",
                "text": "项目以项目编号唯一标识；只有草稿项目可以审批；审批流程需要定时检查。",
            }],
        )

    def _raw(self) -> dict:
        ref = self.source_ref
        return {
            "schema_version": "scenario_model.v1",
            "entities": [{
                "key": "entity.project",
                "name": "项目",
                "properties": [
                    {
                        "name": "项目编号",
                        "data_type": "string",
                        "is_key": True,
                        "is_title": True,
                        "is_required": True,
                    },
                    {
                        "name": "状态",
                        "data_type": "string",
                        "is_required": True,
                        "is_enum": True,
                        "enum_values": ["草稿", "已审批"],
                    },
                ],
                "state_property": "状态",
                "evidence_refs": [ref],
                "confidence": 0.98,
            }],
            "relations": [],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [],
            "relation_mappings": [],
            "unresolved": [],
            "coverage": [{
                "source_ref": ref,
                "status": "modeled",
                "reason": "项目对象已建模",
                "change_keys": ["entity.project"],
            }],
        }

    def _normalize(self, raw: dict, **kwargs) -> dict:
        return scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=self._bundle(),
            **kwargs,
        )

    def _seed_mapped_project(self) -> tuple[OntologyEntity, DataSource, DataMapping]:
        entity = OntologyEntity(
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
            state_property="状态",
        )
        self.db.add(entity)
        self.db.flush()
        self.db.add_all([
            OntologyProperty(
                entity_id=entity.id,
                name="项目编号",
                data_type="string",
                is_key=True,
                is_required=True,
            ),
            OntologyProperty(
                entity_id=entity.id,
                name="状态",
                data_type="string",
                is_required=True,
                is_enum=True,
                enum_values=["草稿", "已审批"],
            ),
        ])
        source = DataSource(
            tenant_id=self.scenario.tenant_id,
            scenario_id=self.scenario.id,
            name="项目业务库",
            type="sqlite",
            config={"path": "not-opened-in-test.db"},
        )
        self.db.add(source)
        self.db.flush()
        mapping = DataMapping(
            scenario_id=self.scenario.id,
            entity_id=entity.id,
            data_source_id=source.id,
            table_name="projects",
            column_map={"项目编号": "project_no", "状态": "legacy_status"},
            transform_rules={"状态": [{"type": "map", "mapping": {"D": "草稿"}}]},
            status="ok",
        )
        self.db.add(mapping)
        self.db.commit()
        return entity, source, mapping

    @staticmethod
    def _physical_tables(table_name: str = "projects") -> list[dict]:
        return [{
            "name": table_name,
            "columns": [
                {"name": "project_no", "type": "TEXT", "pk": True},
                {"name": "status", "type": "TEXT", "pk": False},
            ],
        }]

    def test_existing_primary_key_plus_new_primary_key_is_blocking(self) -> None:
        entity = OntologyEntity(
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
            state_property="状态",
        )
        self.db.add(entity)
        self.db.flush()
        self.db.add(OntologyProperty(
            entity_id=entity.id,
            name="项目编号",
            data_type="string",
            is_key=True,
            is_required=True,
        ))
        self.db.commit()

        raw = self._raw()
        raw["entities"][0]["properties"] = [
            {
                "name": "外部系统编号",
                "data_type": "string",
                "is_key": True,
                "is_required": True,
            },
            {
                "name": "状态",
                "data_type": "string",
                "is_required": True,
                "is_enum": True,
                "enum_values": ["草稿", "已审批"],
            },
        ]
        payload = self._normalize(raw)

        self.assertIn(
            "multiple_primary_keys",
            {item["code"] for item in payload["unresolved"]},
        )
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyProperty)),
            1,
        )

    def test_same_name_created_after_compile_is_not_silently_treated_as_update(self) -> None:
        payload = self._normalize(self._raw())
        raced = OntologyEntity(
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
        )
        self.db.add(raced)
        self.db.flush()
        self.db.add(OntologyProperty(
            entity_id=raced.id,
            name="竞态编号",
            data_type="string",
            is_key=True,
            is_title=True,
            is_required=True,
        ))
        self.db.commit()

        with self.assertRaisesRegex(PolicyViolation, "同名对象类型"):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, payload
            )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)), 1
        )

    def test_existing_title_is_preserved_and_competing_title_is_blocking(self) -> None:
        entity = OntologyEntity(
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
        )
        self.db.add(entity)
        self.db.flush()
        self.db.add_all([
            OntologyProperty(
                entity_id=entity.id,
                name="项目编号",
                data_type="string",
                is_key=True,
                is_title=False,
                is_required=True,
            ),
            OntologyProperty(
                entity_id=entity.id,
                name="项目名称",
                data_type="string",
                is_title=True,
                is_required=True,
            ),
        ])
        self.db.commit()

        preserving = self._raw()
        del preserving["entities"][0]["properties"][0]["is_title"]
        preserved = self._normalize(preserving)
        codes = {item["code"] for item in preserved["unresolved"]}
        self.assertNotIn("title_fallback_to_primary_key", codes)
        self.assertNotIn("existing_property_conflict", codes)
        self.assertNotIn("multiple_title_properties", codes)

        competing = self._normalize(self._raw())
        competing_codes = {
            item["code"] for item in competing["unresolved"]
        }
        self.assertIn("existing_property_conflict", competing_codes)
        self.assertIn("multiple_title_properties", competing_codes)

    def test_multiple_primary_keys_preserve_properties_without_missing_key_cascade(self) -> None:
        raw = self._raw()
        raw["entities"][0]["properties"].insert(1, {
            "name": "外部系统编号",
            "data_type": "string",
            "is_key": True,
            "is_required": True,
        })

        payload = self._normalize(raw)

        entity = payload["entities"][0]
        self.assertEqual(
            {prop["name"] for prop in entity["properties"]},
            {"项目编号", "外部系统编号", "状态"},
        )
        codes = {item["code"] for item in payload["unresolved"]}
        self.assertIn("invalid_entity", codes)
        self.assertIn("multiple_primary_keys", codes)
        self.assertNotIn("missing_primary_key", codes)
        self.assertNotIn("invalid_combined_entity", codes)
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, payload
            )

    def test_one_invalid_property_keeps_valid_siblings_without_secondary_blockers(self) -> None:
        raw = self._raw()
        raw["entities"][0]["properties"][0]["enum_values"] = {
            "not": "a list"
        }
        raw["rules"] = [{
            "key": "rule.project_id_present",
            "name": "项目编号存在",
            "entity_ref": "entity.project",
            "condition": {
                "field": "项目编号",
                "op": "is_not_null",
            },
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("rule.project_id_present")

        payload = self._normalize(raw)

        self.assertEqual(
            {prop["name"] for prop in payload["entities"][0]["properties"]},
            {"状态"},
        )
        codes = [item["code"] for item in payload["unresolved"]]
        self.assertEqual(codes.count("invalid_property"), 1)
        self.assertNotIn("missing_primary_key", codes)
        self.assertNotIn("missing_title_property", codes)
        self.assertNotIn("unknown_rule_field", codes)
        self.assertNotIn("invalid_entity", codes)

    def test_direct_duplicate_coverage_and_model_forged_code_stay_blocking(self) -> None:
        raw = self._raw()
        raw["coverage"].append(dict(raw["coverage"][0]))
        raw["unresolved"] = [{
            "code": "missing_primary_key",
            "message": "模型自报但不得伪装成平台校验器结果",
            "source_refs": [self.source_ref],
            "blocking": True,
        }]

        payload = self._normalize(raw)
        codes = [item["code"] for item in payload["unresolved"]]
        self.assertIn("duplicate_source_coverage", codes)
        self.assertIn("document_reported_issue", codes)
        self.assertNotIn("missing_primary_key", codes)

    def test_unbound_empty_entity_can_receive_enum_upgrade_atomically(self) -> None:
        entity = OntologyEntity(
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
        )
        self.db.add(entity)
        self.db.flush()
        self.db.add_all([
            OntologyProperty(
                entity_id=entity.id,
                name="项目编号",
                data_type="string",
                is_key=True,
                is_required=True,
            ),
            OntologyProperty(
                entity_id=entity.id,
                name="状态",
                data_type="string",
                is_required=True,
                is_enum=False,
                enum_values=[],
            ),
        ])
        self.db.commit()

        payload = self._normalize(self._raw())

        self.assertNotIn(
            "existing_property_conflict",
            {item["code"] for item in payload["unresolved"]},
        )
        proposed = next(
            prop for prop in payload["entities"][0]["properties"]
            if prop["name"] == "状态"
        )
        self.assertEqual(proposed["_operation"], "update")
        self.assertEqual(proposed["_structural_update"], "enum_upgrade")

        scenario_model_compiler.apply_scenario_model(
            self.db, self.scenario, payload
        )

        self.db.refresh(entity)
        status = next(prop for prop in entity.properties if prop.name == "状态")
        self.assertEqual(entity.state_property, "状态")
        self.assertTrue(status.is_enum)
        self.assertEqual(status.enum_values, ["草稿", "已审批"])

    def test_enum_upgrade_stays_blocking_after_entity_has_instances(self) -> None:
        entity = OntologyEntity(
            scenario_id=self.scenario.id,
            name="项目",
            namespace=self.scenario.namespace,
        )
        self.db.add(entity)
        self.db.flush()
        self.db.add_all([
            OntologyProperty(
                entity_id=entity.id,
                name="项目编号",
                data_type="string",
                is_key=True,
                is_required=True,
            ),
            OntologyProperty(
                entity_id=entity.id,
                name="状态",
                data_type="string",
                is_required=True,
                is_enum=False,
                enum_values=[],
            ),
            OntologyInstance(
                scenario_id=self.scenario.id,
                entity_id=entity.id,
                name="P-001",
                attributes={"项目编号": "P-001", "状态": "历史状态"},
            ),
        ])
        self.db.commit()

        payload = self._normalize(self._raw())

        self.assertIn(
            "existing_property_conflict",
            {item["code"] for item in payload["unresolved"]},
        )
        proposed = next(
            prop for prop in payload["entities"][0]["properties"]
            if prop["name"] == "状态"
        )
        self.assertNotIn("_structural_update", proposed)
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, payload
            )

    def test_cross_section_duplicate_raw_key_gets_a_typed_identity(self) -> None:
        raw = self._raw()
        raw["functions"] = [{
            "key": "entity.project",
            "name": "检查项目状态",
            "input_schema": _schema({"状态": {"type": "string"}}),
            "output_schema": _schema({"可审批": {"type": "boolean"}}),
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("entity.project")
        payload = self._normalize(raw)

        self.assertNotIn(
            "duplicate_change_key",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertEqual(payload["entities"][0]["key"], "entity.project")
        self.assertEqual(payload["functions"][0]["key"], "function.project")
        self.assertEqual(
            set(payload["coverage"][0]["change_keys"]),
            {"entity.project", "function.project"},
        )
        # Normalization remains proposal-only.
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(FunctionDefinition)), 0)

    def test_modeled_coverage_requires_a_real_change_or_evidence_link(self) -> None:
        raw = self._raw()
        for section in (
            "entities", "relations", "functions", "actions", "rules",
            "events", "workflows", "mappings",
        ):
            raw[section] = []
        raw["coverage"] = [{
            "source_ref": self.source_ref,
            "status": "modeled",
            "reason": "声称已经建模，但没有对应变更或证据",
            "change_keys": ["entity.does_not_exist"],
        }]

        payload = self._normalize(raw)

        self.assertIn(
            "invalid_modeled_coverage",
            {item["code"] for item in payload["unresolved"]},
        )
        self.assertEqual(payload["coverage"][0]["change_keys"], [])
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

    def test_multiple_mappings_for_one_entity_are_blocking(self) -> None:
        raw = self._raw()
        raw["mappings"] = [
            {
                "key": "mapping.project.primary",
                "entity_ref": "entity.project",
                "data_source_ref": "source-projects",
                "table_name": "projects",
                "column_map": {"项目编号": "project_no", "状态": "status"},
                "evidence_refs": [self.source_ref],
                "confidence": 0.9,
            },
            {
                "key": "mapping.project.duplicate",
                "entity_ref": "entity.project",
                "data_source_ref": "source-projects",
                "table_name": "projects",
                "column_map": {"项目编号": "project_no", "状态": "status"},
                "evidence_refs": [self.source_ref],
                "confidence": 0.9,
            },
        ]
        raw["coverage"][0]["change_keys"].extend(
            ["mapping.project.primary", "mapping.project.duplicate"]
        )
        payload = self._normalize(
            raw,
            mapping_catalog=[{
                "data_source_id": "source-projects",
                "data_source_name": "项目业务库",
            }],
            columns_by_table={
                ("source-projects", "projects"): {"project_no", "status"},
            },
        )

        self.assertIn(
            "duplicate_entity_mapping",
            {item["code"] for item in payload["unresolved"]},
        )

    def test_same_mapping_identity_updates_in_place_and_preserves_transforms(self) -> None:
        _entity, source, mapping = self._seed_mapped_project()
        raw = self._raw()
        raw["mappings"] = [{
            "key": "mapping.project",
            "entity_ref": "entity.project",
            "data_source_ref": source.id,
            "table_name": "projects",
            "column_map": {"项目编号": "project_no", "状态": "status"},
            "evidence_refs": [self.source_ref],
            "confidence": 0.95,
        }]
        raw["coverage"][0]["change_keys"].append("mapping.project")
        payload = self._normalize(
            raw,
            mapping_catalog=[{
                "data_source_id": source.id,
                "data_source_name": source.name,
            }],
            columns_by_table={(source.id, "projects"): {"project_no", "status"}},
        )
        mapping_change = next(
            item for item in payload["changes"] if item["change_id"] == "mapping.project"
        )
        self.assertEqual(mapping_change["operation"], "update")
        original_id = mapping.id
        original_transforms = mapping.transform_rules

        with (
            patch.object(
                scenario_model_compiler.datasource_service,
                "list_tables",
                return_value=self._physical_tables(),
            ),
            patch.object(
                scenario_model_compiler.mapping_refresh_service,
                "cancel_active_mapping_refresh_jobs",
                return_value=0,
            ) as cancel_jobs,
        ):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

        updated = self.db.get(DataMapping, original_id)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.column_map["状态"], "status")
        self.assertEqual(updated.transform_rules, original_transforms)
        cancel_jobs.assert_called_once()

    def test_changed_mapping_identity_is_explicit_replace_with_new_id(self) -> None:
        entity, _old_source, old_mapping = self._seed_mapped_project()
        new_source = DataSource(
            tenant_id=self.scenario.tenant_id,
            scenario_id=self.scenario.id,
            name="项目新库",
            type="sqlite",
            config={"path": "not-opened-new-test.db"},
        )
        self.db.add(new_source)
        self.db.commit()
        old_id = old_mapping.id
        raw = self._raw()
        raw["mappings"] = [{
            "key": "mapping.project.new_source",
            "entity_ref": "entity.project",
            "data_source_ref": new_source.id,
            "table_name": "projects",
            "column_map": {"项目编号": "project_no", "状态": "status"},
            "evidence_refs": [self.source_ref],
            "confidence": 0.95,
        }]
        raw["coverage"][0]["change_keys"].append("mapping.project.new_source")
        payload = self._normalize(
            raw,
            mapping_catalog=[{
                "data_source_id": new_source.id,
                "data_source_name": new_source.name,
            }],
            columns_by_table={(new_source.id, "projects"): {"project_no", "status"}},
        )
        operations = {
            (item["operation"], item["change_id"])
            for item in payload["changes"]
            if item["resource"] == "mapping"
        }
        self.assertIn(("add", "mapping.project.new_source"), operations)
        self.assertIn(("delete", f"mapping.project.new_source:delete:{old_id}"), operations)

        with (
            patch.object(
                scenario_model_compiler.datasource_service,
                "list_tables",
                return_value=self._physical_tables(),
            ),
            patch.object(
                scenario_model_compiler.mapping_refresh_service,
                "cancel_active_mapping_refresh_jobs",
                return_value=0,
            ) as cancel_jobs,
        ):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

        mappings = list(
            self.db.scalars(
                select(DataMapping).where(DataMapping.entity_id == entity.id)
            ).all()
        )
        self.assertEqual(len(mappings), 1)
        self.assertNotEqual(mappings[0].id, old_id)
        self.assertEqual(mappings[0].data_source_id, new_source.id)
        self.assertEqual(mappings[0].transform_rules, {})
        cancel_jobs.assert_called_once_with(
            self.db,
            old_id,
            reason="复合业务文档确认的新映射身份已替换旧定义",
        )

    def test_failed_or_empty_attachment_cannot_fall_back_to_instruction_text(self) -> None:
        cases = [
            {
                "id": "failed-document",
                "filename": "failed.pdf",
                "status": "error",
                "error": "PDF 解析失败",
                "text": "",
            },
            {
                "id": "empty-document",
                "filename": "empty.md",
                "status": "parsed",
                "text": "   ",
            },
        ]
        for document in cases:
            with self.subTest(status=document["status"]):
                with self.assertRaises(ValueError):
                    scenario_model_compiler.build_source_bundle(
                        "请严格按照这个附件建模",
                        [document],
                    )

    def test_rule_field_must_exist_on_referenced_entity(self) -> None:
        raw = self._raw()
        raw["rules"] = [{
            "key": "rule.nonexistent_field",
            "name": "不存在字段规则",
            "entity_ref": "entity.project",
            "condition": {"field": "不存在字段", "op": "==", "value": "任意值"},
            "trigger_action_refs": [],
            "severity": "warning",
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("rule.nonexistent_field")
        payload = self._normalize(raw)

        self.assertIn(
            "unknown_rule_field",
            {item["code"] for item in payload["unresolved"]},
        )
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

    def test_existing_property_metadata_delta_preview_matches_apply(self) -> None:
        entity, _source, _mapping = self._seed_mapped_project()
        raw = self._raw()
        raw["entities"][0]["properties"][1].update({
            "description": "项目当前审批状态",
            "default_value": "草稿",
        })
        payload = self._normalize(raw)
        property_change = next(
            item
            for item in payload["changes"]
            if item["change_id"] == "entity.project:property:状态"
        )
        self.assertEqual(property_change["operation"], "update")

        scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

        status_property = next(prop for prop in entity.properties if prop.name == "状态")
        self.assertEqual(status_property.description, "项目当前审批状态")
        self.assertEqual(status_property.default_value, "草稿")

    def test_event_trigger_is_resolved_to_id_and_feedback_loop_is_blocking(self) -> None:
        raw = self._raw()
        raw["events"] = [{
            "key": "event.approved",
            "name": "项目已审批",
            "payload_schema": _schema({"项目编号": {"type": "string"}}),
            "evidence_refs": [self.source_ref],
            "confidence": 0.95,
        }]
        raw["workflows"] = [{
            "key": "workflow.after_approved",
            "name": "审批后归档",
            "trigger_type": "event",
            "trigger_config": {"event_ref": "event.approved"},
            "nodes": [
                {"id": "start", "type": "start", "data": {"label": "开始"}},
                {"id": "end", "type": "end", "data": {"label": "结束"}},
            ],
            "edges": [{"id": "e1", "source": "start", "target": "end"}],
            "evidence_refs": [self.source_ref],
            "confidence": 0.95,
        }]
        raw["coverage"][0]["change_keys"].extend(
            ["event.approved", "workflow.after_approved"]
        )
        payload = self._normalize(raw)
        self.assertFalse(payload["unresolved"])
        scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)
        workflow = self.db.scalars(select(OntologyWorkflow)).one()
        event_id = self.db.scalars(select(OntologyEvent.id)).one()
        self.assertEqual(workflow.trigger_config["event_id"], event_id)

        self.db.rollback()
        loop_raw = self._raw()
        loop_raw["events"] = raw["events"]
        loop_raw["workflows"] = [{
            **raw["workflows"][0],
            "nodes": [
                {"id": "start", "type": "start", "data": {"label": "开始"}},
                {
                    "id": "emit",
                    "type": "event",
                    "data": {"label": "再次发布", "resource_ref": "event.approved"},
                },
                {"id": "end", "type": "end", "data": {"label": "结束"}},
            ],
            "edges": [
                {"id": "e1", "source": "start", "target": "emit"},
                {"id": "e2", "source": "emit", "target": "end"},
            ],
        }]
        loop_raw["coverage"] = raw["coverage"]
        loop_payload = self._normalize(loop_raw)
        self.assertIn(
            "event_feedback_loop",
            {item["code"] for item in loop_payload["unresolved"]},
        )
        loop_issue = next(
            item for item in loop_payload["unresolved"]
            if item["code"] == "event_feedback_loop"
        )
        self.assertIn("触发事件已由 trigger_config 表示", loop_issue["message"])
        self.assertIn("emit", loop_issue["message"])

    def test_zero_change_payload_is_blocked_without_mutation(self) -> None:
        raw = self._raw()
        for section in (
            "entities", "relations", "functions", "actions", "rules",
            "events", "workflows", "mappings",
        ):
            raw[section] = []
        raw["coverage"] = [{
            "source_ref": self.source_ref,
            "status": "context",
            "reason": "仅为上下文，不产生业务模型变更",
            "change_keys": [],
        }]
        payload = self._normalize(raw)

        self.assertEqual(payload["changes"], [])
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0)

    def test_unsupported_scheduled_cron_is_blocking(self) -> None:
        raw = self._raw()
        raw["workflows"] = [{
            "key": "workflow.cron_check",
            "name": "定时检查项目",
            "trigger_type": "scheduled",
            "trigger_config": {"cron": "0 8 * * *", "timezone": "Asia/Shanghai"},
            "nodes": [
                {"id": "start", "type": "start", "data": {"label": "开始"}},
                {"id": "end", "type": "end", "data": {"label": "结束"}},
            ],
            "edges": [{"id": "e1", "source": "start", "target": "end"}],
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.cron_check")
        payload = self._normalize(raw)

        self.assertIn(
            "invalid_workflow_trigger",
            {item["code"] for item in payload["unresolved"]},
        )
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)
        self.assertEqual(self.db.scalar(select(func.count()).select_from(OntologyWorkflow)), 0)

    def test_supported_interval_schedule_is_saved_as_runnable_config(self) -> None:
        raw = self._raw()
        raw["workflows"] = [{
            "key": "workflow.interval_check",
            "name": "周期检查项目",
            "trigger_type": "scheduled",
            "trigger_config": {"interval_seconds": "600"},
            "nodes": [
                {"id": "start", "type": "start", "data": {"label": "开始"}},
                {"id": "end", "type": "end", "data": {"label": "结束"}},
            ],
            "edges": [{"id": "e1", "source": "start", "target": "end"}],
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.interval_check")
        payload = self._normalize(raw)
        self.assertFalse(payload["unresolved"])

        scenario_model_compiler.apply_scenario_model(self.db, self.scenario, payload)

        workflow = self.db.scalars(select(OntologyWorkflow)).one()
        self.assertEqual(workflow.trigger_config["interval_seconds"], 600)

    def _workflow_resources(self, raw: dict) -> None:
        raw["actions"] = [{
            "key": "action.approve",
            "name": "审批项目",
            "entity_ref": "entity.project",
            "input_schema": _schema({"项目编号": {"type": "string"}}),
            "evidence_refs": [self.source_ref],
            "confidence": 0.95,
        }]
        raw["rules"] = [{
            "key": "rule.draft_only",
            "name": "仅草稿可审批",
            "entity_ref": "entity.project",
            "condition": {"field": "状态", "op": "==", "value": "草稿"},
            "trigger_action_refs": ["action.approve"],
            "severity": "warning",
            "evidence_refs": [self.source_ref],
            "confidence": 0.95,
        }]
        raw["coverage"][0]["change_keys"].extend(
            ["action.approve", "rule.draft_only"]
        )

    def test_workflow_safe_aliases_unique_refs_and_missing_start_are_normalized(self) -> None:
        raw = self._raw()
        self._workflow_resources(raw)
        raw["workflows"] = [{
            "key": "workflow.approve",
            "name": "项目审批",
            "trigger_type": "manual",
            "nodes": [
                {
                    "id": "approve",
                    "type": "operation_node",
                    "name": "审批项目",
                    "data": {"label": "审批项目"},
                },
                {
                    "id": "check",
                    "type": "decision",
                    "data": {
                        "label": "仅草稿可审批",
                        "ruleRef": "rule.draft_only",
                    },
                },
                {"id": "pass", "type": "human_approval", "name": "通过处理"},
                {"id": "reject", "type": "approval_node", "name": "拒绝处理"},
                {"id": "done", "type": "finish_node", "name": "完成"},
            ],
            "edges": [
                {"id": "e1", "fromNodeId": "审批项目", "targetId": "check"},
                {"id": "e2", "source": "check", "target": "pass", "branch": "通过"},
                {"id": "e3", "source": "check", "target": "reject"},
                {"id": "e4", "source": "pass", "target": "done"},
                {"id": "e5", "source": "reject", "target": "done"},
            ],
            "evidence_refs": [self.source_ref],
            "confidence": 0.94,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.approve")

        payload = self._normalize(raw)

        self.assertFalse(payload["unresolved"])
        workflow = payload["workflows"][0]
        nodes = {node["id"]: node for node in workflow["nodes"]}
        self.assertEqual(nodes["approve"]["type"], "action")
        self.assertEqual(nodes["approve"]["data"]["resource"]["key"], "action.approve")
        self.assertEqual(nodes["check"]["type"], "rule")
        self.assertEqual(nodes["pass"]["type"], "approval")
        self.assertEqual(nodes["done"]["type"], "end")
        self.assertEqual(sum(node["type"] == "start" for node in nodes.values()), 1)
        self.assertEqual(workflow["edges"][1]["source"], "approve")
        branch_labels = {
            edge["label"]
            for edge in workflow["edges"]
            if edge["source"] == "check"
        }
        self.assertEqual(branch_labels, {"true", "false"})

    def test_two_blank_rule_branches_remain_blocking(self) -> None:
        raw = self._raw()
        self._workflow_resources(raw)
        raw["workflows"] = [{
            "key": "workflow.ambiguous_branch",
            "name": "歧义分支",
            "trigger_type": "manual",
            "nodes": [
                {"id": "start", "type": "start"},
                {
                    "id": "check",
                    "type": "rule",
                    "data": {"resource_ref": "rule.draft_only"},
                },
                {
                    "id": "check_again",
                    "type": "rule_node",
                    "data": {"resource_ref": "rule.draft_only"},
                },
                {"id": "pass", "type": "approval"},
                {"id": "reject", "type": "approval"},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"source": "start", "target": "check"},
                {"source": "check", "target": "pass"},
                {"source": "check", "target": "reject"},
                {"source": "pass", "target": "check_again"},
                {"source": "check_again", "target": "end", "label": "true"},
                {"source": "reject", "target": "end"},
            ],
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.ambiguous_branch")

        payload = self._normalize(raw)

        issues = [item for item in payload["unresolved"] if item["code"] == "invalid_workflow"]
        self.assertEqual(len(issues), 1)
        self.assertIn("规则分支不完整", issues[0]["message"])
        self.assertIn("check（缺少 false/true）", issues[0]["message"])
        self.assertIn("check_again（缺少 false）", issues[0]["message"])
        labels = [
            edge["label"]
            for edge in payload["workflows"][0]["edges"]
            if edge["source"] == "check"
        ]
        self.assertEqual(labels, ["", ""])

    def test_dangling_edges_and_missing_resource_refs_are_consolidated(self) -> None:
        raw = self._raw()
        self._workflow_resources(raw)
        raw["workflows"] = [{
            "key": "workflow.broken_graph",
            "name": "结构缺失流程",
            "trigger_type": "manual",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "run", "type": "action", "data": {"resource": None}},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"id": "e1", "source": "", "target": ""},
                {"id": "e2", "source": "", "target": ""},
            ],
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.broken_graph")

        payload = self._normalize(raw)

        codes = [item["code"] for item in payload["unresolved"]]
        self.assertEqual(codes.count("missing_workflow_resource_refs"), 1)
        self.assertEqual(codes.count("workflow_graph_reference_mismatch"), 1)
        self.assertNotIn("missing_reference", codes)
        self.assertNotIn("invalid_workflow", codes)

    def test_blank_node_types_are_one_blocker_and_semantic_names_are_not_guessed(self) -> None:
        raw = self._raw()
        raw["workflows"] = [{
            "key": "workflow.opaque",
            "name": "无法确定节点类型的流程",
            "trigger_type": "manual",
            "nodes": [
                {"id": "n1", "type": "", "name": "开始扫描"},
                {"id": "n2", "type": "", "name": "扫描完成"},
            ],
            "edges": [{"id": "e1", "source": "n1", "target": "n2"}],
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.opaque")

        payload = self._normalize(raw)

        issues = [
            item for item in payload["unresolved"]
            if item["code"] == "unsupported_workflow_node"
        ]
        self.assertEqual(len(issues), 1)
        self.assertIn("n1（空类型）", issues[0]["message"])
        self.assertIn("n2（空类型）", issues[0]["message"])
        original = {
            node["id"]: node["type"]
            for node in payload["workflows"][0]["nodes"]
            if node["id"] in {"n1", "n2"}
        }
        self.assertEqual(original, {"n1": "", "n2": ""})

    def test_missing_start_with_multiple_roots_remains_blocking(self) -> None:
        raw = self._raw()
        self._workflow_resources(raw)
        raw["workflows"] = [{
            "key": "workflow.multiple_roots",
            "name": "多入口流程",
            "trigger_type": "manual",
            "nodes": [
                {
                    "id": "first",
                    "type": "action",
                    "data": {"resource_ref": "action.approve"},
                },
                {
                    "id": "second",
                    "type": "action",
                    "data": {"resource_ref": "action.approve"},
                },
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"source": "first", "target": "end"},
                {"source": "second", "target": "end"},
            ],
            "evidence_refs": [self.source_ref],
            "confidence": 0.9,
        }]
        raw["coverage"][0]["change_keys"].append("workflow.multiple_roots")

        payload = self._normalize(raw)

        workflow = payload["workflows"][0]
        self.assertFalse(any(node["type"] == "start" for node in workflow["nodes"]))
        issues = [item for item in payload["unresolved"] if item["code"] == "invalid_workflow"]
        self.assertEqual(len(issues), 1)
        self.assertIn("一个开始节点", issues[0]["message"])


if __name__ == "__main__":
    unittest.main()
