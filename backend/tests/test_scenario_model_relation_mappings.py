from __future__ import annotations

import copy
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
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    RelationDataMapping,
    Tenant,
    User,
)
from app.services import permission_service, scenario_model_compiler
from app.services.policies import PolicyViolation


class ScenarioModelRelationMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, expire_on_commit=False)
        tenant = Tenant(id="tenant-relation-compiler", name="关系映射编译租户")
        user = User(
            id="user-relation-compiler",
            tenant_id=tenant.id,
            email="relation-compiler@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-relation-compiler",
            tenant_id=tenant.id,
            name="项目参与方",
            namespace="construction",
            status="draft",
        )
        self.source = DataSource(
            id="source-relation-compiler",
            tenant_id=tenant.id,
            scenario_id=self.scenario.id,
            name="项目业务库",
            type="postgres",
            config={},
        )
        self.db.add_all([tenant, user, self.scenario, self.source])
        self.db.commit()
        permission_service.ensure_organization(
            self.db, tenant.id, owner_user_id=user.id
        )
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    @property
    def source_ref(self) -> str:
        return "relation-brief:p0001"

    def _bundle(self, *, two_paragraphs: bool = False) -> dict:
        text = "项目通过企业编号关联施工企业。"
        if two_paragraphs:
            text += "\n\n关系映射使用已检查的数据表字段。"
        return scenario_model_compiler.build_source_bundle(
            "编译关系映射",
            [{
                "id": "relation-brief",
                "filename": "关系映射说明.md",
                "text": text,
            }],
        )

    def _tables(self) -> list[dict]:
        return [
            {
                "name": "projects",
                "columns": [
                    {"name": "project_id", "type": "TEXT", "pk": True},
                    {"name": "company_id", "type": "TEXT", "pk": False},
                ],
            },
            {
                "name": "companies",
                "columns": [
                    {"name": "company_id", "type": "TEXT", "pk": True},
                    {"name": "project_id", "type": "TEXT", "pk": False},
                ],
            },
            {
                "name": "project_companies",
                "columns": [
                    {"name": "project_id", "type": "TEXT", "pk": False},
                    {"name": "company_id", "type": "TEXT", "pk": False},
                ],
            },
        ]

    def _catalog(self) -> tuple[list[dict], dict[tuple[str, str], set[str]]]:
        tables = self._tables()
        return (
            [{
                "data_source_id": self.source.id,
                "data_source_name": self.source.name,
                "type": self.source.type,
                "tables": tables,
            }],
            {
                (self.source.id, table["name"]): {
                    column["name"] for column in table["columns"]
                }
                for table in tables
            },
        )

    def _raw(self, mode: str) -> dict:
        relation_mapping = {
            "key": "relation_mapping.project_company",
            "relation_ref": "relation.project_company",
            "source_mapping_ref": "mapping.project",
            "target_mapping_ref": "mapping.company",
            "mode": mode,
            "evidence_refs": [self.source_ref],
            "confidence": 0.96,
        }
        if mode == "source_fk":
            relation_mapping["foreign_key_column"] = "company_id"
        elif mode == "target_fk":
            relation_mapping["foreign_key_column"] = "project_id"
        else:
            relation_mapping.update({
                "join_data_source_ref": self.source.id,
                "join_table_name": "project_companies",
                "source_key_column": "project_id",
                "target_key_column": "company_id",
            })
        return {
            "schema_version": scenario_model_compiler.SCHEMA_VERSION,
            "entities": [
                {
                    "key": "entity.project",
                    "name": "项目",
                    "properties": [{
                        "name": "项目编号",
                        "data_type": "string",
                        "is_key": True,
                        "is_title": True,
                        "is_required": True,
                    }],
                    "evidence_refs": [self.source_ref],
                    "confidence": 0.98,
                },
                {
                    "key": "entity.company",
                    "name": "施工企业",
                    "properties": [{
                        "name": "企业编号",
                        "data_type": "string",
                        "is_key": True,
                        "is_title": True,
                        "is_required": True,
                    }],
                    "evidence_refs": [self.source_ref],
                    "confidence": 0.98,
                },
            ],
            "relations": [{
                "key": "relation.project_company",
                "name": "项目施工企业",
                "source_ref": "entity.project",
                "target_ref": "entity.company",
                "relation_type": "N:1",
                "evidence_refs": [self.source_ref],
                "confidence": 0.97,
            }],
            "functions": [],
            "actions": [],
            "rules": [],
            "events": [],
            "workflows": [],
            "mappings": [
                {
                    "key": "mapping.project",
                    "entity_ref": "entity.project",
                    "data_source_ref": self.source.id,
                    "table_name": "projects",
                    "column_map": {"项目编号": "project_id"},
                    "evidence_refs": [self.source_ref],
                    "confidence": 0.97,
                },
                {
                    "key": "mapping.company",
                    "entity_ref": "entity.company",
                    "data_source_ref": self.source.id,
                    "table_name": "companies",
                    "column_map": {"企业编号": "company_id"},
                    "evidence_refs": [self.source_ref],
                    "confidence": 0.97,
                },
            ],
            "relation_mappings": [relation_mapping],
            "unresolved": [],
            "coverage": [{
                "source_ref": self.source_ref,
                "status": "modeled",
                "reason": "对象、关系及其数据映射均已建模",
                "change_keys": ["relation_mapping.project_company"],
            }],
        }

    def _normalize(self, raw: dict, *, bundle: dict | None = None) -> dict:
        catalog, columns = self._catalog()
        return scenario_model_compiler.normalize_scenario_model(
            self.db,
            self.scenario,
            raw,
            source_bundle=bundle or self._bundle(),
            mapping_catalog=catalog,
            columns_by_table=columns,
        )

    def _assert_mode_applies(self, mode: str) -> None:
        payload = self._normalize(self._raw(mode))
        self.assertFalse([
            issue for issue in payload["unresolved"] if issue["blocking"]
        ])
        self.assertEqual(
            next(
                change for change in payload["changes"]
                if change["resource"] == "relation_mapping"
            )["operation"],
            "add",
        )
        with patch.object(
            scenario_model_compiler.datasource_service,
            "list_tables",
            return_value=self._tables(),
        ):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, payload
            )
        relation_mapping = self.db.scalars(select(RelationDataMapping)).one()
        self.assertEqual(relation_mapping.mode, mode)
        self.assertEqual(relation_mapping.status, "ready")
        self.assertTrue(relation_mapping.data_source_binding_key)
        object_mappings = self.db.scalars(select(DataMapping)).all()
        self.assertEqual(len(object_mappings), 2)
        self.assertTrue(all(item.data_source_binding_key for item in object_mappings))
        self.assertTrue(all(
            "sql_read" in (item.data_source_binding_ref or {}).get(
                "required_capabilities", []
            )
            for item in object_mappings
        ))

    def test_source_fk_relation_mapping_uses_formal_preflight_and_applies(self) -> None:
        self._assert_mode_applies("source_fk")

    def test_target_fk_relation_mapping_uses_formal_preflight_and_applies(self) -> None:
        self._assert_mode_applies("target_fk")

    def test_join_table_relation_mapping_uses_formal_preflight_and_applies(self) -> None:
        self._assert_mode_applies("join_table")

    def test_existing_relation_mapping_diff_updates_in_place(self) -> None:
        first = self._normalize(self._raw("source_fk"))
        with patch.object(
            scenario_model_compiler.datasource_service,
            "list_tables",
            return_value=self._tables(),
        ):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, first
            )
        original = self.db.scalars(select(RelationDataMapping)).one()
        original_id = original.id
        self.db.commit()
        self.db.expire_all()
        self.scenario = self.db.get(BusinessScenario, self.scenario.id)
        assert self.scenario is not None

        changed = self._normalize(self._raw("join_table"))
        change = next(
            item for item in changed["changes"]
            if item["resource"] == "relation_mapping"
        )
        self.assertEqual(change["operation"], "update")
        with patch.object(
            scenario_model_compiler.datasource_service,
            "list_tables",
            return_value=self._tables(),
        ):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, changed
            )
        updated = self.db.scalars(select(RelationDataMapping)).one()
        self.assertEqual(updated.id, original_id)
        self.assertEqual(updated.mode, "join_table")
        self.assertEqual(updated.table_name, "project_companies")

    def test_missing_data_source_or_column_is_blocking_before_any_write(self) -> None:
        bad_column = self._raw("source_fk")
        bad_column["relation_mappings"][0]["foreign_key_column"] = "missing_fk"
        column_payload = self._normalize(bad_column)
        self.assertIn(
            "missing_relation_mapping_column",
            {issue["code"] for issue in column_payload["unresolved"]},
        )
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, column_payload
            )
        self.assertEqual(
            self.db.scalar(select(func.count()).select_from(OntologyEntity)), 0
        )

        bad_source = self._raw("join_table")
        bad_source["relation_mappings"][0]["join_data_source_ref"] = "missing"
        source_payload = self._normalize(bad_source)
        self.assertIn(
            "missing_reference",
            {issue["code"] for issue in source_payload["unresolved"]},
        )

    def test_cross_chunk_relation_and_mapping_refs_resolve_by_typed_alias(self) -> None:
        bundle = self._bundle(two_paragraphs=True)
        first_ref, second_ref = [
            paragraph["ref"] for paragraph in bundle["paragraphs"]
        ]
        first = self._raw("source_fk")
        first["relation_mappings"] = []
        first["coverage"] = [{
            "source_ref": first_ref,
            "status": "modeled",
            "reason": "对象关系及对象映射",
            "change_keys": ["entity.project"],
        }]
        for section in (
            "entities", "relations", "mappings"
        ):
            for item in first[section]:
                item["evidence_refs"] = [first_ref]
        second = {
            "schema_version": scenario_model_compiler.SCHEMA_VERSION,
            **{
                section: []
                for section in scenario_model_compiler._RESOURCE_SECTIONS
            },
            "unresolved": [],
            "coverage": [{
                "source_ref": second_ref,
                "status": "modeled",
                "reason": "关系映射",
                "change_keys": ["relation_mapping:project_company"],
            }],
        }
        second["relation_mappings"] = [{
            "key": "project_company",
            "relation_ref": "relations:project_company",
            "source_mapping_ref": "mappings:project",
            "target_mapping_ref": "mapping_company",
            "mode": "source_fk",
            "foreign_key_column": "company_id",
            "evidence_refs": [second_ref],
            "confidence": 0.96,
        }]
        merged = scenario_model_compiler._merge_chunk_models([first, second])
        payload = self._normalize(merged, bundle=bundle)
        self.assertNotIn(
            "missing_reference",
            {issue["code"] for issue in payload["unresolved"]},
        )
        self.assertEqual(
            payload["relation_mappings"][0]["relation"],
            {"kind": "generated", "key": "relation.project_company"},
        )

    def test_late_formal_preflight_failure_rolls_back_every_resource(self) -> None:
        payload = self._normalize(self._raw("source_fk"))
        with (
            patch.object(
                scenario_model_compiler.datasource_service,
                "list_tables",
                return_value=self._tables(),
            ),
            patch.object(
                scenario_model_compiler.ontology_service,
                "validate_relation_data_mapping",
                side_effect=ValueError("simulated late relation preflight failure"),
            ),
        ):
            with self.assertRaises(PolicyViolation):
                scenario_model_compiler.apply_scenario_model(
                    self.db, self.scenario, payload
                )
        for model in (
            OntologyEntity, OntologyProperty, OntologyRelation,
            DataMapping, RelationDataMapping,
        ):
            self.assertEqual(
                self.db.scalar(select(func.count()).select_from(model)), 0
            )

    def test_title_fallback_is_visible_and_conflicting_titles_block(self) -> None:
        raw = self._raw("source_fk")
        del raw["entities"][0]["properties"][0]["is_title"]
        payload = self._normalize(raw)
        notices = [
            issue for issue in payload["unresolved"]
            if issue["code"] == "title_fallback_to_primary_key"
        ]
        self.assertEqual(len(notices), 1)
        self.assertFalse(notices[0]["blocking"])
        project_id = payload["entities"][0]["properties"][0]
        self.assertTrue(project_id["is_title"])

        conflicting = copy.deepcopy(raw)
        conflicting["entities"][0]["properties"].append({
            "name": "项目名称",
            "data_type": "string",
            "is_title": True,
        })
        conflicting["entities"][0]["properties"][0]["is_title"] = True
        blocked = self._normalize(conflicting)
        self.assertIn(
            "multiple_title_properties",
            {issue["code"] for issue in blocked["unresolved"]},
        )
        with self.assertRaises(PolicyViolation):
            scenario_model_compiler.apply_scenario_model(
                self.db, self.scenario, blocked
            )


if __name__ == "__main__":
    unittest.main()
