from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import BusinessScenario, DataSource, Tenant, User
from app.models import DataMapping, OntologyEntity, OntologyRelation, RelationDataMapping
from app.services import (
    business_query_service,
    datasource_service,
    permission_service,
    runtime_definition_service,
    template_artifact_service,
)
from examples import upgrade_bookkeeping_audit, upgrade_medical_audit


class MedicalAuditBusinessTaskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.source_path = Path(self.temp.name) / "medical-audit.db"
        self._build_source(self.source_path)
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        tenant = Tenant(id="tenant-medical-task", name="医保审计测试租户")
        user = User(
            id="user-medical-task",
            tenant_id=tenant.id,
            email="medical-task@example.test",
            password_hash="test-only",
            status="active",
        )
        self.scenario = BusinessScenario(
            id="scenario-medical-task",
            tenant_id="tenant-medical-task",
            name="医保审计测试",
            namespace="medical_audit",
            status="active",
        )
        self.source = DataSource(
            id="source-medical-task",
            tenant_id="tenant-medical-task",
            scenario_id=self.scenario.id,
            name="医保审计业务库",
            type="sqlite",
            config={"path": str(self.source_path)},
            status="ok",
        )
        self.db.add_all([tenant, user, self.scenario, self.source])
        self.db.commit()
        permission_service.ensure_organization(
            self.db,
            tenant.id,
            owner_user_id=user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        datasource_service.invalidate_engine(self.source)
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _build_source(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE "就诊表" (
                    "就诊ID" TEXT,
                    "就诊凭证编号" TEXT,
                    "定点医药机构编号" TEXT,
                    "定点医药机构名称" TEXT,
                    "医院等级" TEXT,
                    "定点归属医保区划" TEXT,
                    "人员编号" TEXT,
                    "人员姓名" TEXT,
                    "开始时间" TEXT,
                    "结束时间" TEXT,
                    "医疗类别" TEXT,
                    "住院主诊断名称" TEXT
                );
                CREATE TABLE "项目明细表" (
                    "记账流水号" INTEGER,
                    "就诊ID" TEXT,
                    "定点医药机构编号" TEXT,
                    "定点医药机构名称" TEXT,
                    "人员编号" TEXT,
                    "医保目录编码" TEXT,
                    "医保目录名称" TEXT,
                    "数量" REAL,
                    "单价" REAL,
                    "明细项目费用总额" REAL,
                    "费用发生时间" TEXT,
                    "医疗收费项目类别" TEXT,
                    "目录类别" TEXT,
                    "规格" TEXT,
                    "开单科室名称" TEXT,
                    "开单医师姓名" TEXT
                );
                CREATE TABLE "规则表" (
                    "序号" INTEGER,
                    "国家问题清单" TEXT,
                    "所属领域" TEXT,
                    "有关依据" TEXT,
                    "违规类型" TEXT,
                    "国家违规参考示例" TEXT,
                    "首次进入问题清单年份" TEXT,
                    "用途" TEXT
                );
                INSERT INTO "就诊表" VALUES
                    ('E01','V01','F01','贵阳泰康乐综合医院','二级','520100','P01','患者一','2025-01-01','2025-01-01','门诊','');
                """
            )
            rows = []
            for index in range(10):
                quantity = 3 if index == 8 else 4
                amount = 29.16 if quantity == 3 else 38.88
                rows.append(
                    (
                        index + 1,
                        f"E{index + 1:02d}",
                        "F01",
                        "贵阳泰康乐综合医院",
                        f"P{index + 1:02d}",
                        "S-GUA-SHA",
                        "刮痧治疗",
                        quantity,
                        9.72,
                        amount,
                        f"2025-{index + 1:02d}-01 09:00:00",
                        "中医诊疗",
                        "诊疗项目",
                        "",
                        "中医科",
                        "医师",
                    )
                )
            rows.append(
                (
                    11,
                    "E11",
                    "F01",
                    "贵阳泰康乐综合医院",
                    "P11",
                    "S-CN-GUA-SHA",
                    "中医刮痧",
                    1,
                    36.0,
                    36.0,
                    "2026-03-30 09:03:06",
                    "中医诊疗",
                    "诊疗项目",
                    "",
                    "中医科",
                    "医师",
                )
            )
            connection.executemany(
                'INSERT INTO "项目明细表" VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                rows,
            )

    def test_named_audit_condition_returns_ten_charge_lines_and_correct_total(self) -> None:
        upgrade_medical_audit._ensure_domain_views(self.source)
        entities = upgrade_medical_audit._upsert_entities(self.db, self.scenario)
        mappings = upgrade_medical_audit._upsert_mappings(
            self.db,
            self.scenario,
            self.source,
            entities,
        )
        upgrade_medical_audit._upsert_relations(
            self.db,
            self.scenario,
            self.source,
            entities,
            mappings,
        )
        self.db.commit()
        definition = runtime_definition_service.resolve_active(
            self.db,
            self.scenario,
            environment="dev",
        )
        result = business_query_service.query_business_data(
            self.db,
            definition=definition,
            mappings=list(definition.mappings.values()),
            data_sources=[self.source],
            args={
                "base_entity": "收费明细",
                "base_properties": [
                    "收费明细ID",
                    "就诊ID",
                    "医疗机构名称",
                    "服务项目名称",
                    "收费数量",
                    "单价",
                    "收费金额",
                    "发生时间",
                ],
                "base_filters": [
                    {"property": "医疗机构名称", "op": "eq", "value": "贵阳泰康乐综合医院"},
                    {"property": "服务项目名称", "op": "eq", "value": "刮痧治疗"},
                    {"property": "收费数量", "op": "gt", "value": 2},
                ],
                "limit": 50,
            },
        )
        self.assertEqual(result["row_count"], 10)
        self.assertFalse(result["truncated"])
        self.assertTrue(all(item["服务项目名称"] == "刮痧治疗" for item in result["records"]))
        self.assertTrue(all(float(item["收费数量"]) > 2 for item in result["records"]))
        self.assertAlmostEqual(
            sum(float(item["收费金额"]) for item in result["records"]),
            379.08,
            places=2,
        )

        no_match = business_query_service.query_business_data(
            self.db,
            definition=definition,
            mappings=list(definition.mappings.values()),
            data_sources=[self.source],
            args={
                "base_entity": "收费明细",
                "base_properties": ["收费明细ID", "收费数量"],
                "base_filters": [
                    {"property": "医疗机构名称", "op": "eq", "value": "贵阳泰康乐综合医院"},
                    {"property": "服务项目名称", "op": "eq", "value": "刮痧治疗"},
                    {"property": "收费数量", "op": "gt", "value": 999},
                ],
                "limit": 50,
            },
        )
        self.assertEqual(no_match["row_count"], 0)
        self.assertEqual(no_match["records"], [])


class BookkeepingOntologyUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.scenario = BusinessScenario(
            id="scenario-bookkeeping-task",
            tenant_id="tenant-bookkeeping-task",
            name="代理记账测试",
            namespace="bookkeeping_audit",
            status="active",
        )
        self.source = DataSource(
            id="source-bookkeeping-task",
            tenant_id=self.scenario.tenant_id,
            scenario_id=self.scenario.id,
            name="代理记账业务库",
            type="sqlite",
            config={"path": "unused.db"},
            status="ok",
        )
        self.db.add_all([self.scenario, self.source])
        self.db.flush()
        self.entities: dict[str, OntologyEntity] = {}
        self.mappings: dict[str, DataMapping] = {}
        names = ["客户", *(spec["name"] for spec in upgrade_bookkeeping_audit.AUDIT_OBJECT_SPECS)]
        for index, name in enumerate(names):
            entity = OntologyEntity(
                scenario_id=self.scenario.id,
                name=name,
                api_name=f"bookkeeping_object_{index}",
                namespace="bookkeeping_audit",
            )
            self.db.add(entity)
            self.db.flush()
            mapping = DataMapping(
                scenario_id=self.scenario.id,
                entity_id=entity.id,
                data_source_id=self.source.id,
                table_name=f"table_{index}",
                column_map={"项目ID": "project_id"},
                transform_rules={},
                status="ready",
            )
            self.db.add(mapping)
            self.entities[name] = entity
            self.mappings[name] = mapping
        self.db.commit()

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_relation_upgrade_is_complete_and_idempotent(self) -> None:
        for _ in range(2):
            upgrade_bookkeeping_audit._upsert_audit_relations(
                self.db,
                self.scenario,
                self.source,
                self.entities,
                self.mappings,
            )
            self.db.commit()

        relations = self.db.query(OntologyRelation).filter_by(
            scenario_id=self.scenario.id,
            namespace="bookkeeping_audit",
        ).all()
        bindings = self.db.query(RelationDataMapping).filter_by(
            scenario_id=self.scenario.id,
        ).all()
        self.assertEqual(len(relations), len(upgrade_bookkeeping_audit.AUDIT_RELATION_SPECS))
        self.assertEqual(len(bindings), len(upgrade_bookkeeping_audit.AUDIT_RELATION_SPECS))
        self.assertTrue(all(item.name and item.api_name for item in relations))
        self.assertTrue(all(item.storage_kind == "foreign_key" for item in relations))
        self.assertTrue(all(item.mode == "target_fk" and item.status == "ready" for item in bindings))


class BookkeepingAuditTemplateAssetTests(unittest.TestCase):
    def test_native_audit_templates_are_valid_and_stay_within_agent_schema_budget(self) -> None:
        expected = {
            "audit_report.docx": ("docx", 18),
            "financial_statement_notes.docx": ("docx", 20),
            "audited_financial_statements.xlsx": ("xlsx", 30),
        }
        for filename, (artifact_format, maximum_variables) in expected.items():
            path = upgrade_bookkeeping_audit.ASSET_DIR / filename
            content = path.read_bytes()
            metadata = template_artifact_service.inspect_template(filename, content)
            placeholders = template_artifact_service._placeholder_paths(filename, content)
            self.assertEqual(metadata["format"], artifact_format)
            self.assertLessEqual(len(placeholders), maximum_variables)
            self.assertIn("project.project_id", placeholders)


if __name__ == "__main__":
    unittest.main()
