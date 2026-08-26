from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Agent, AuthorizationGrant, BusinessScenario, DataSource, LLMConfig, Tenant, User
from app.models import (
    DataMapping,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    OntologyWorkflow,
    RelationDataMapping,
)
from app.services import (
    agent_engine,
    business_query_service,
    datasource_service,
    medical_audit_service,
    permission_service,
    runtime_definition_service,
    template_artifact_service,
)
from app.services.agent_engine import AgentContext
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
            name=upgrade_medical_audit.SCENARIO_NAME,
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
        self.organization = permission_service.ensure_organization(
            self.db,
            tenant.id,
            owner_user_id=user.id,
        )
        self.db.commit()
        self.db.info["tenant_id"] = tenant.id
        self.db.info["user_id"] = user.id

    def tearDown(self) -> None:
        extra_engine = getattr(self, "_concurrent_upgrade_engine", None)
        if extra_engine is not None:
            extra_engine.dispose()
        datasource_service.invalidate_engine(self.source)
        self.db.close()
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def _build_source(path: Path) -> None:
        with closing(sqlite3.connect(path)) as connection:
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
                    "性别" TEXT,
                    "年龄" INTEGER,
                    "住院天数" REAL,
                    "入院诊断描述" TEXT,
                    "住院主诊断代码" TEXT,
                    "住院主诊断名称" TEXT,
                    "手术操作代码" TEXT,
                    "手术操作名称" TEXT
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
                    "开单医师姓名" TEXT,
                    "结算ID" TEXT,
                    "医药机构目录编码" TEXT,
                    "医药机构目录名称" TEXT,
                    "商品名" TEXT,
                    "符合范围金额" REAL,
                    "剂型名称" TEXT,
                    "使用频次描述" TEXT,
                    "周期天数" REAL
                );
                CREATE TABLE "结算表" (
                    "结算ID" TEXT,
                    "就诊ID" TEXT,
                    "定点医药机构编号" TEXT,
                    "定点医药机构名称" TEXT,
                    "人员编号" TEXT,
                    "人员姓名" TEXT,
                    "结算时间" TEXT,
                    "医疗费总额" REAL,
                    "符合范围金额" REAL,
                    "统筹基金支出" REAL,
                    "基金支付总额" REAL,
                    "个人支付金额" REAL,
                    "现金支付金额" REAL
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
                INSERT INTO "就诊表" (
                    "就诊ID","就诊凭证编号","定点医药机构编号","定点医药机构名称",
                    "医院等级","定点归属医保区划","人员编号","人员姓名",
                    "开始时间","结束时间","医疗类别","性别","年龄","住院天数",
                    "入院诊断描述","住院主诊断代码","住院主诊断名称",
                    "手术操作代码","手术操作名称"
                ) VALUES
                    ('E01','V01','F01','贵阳泰康乐综合医院','二级','520100','P01','患者一','2025-01-01','2025-01-01','门诊','女',42,1,'','','','',''),
                    ('E-D1','VD1','F01','贵阳泰康乐综合医院','二级','520100','PD1','日计价患者一','2025-02-01','2025-02-03','住院','男',60,3,'肺部感染','J18','肺炎','',''),
                    ('E-D2','VD2','F01','贵阳泰康乐综合医院','二级','520100','PD2','日计价患者二','2025-02-01','2025-02-04','住院','女',55,4,'术后观察','Z48','术后随诊','',''),
                    ('E-X1','VX1','F01','贵阳泰康乐综合医院','二级','520100','PX1','共现患者','2025-03-01','2025-03-01','门诊','男',48,1,'肠道检查','Z01','健康检查','',''),
                    ('E-T1','VT1','F01','贵阳泰康乐综合医院','二级','520100','PT1','用药患者','2025-04-01','2025-04-02','住院','女',50,2,'眩晕','R42','眩晕','','');
                INSERT INTO "结算表" VALUES
                    ('S-D1','E-D1','F01','贵阳泰康乐综合医院','PD1','日计价患者一',
                     '2025-02-04',100,90,70,75,25,25);
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
                'INSERT INTO "项目明细表" ('
                '"记账流水号","就诊ID","定点医药机构编号","定点医药机构名称",'
                '"人员编号","医保目录编码","医保目录名称","数量","单价",'
                '"明细项目费用总额","费用发生时间","医疗收费项目类别",'
                '"目录类别","规格","开单科室名称","开单医师姓名"'
                ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                rows,
            )
            connection.executemany(
                'INSERT INTO "项目明细表" ('
                '"记账流水号","就诊ID","定点医药机构编号","定点医药机构名称",'
                '"人员编号","医保目录编码","医保目录名称","数量","单价",'
                '"明细项目费用总额","费用发生时间","医疗收费项目类别",'
                '"目录类别","规格","开单科室名称","开单医师姓名",'
                '"结算ID","医药机构目录编码","医药机构目录名称","商品名",'
                '"符合范围金额","剂型名称","使用频次描述","周期天数"'
                ') VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                [
                    (100, "E-D1", "F01", "贵阳泰康乐综合医院", "PD1", "N-I", "Ⅰ级护理", 4, 15, 60, "2025-02-01 08:00:00", "护理", "诊疗项目", "", "住院部", "医师", "S-D1", "", "Ⅰ级护理", "", 60, "", "每日一次", 4),
                    (101, "E-D2", "F01", "贵阳泰康乐综合医院", "PD2", "N-I", "Ⅰ级护理", 4, 15, 60, "2025-02-01 08:00:00", "护理", "诊疗项目", "", "住院部", "医师", "", "", "Ⅰ级护理", "", 60, "", "每日一次", 4),
                    (110, "E-X1", "F01", "贵阳泰康乐综合医院", "PX1", "S-COLON", "电子结肠镜检查", 1, 200, 200, "2025-03-01 09:00:00", "检查", "诊疗项目", "", "内镜室", "医师", "", "", "电子结肠镜检查", "", 180, "", "", 1),
                    (111, "E-X1", "F01", "贵阳泰康乐综合医院", "PX1", "S-SIGMOID", "电子乙状结肠镜检查", 1, 47.79, 47.79, "2025-03-01 09:05:00", "检查", "诊疗项目", "", "内镜室", "医师", "", "", "电子乙状结肠镜检查", "", 43, "", "", 1),
                    (120, "E-T1", "F01", "贵阳泰康乐综合医院", "PT1", "D-TIANMA", "天麻素注射液", 1, 10, 10, "2025-04-01 10:00:00", "药品", "西药", "2ml", "住院部", "医师", "", "D-TIANMA-H", "天麻素注射液", "天麻素", 9, "注射剂", "每日一次", 2),
                    (121, "E-T1", "F01", "贵阳泰康乐综合医院", "PT1", "D-TIANMA", "天麻素注射液", 1, 11, 11, "2025-04-02 10:00:00", "药品", "西药", "2ml", "住院部", "医师", "", "D-TIANMA-H", "天麻素注射液", "天麻素", 10, "注射剂", "每日一次", 2),
                ],
            )
            connection.commit()

    def _add_upgrade_agent(
        self,
        agent_id: str,
        *,
        scenario: BusinessScenario | None = None,
        name: str = upgrade_medical_audit.AGENT_NAME,
    ) -> Agent:
        target = scenario or self.scenario
        agent = Agent(
            id=agent_id,
            tenant_id=target.tenant_id,
            name=name,
            scenario_id=target.id,
            system_prompt="升级前提示词",
            data_source_ids=[],
            capability_scope={},
        )
        self.db.add(agent)
        return agent

    def _medical_mapping_contract(
        self,
    ) -> medical_audit_service.MedicalAuditMappingContract:
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
        return medical_audit_service.resolve_mapping_contract(
            [self.source],
            list(definition.mappings.values()),
            definition=definition,
        )

    def _seed_unmarked_v1_medical_ontology(
        self,
    ) -> tuple[dict[str, OntologyEntity], list[OntologyProperty], list[OntologyRelation]]:
        entities: dict[str, OntologyEntity] = {}
        properties: list[OntologyProperty] = []
        for entity_index, spec in enumerate(upgrade_medical_audit.ENTITY_SPECS):
            entity = OntologyEntity(
                id=f"medicalv1entity{entity_index:02d}",
                scenario_id=self.scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
                namespace=upgrade_medical_audit.NAMESPACE,
                lifecycle_status="active",
                description=spec["description"],
                icon=spec["icon"],
                color=spec["color"],
                is_abstract=False,
                state_property=spec.get("state_property", ""),
            )
            self.db.add(entity)
            entities[entity.name] = entity
            for property_index, property_spec in enumerate(spec["properties"]):
                prop = OntologyProperty(
                    id=f"medicalv1prop{entity_index:02d}{property_index:02d}",
                    entity_id=entity.id,
                    name=property_spec["name"],
                    api_name=property_spec["api_name"],
                    data_type=property_spec["data_type"],
                    description=property_spec.get("description", ""),
                    is_key=bool(property_spec.get("is_key")),
                    is_title=bool(property_spec.get("is_title")),
                    is_required=bool(property_spec.get("is_required")),
                    is_sensitive=bool(property_spec.get("is_sensitive")),
                    is_enum=bool(property_spec.get("is_enum")),
                    enum_values=list(property_spec.get("enum_values") or []),
                    default_value="",
                    constraints={},
                )
                self.db.add(prop)
                properties.append(prop)
        self.db.flush()
        relations: list[OntologyRelation] = []
        for relation_index, spec in enumerate(upgrade_medical_audit.RELATION_SPECS):
            relation = OntologyRelation(
                id=f"medicalv1relation{relation_index:02d}",
                scenario_id=self.scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
                namespace=upgrade_medical_audit.NAMESPACE,
                source_entity_id=entities[spec["source"]].id,
                target_entity_id=entities[spec["target"]].id,
                source_display_name=spec["source_display_name"],
                source_api_name=spec["source_api_name"],
                target_display_name=spec["target_display_name"],
                target_api_name=spec["target_api_name"],
                relation_type=spec["relation_type"],
                storage_kind="foreign_key" if spec.get("mapping_mode") else "none",
                constraints={},
                description=upgrade_medical_audit._relation_description(spec),
            )
            self.db.add(relation)
            relations.append(relation)
        self.db.commit()
        return entities, properties, relations

    def _seed_unmarked_deprecated_legacy_model(self) -> list[OntologyEntity]:
        entities: list[OntologyEntity] = []
        property_serial = 1000
        for entity_index, (name, shape) in enumerate(
            sorted(upgrade_medical_audit.LEGACY_ENTITY_SHAPES.items())
        ):
            entity_id = f"{entity_index + 500:032x}"
            entity = OntologyEntity(
                id=entity_id,
                scenario_id=self.scenario.id,
                name=name,
                api_name=(
                    entity_id if entity_index % 2 else f"entity_{entity_id}"
                ),
                namespace="default",
                lifecycle_status="deprecated",
                description=shape["description"],
                icon="box",
                color="#4f46e5",
                is_abstract=False,
                state_property="",
            )
            self.db.add(entity)
            entities.append(entity)
            for (
                property_name,
                data_type,
                is_key,
                is_required,
                description,
            ) in shape["properties"]:
                property_serial += 1
                property_id = f"{property_serial:032x}"
                self.db.add(OntologyProperty(
                    id=property_id,
                    entity_id=entity.id,
                    name=property_name,
                    api_name=(
                        property_id
                        if property_serial % 2
                        else f"property_{property_id}"
                    ),
                    data_type=data_type,
                    description=description,
                    is_key=is_key,
                    is_title=is_key,
                    is_required=is_required,
                    is_sensitive=False,
                    is_enum=False,
                    enum_values=[],
                    default_value=(
                        ""
                        if name == "违规记录" and property_name == "医院名称"
                        else None
                    ),
                    constraints={},
                ))
        self.db.commit()
        return entities

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

        pages = []
        offset = 0
        while True:
            page = business_query_service.query_business_data(
                self.db,
                definition=definition,
                mappings=list(definition.mappings.values()),
                data_sources=[self.source],
                args={
                    "base_entity": "收费明细",
                    "base_properties": ["收费明细ID", "收费金额", "发生时间"],
                    "base_filters": [
                        {
                            "property": "医疗机构名称",
                            "op": "eq",
                            "value": "贵阳泰康乐综合医院",
                        },
                        {"property": "服务项目名称", "op": "eq", "value": "刮痧治疗"},
                        {"property": "收费数量", "op": "gt", "value": 2},
                    ],
                    "sort": [
                        {
                            "entity_name": "收费明细",
                            "property": "发生时间",
                            "direction": "asc",
                        }
                    ],
                    "limit": 4,
                    "offset": offset,
                },
            )
            pages.append(page)
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]

        self.assertEqual(
            [(page["offset"], page["row_count"]) for page in pages],
            [(0, 4), (4, 4), (8, 2)],
        )
        self.assertEqual(
            [page["next_offset"] for page in pages],
            [4, 8, None],
        )
        self.assertEqual(
            [page["truncated"] for page in pages],
            [True, True, False],
        )
        paged_records = [record for page in pages for record in page["records"]]
        paged_ids = [record["收费明细ID"] for record in paged_records]
        self.assertEqual(len(paged_ids), 10)
        self.assertEqual(len(set(paged_ids)), 10)
        self.assertAlmostEqual(
            sum(float(item["收费金额"]) for item in paged_records),
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

    def test_controlled_audit_strategies_return_evidence_totals_and_zero_semantics(self) -> None:
        contract = self._medical_mapping_contract()
        threshold = medical_audit_service.run_medical_audit(
            contract,
            {
                "strategy": "charge_threshold",
                "facility_name": "贵阳泰康乐综合医院",
                "service_name": "刮痧治疗",
                "threshold": 2,
                "limit": 3,
            },
        )
        self.assertEqual(threshold["summary"]["violation_count"], 10)
        self.assertAlmostEqual(threshold["summary"]["violation_amount"], 379.08, places=2)
        self.assertEqual(threshold["row_count"], 3)
        self.assertTrue(threshold["truncated"])
        self.assertEqual(threshold["next_offset"], 3)
        self.assertEqual(threshold["evidence"]["matching"], "exact")
        self.assertEqual(threshold["lineage"]["schema_version"], 2)
        self.assertEqual(
            threshold["lineage"]["mapping_contract"], contract.lineage()
        )
        self.assertEqual(
            set(threshold["lineage"]["mapping_contract"]["mapping_ids"]),
            {"charge", "encounter"},
        )
        self.assertNotIn("patient_id", threshold["records"][0])
        next_page = medical_audit_service.run_medical_audit(
            contract,
            {
                "strategy": "charge_threshold",
                "facility_name": "贵阳泰康乐综合医院",
                "service_name": "刮痧治疗",
                "threshold": 2,
                "limit": 3,
                "offset": threshold["next_offset"],
            },
        )
        self.assertEqual(next_page["summary"]["violation_count"], 10)
        self.assertEqual(next_page["row_count"], 3)
        self.assertTrue(
            {row["charge_line_id"] for row in threshold["records"]}.isdisjoint(
                {row["charge_line_id"] for row in next_page["records"]}
            )
        )

        daily = medical_audit_service.run_medical_audit(
            contract,
            {
                "strategy": "daily_overstay",
                "facility_name": "贵阳泰康乐综合医院",
                "service_names": ["Ⅰ级护理"],
            },
        )
        self.assertEqual(daily["summary"]["violation_count"], 1)
        self.assertEqual(daily["summary"]["excess_quantity"], 1)
        self.assertEqual(daily["summary"]["violation_amount"], 15)
        self.assertEqual(daily["records"][0]["encounter_id"], "E-D1")

        duplicate = medical_audit_service.run_medical_audit(
            contract,
            {
                "strategy": "included_service_duplicate",
                "included_service": "电子结肠镜检查",
                "duplicate_service": "电子乙状结肠镜检查",
            },
        )
        self.assertEqual(duplicate["summary"]["violation_count"], 1)
        self.assertEqual(duplicate["summary"]["violation_amount"], 47.79)
        self.assertEqual(duplicate["records"][0]["duplicate_service"], "电子乙状结肠镜检查")

        limited = medical_audit_service.run_medical_audit(
            contract,
            {
                "strategy": "limited_drug_duration",
                "drug_name": "天麻素注射液",
                "max_days": 14,
            },
        )
        self.assertTrue(limited["ok"])
        self.assertTrue(limited["empty"])
        self.assertEqual(limited["summary"]["violation_count"], 0)
        self.assertEqual(limited["summary"]["audited_scope_count"], 1)
        self.assertEqual(limited["summary"]["max_observed_days"], 2)
        self.assertEqual(limited["records"], [])
        self.assertIn("未发现", limited["message"])

        with self.assertRaises(medical_audit_service.MedicalAuditError) as rejected:
            medical_audit_service.run_medical_audit(
                contract,
                {
                    "strategy": "charge_threshold",
                    "service_name": "刮痧治疗",
                    "threshold": 2,
                    "sql": 'DELETE FROM "项目明细表"',
                },
            )
        self.assertEqual(rejected.exception.code, "INVALID_TOOL_ARGUMENTS")
        with self.assertRaises(medical_audit_service.MedicalAuditError):
            medical_audit_service.run_medical_audit(
                contract,
                {
                    "strategy": "charge_threshold",
                    "service_name": {"not": "a string"},
                    "threshold": 2,
                },
            )

    def test_medical_truth_guard_rejects_excluded_or_unresolved_facility_scope(
        self,
    ) -> None:
        contract = self._medical_mapping_contract()
        facility = "贵阳泰康乐综合医院"
        arguments = {
            "strategy": "charge_threshold",
            "facility_name": facility,
            "service_name": "刮痧治疗",
            "threshold": 2,
            "limit": 20,
        }
        result = medical_audit_service.run_medical_audit(contract, arguments)
        policy = medical_audit_service.access_policy(
            result["lineage"]["property_refs"]
        )

        excluded_request = (
            f"除{facility}以外所有医疗机构，"
            "审计刮痧治疗收费数量大于2次的违规记录"
        )
        excluded_lookup = medical_audit_service.find_facility_names_in_text(
            contract,
            excluded_request,
            property_access=policy,
        )
        self.assertEqual(excluded_lookup, [facility])
        excluded = agent_engine._truthful_final_content(
            "审计完成。",
            user_message=excluded_request,
            tool_outcomes=[{
                "name": "run_medical_audit",
                "arguments": arguments,
                "result": json.dumps(result, ensure_ascii=False),
            }],
            controlled_medical_audit=True,
            authoritative_medical_facilities=excluded_lookup,
            medical_facility_lookup_succeeded=True,
        )
        self.assertIn("未形成可验证审计结论", excluded)
        self.assertNotIn("医保确定性汇总", excluded)

        equivalent_request = (
            f"请审计{facility}刮痧治疗收费数量大于2次的违规记录"
        )
        equivalent = agent_engine._truthful_final_content(
            "审计完成。",
            user_message=equivalent_request,
            tool_outcomes=[{
                "name": "run_medical_audit",
                "arguments": arguments,
                "result": json.dumps(result, ensure_ascii=False),
            }],
            controlled_medical_audit=True,
            authoritative_medical_facilities=[facility],
            medical_facility_lookup_succeeded=True,
        )
        self.assertIn("医保确定性汇总", equivalent)
        self.assertNotIn("未形成可验证审计结论", equivalent)

        alias = "泰康乐医院"
        alias_request = f"请审计{alias}刮痧治疗收费数量大于2次的违规记录"
        alias_arguments = {**arguments, "facility_name": alias}
        alias_result = medical_audit_service.run_medical_audit(
            contract,
            alias_arguments,
        )
        self.assertEqual(alias_result["summary"]["violation_count"], 0)
        alias_lookup = medical_audit_service.find_facility_names_in_text(
            contract,
            alias_request,
            property_access=policy,
        )
        self.assertEqual(alias_lookup, [])
        unresolved_alias = agent_engine._truthful_final_content(
            "未发现违规记录。",
            user_message=alias_request,
            tool_outcomes=[{
                "name": "run_medical_audit",
                "arguments": alias_arguments,
                "result": json.dumps(alias_result, ensure_ascii=False),
            }],
            controlled_medical_audit=True,
            authoritative_medical_facilities=alias_lookup,
            medical_facility_lookup_succeeded=True,
        )
        self.assertIn("未形成可验证审计结论", unresolved_alias)
        self.assertNotIn("医保确定性汇总", unresolved_alias)

    def test_medical_audit_mapping_contract_fails_closed_without_guessing(self) -> None:
        contract = self._medical_mapping_contract()
        definition = runtime_definition_service.resolve_active(
            self.db,
            self.scenario,
            environment="dev",
        )
        mappings = list(definition.mappings.values())
        with self.assertRaisesRegex(
            medical_audit_service.MedicalAuditError,
            "medical_charge_line",
        ):
            medical_audit_service.resolve_mapping_contract(
                [self.source], [], definition=definition
            )

        charge_mapping = next(
            mapping
            for mapping in mappings
            if definition.entities[str(mapping.entity_id)].api_name
            == "medical_charge_line"
        )
        duplicate = SimpleNamespace(**vars(charge_mapping))
        duplicate.id = "duplicate-medical-charge-mapping"
        with self.assertRaisesRegex(
            medical_audit_service.MedicalAuditError,
            "多个当前运行映射",
        ):
            medical_audit_service.resolve_mapping_contract(
                [self.source], [*mappings, duplicate], definition=definition
            )

        invalid_transform = SimpleNamespace(**vars(charge_mapping))
        invalid_transform.transform_rules = {
            "收费数量": [{"op": "multiply", "value": 2}]
        }
        with self.assertRaisesRegex(
            medical_audit_service.MedicalAuditError,
            "不能证明.*转换规则",
        ):
            medical_audit_service.resolve_mapping_contract(
                [self.source],
                [
                    invalid_transform if item.id == charge_mapping.id else item
                    for item in mappings
                ],
                definition=definition,
            )

        invalid_status = SimpleNamespace(**vars(charge_mapping))
        invalid_status.status = "error"
        invalid_status.last_error = "forced mapping error"
        with self.assertRaisesRegex(
            medical_audit_service.MedicalAuditError,
            "运行映射未就绪",
        ):
            medical_audit_service.resolve_mapping_contract(
                [self.source],
                [
                    invalid_status if item.id == charge_mapping.id else item
                    for item in mappings
                ],
                definition=definition,
            )

        # A familiar fallback table must not be selected when the explicitly
        # mapped table disappears after the contract was resolved.
        with closing(sqlite3.connect(self.source_path)) as connection:
            connection.execute(
                'CREATE TABLE "费用明细表" AS SELECT * FROM "项目明细表"'
            )
            connection.execute('DROP TABLE "项目明细表"')
            connection.commit()
        with self.assertRaises(medical_audit_service.MedicalAuditError) as rejected:
            medical_audit_service.run_medical_audit(
                contract,
                {
                    "strategy": "charge_threshold",
                    "service_name": "刮痧治疗",
                    "threshold": 2,
                },
            )
        self.assertEqual(rejected.exception.code, "RESOURCE_NOT_FOUND")

    def test_historic_medical_result_is_pinned_to_mapping_and_definition(self) -> None:
        contract = self._medical_mapping_contract()
        args = {
            "strategy": "charge_threshold",
            "service_name": "刮痧治疗",
            "threshold": 2,
            "limit": 2,
        }
        result = medical_audit_service.run_medical_audit(contract, args)
        policy = medical_audit_service.access_policy(
            result["lineage"]["property_refs"]
        )
        self.assertTrue(medical_audit_service.authorize_historic_result(
            contract,
            args,
            result,
            property_access=policy,
        ))

        tampered = json.loads(json.dumps(result, ensure_ascii=False))
        tampered["lineage"]["mapping_contract"]["fingerprint"] = "0" * 64
        self.assertFalse(medical_audit_service.authorize_historic_result(
            contract,
            args,
            tampered,
            property_access=policy,
        ))

        charge_entity = self.db.scalar(select(OntologyEntity).where(
            OntologyEntity.scenario_id == self.scenario.id,
            OntologyEntity.api_name == "medical_charge_line",
        ))
        charge_entity.description += "\n定义版本变化"
        self.db.commit()
        changed_definition = runtime_definition_service.resolve_active(
            self.db,
            self.scenario,
            environment="dev",
        )
        changed_contract = medical_audit_service.resolve_mapping_contract(
            [self.source],
            list(changed_definition.mappings.values()),
            definition=changed_definition,
        )
        self.assertNotEqual(
            changed_contract.definition_provenance["definition_hash"],
            contract.definition_provenance["definition_hash"],
        )
        self.assertFalse(medical_audit_service.authorize_historic_result(
            changed_contract,
            args,
            result,
            property_access=policy,
        ))

    def test_main_legacy_projection_accepts_only_historic_null_or_empty_defaults(self) -> None:
        entity_id = "1451faa6e89f4738a56a24af2f11f2b6"
        entity = OntologyEntity(
            id=entity_id,
            scenario_id=self.scenario.id,
            name="业务数据",
            description="用户上传的业务数据实体",
            icon="box",
            color="#4f46e5",
            is_abstract=False,
            namespace="default",
            state_property="",
            api_name=f"entity_{entity_id}",
            lifecycle_status="deprecated",
        )
        projected_properties = (
            ("34f43abf1a11456dbc9af03ecb025b7e", "上传时间", "datetime", False, True, True),
            ("724021630a974419adf6ab3595386c7c", "关联客户", "string", False, False, True),
            ("c8e87fd2e8e34393b90ffe1e0d05cb79", "业务数据ID", "string", True, True, False),
            ("cf1c550793b548e2800ba15f796efe1a", "状态", "string", False, False, False),
            ("dcd6cfc6865e42568884270f8bb68bf9", "数据名称", "string", False, True, False),
            ("ed1202bcb73d41d8856f7aadd82a9aaa", "数据内容", "json", False, False, False),
        )
        properties: list[OntologyProperty] = []
        for (
            property_id,
            name,
            data_type,
            is_key,
            is_required,
            prefixed_api_name,
        ) in projected_properties:
            prop = OntologyProperty(
                id=property_id,
                entity_id=entity.id,
                name=name,
                api_name=(
                    f"property_{property_id}" if prefixed_api_name else property_id
                ),
                data_type=data_type,
                description="",
                is_key=is_key,
                is_title=is_key,
                is_required=is_required,
                is_sensitive=False,
                is_enum=False,
                enum_values=[],
                default_value=None,
                constraints={},
            )
            properties.append(prop)
        self.db.add_all([entity, *properties])
        self.db.commit()

        self.assertTrue(upgrade_medical_audit._legacy_retired_entity_matches(entity))
        key_property = next(item for item in properties if item.is_key)
        key_property.default_value = ""
        self.assertTrue(upgrade_medical_audit._legacy_retired_entity_matches(entity))
        for disallowed_default in ("user-default", 0, False, [], {}):
            with self.subTest(disallowed_default=disallowed_default):
                key_property.default_value = disallowed_default
                self.assertFalse(
                    upgrade_medical_audit._legacy_retired_entity_matches(entity)
                )
        key_property.default_value = None
        key_property.is_title = False
        self.assertFalse(upgrade_medical_audit._legacy_retired_entity_matches(entity))
        key_property.is_title = True
        key_property.api_name = "0" * 32
        self.assertFalse(upgrade_medical_audit._legacy_retired_entity_matches(entity))

    def test_main_domain_mapping_projection_requires_stable_id_and_full_contract(self) -> None:
        entity = SimpleNamespace(
            id="b6ad3ca702eb4f1baad93cba22a5429d",
            name="医疗机构",
            api_name="medical_facility",
        )
        contract = {
            "table_name": "医疗机构视图",
            "column_map": {
                "医疗机构ID": "定点医药机构编号",
                "医疗机构名称": "定点医药机构名称",
                "医院等级": "医院等级",
                "医保区划": "定点归属医保区划",
            },
            "transform_rules": {},
            "status": "ready",
            "last_error": "",
        }
        mapping = SimpleNamespace(
            id="c93d6fdcc165415c903f34efe1ba893b",
            entity_id=entity.id,
            scenario_id="cc5d3ff36d2a468596dfa9f8ef2995da",
            data_source_id="a2d20a398ed744e7839acb910f377d6a",
            created_at="2026-08-25 12:00:47.766866",
            data_source_binding_key="data_source:医保审计业务库:sqlite",
            data_source_binding_ref={
                "adapter": "sqlite",
                "required_capabilities": ["sql_read"],
            },
            environment_status={"dev": {"status": "ready", "last_error": ""}},
            **contract,
        )
        self.assertTrue(
            upgrade_medical_audit._historic_domain_mapping_matches(
                mapping,
                entity,
                contract,
            )
        )
        mapping.id = "0" * 32
        self.assertFalse(
            upgrade_medical_audit._historic_domain_mapping_matches(
                mapping,
                entity,
                contract,
            )
        )
        with self.assertRaisesRegex(RuntimeError, "未标记的现有资源"):
            upgrade_medical_audit._assert_mapping_owned(mapping, entity, contract)
        mapping.id = "c93d6fdcc165415c903f34efe1ba893b"
        mapping.data_source_binding_ref = {
            **mapping.data_source_binding_ref,
            "user_override": True,
        }
        self.assertFalse(
            upgrade_medical_audit._historic_domain_mapping_matches(
                mapping,
                entity,
                contract,
            )
        )

    def test_main_relation_mapping_projection_requires_stable_full_contract(self) -> None:
        relation = SimpleNamespace(
            id="caf17e8a84234b92ae913aa7ef250196",
            name="就诊包含收费明细",
            api_name="encounter_charge_lines",
            description=upgrade_medical_audit._relation_description(
                upgrade_medical_audit.RELATION_SPECS[1]
            ),
        )
        contract = {
            "scenario_id": "cc5d3ff36d2a468596dfa9f8ef2995da",
            "relation_id": relation.id,
            "source_mapping_id": "977d2919009d4ed59c8320dcc3f3bb2c",
            "target_mapping_id": "249218f9fcaa4c92bf6291cb9184d6ce",
            "mode": "target_fk",
            "data_source_id": "a2d20a398ed744e7839acb910f377d6a",
            "table_name": "项目明细表",
            "foreign_key_column": "就诊ID",
            "source_key_column": "",
            "target_key_column": "",
            "status": "ready",
            "last_error": "",
        }
        binding = SimpleNamespace(
            id="70f417c5f02e43968eec694501da9f7e",
            created_at="2026-08-23 17:57:26.345774",
            data_source_binding_key="data_source:医保审计业务库:sqlite",
            data_source_binding_ref={
                "adapter": "sqlite",
                "required_capabilities": ["sql_read"],
            },
            **contract,
        )
        self.assertTrue(
            upgrade_medical_audit._historic_relation_mapping_matches(
                binding, relation, contract
            )
        )
        upgrade_medical_audit._assert_relation_mapping_owned(
            binding,
            relation,
            contract,
            relation_was_marked=False,
        )
        binding.id = "0" * 32
        self.assertFalse(
            upgrade_medical_audit._historic_relation_mapping_matches(
                binding, relation, contract
            )
        )
        with self.assertRaisesRegex(RuntimeError, "未标记的现有资源"):
            upgrade_medical_audit._assert_relation_mapping_owned(
                binding,
                relation,
                contract,
                relation_was_marked=False,
            )
        binding.id = "70f417c5f02e43968eec694501da9f7e"
        binding.foreign_key_column = "用户自定义列"
        with self.assertRaisesRegex(RuntimeError, "定义与恢复包不一致"):
            upgrade_medical_audit._assert_relation_mapping_owned(
                binding,
                relation,
                contract,
                relation_was_marked=False,
            )

    def test_main_legacy_workflow_projection_requires_definition_fingerprint(self) -> None:
        workflow = SimpleNamespace(
            id="48968bf1066d453898f61e58e30fc904",
            scenario_id="cc5d3ff36d2a468596dfa9f8ef2995da",
            name="DAG????",
            description=(
                "???DAG????\n" + upgrade_medical_audit.OLD_WORKFLOW_RETIREMENT_LINE
            ),
            trigger_type="manual",
            trigger_config={},
            steps=[],
            nodes=[
                {
                    "id": "start",
                    "type": "start",
                    "name": "??",
                    "position": {"x": 0, "y": -264},
                    "data": {"name": "??"},
                },
                {
                    "id": "n1",
                    "type": "script",
                    "name": "??",
                    "position": {"x": 0, "y": -132},
                    "data": {"script": "result = 6 * 7", "name": "??"},
                },
                {
                    "id": "n2",
                    "type": "script",
                    "name": "??",
                    "position": {"x": 0, "y": 0},
                    "data": {
                        "script": "result = (n1.result > 40)",
                        "name": "??",
                    },
                },
                {
                    "id": "n3",
                    "type": "script",
                    "name": "????",
                    "position": {"x": 0, "y": 132},
                    "data": {
                        "script": "result = '??: ' + str(n2.result)",
                        "name": "????",
                    },
                },
                {
                    "id": "end",
                    "type": "end",
                    "name": "??",
                    "position": {"x": 0, "y": 264},
                    "data": {"name": "??"},
                },
            ],
            edges=[
                {"id": "e1", "source": "start", "target": "n1", "label": ""},
                {"id": "e2", "source": "n1", "target": "n2", "label": ""},
                {"id": "e3", "source": "n2", "target": "n3", "label": "true"},
                {"id": "e4", "source": "n2", "target": "end", "label": "false"},
                {"id": "e5", "source": "n3", "target": "end", "label": ""},
            ],
            status="disabled",
            enabled=False,
            access_scope="tenant",
            created_at="2026-08-18 03:51:03.471230",
        )
        self.assertTrue(upgrade_medical_audit._historic_workflow_matches(workflow))
        workflow.nodes[1]["data"]["script"] = "result = 'user override'"
        self.assertFalse(upgrade_medical_audit._historic_workflow_matches(workflow))
        with self.assertRaisesRegex(RuntimeError, "不符合历史精确契约"):
            upgrade_medical_audit._assert_workflow_owned(workflow)

    def test_upgrade_is_idempotent_and_agent_tool_completes_audit_loop(self) -> None:
        legacy_entities, legacy_properties, legacy_relations = (
            self._seed_unmarked_v1_medical_ontology()
        )
        deprecated_legacy_entities = self._seed_unmarked_deprecated_legacy_model()
        upgrade_medical_audit._ensure_domain_views(self.source)
        legacy_mappings = upgrade_medical_audit._upsert_mappings(
            self.db,
            self.scenario,
            self.source,
            legacy_entities,
        )
        upgrade_medical_audit._upsert_relations(
            self.db,
            self.scenario,
            self.source,
            legacy_entities,
            legacy_mappings,
        )
        for mapping in legacy_mappings.values():
            environment_status = dict(mapping.environment_status or {})
            environment_status.pop(upgrade_medical_audit.MAPPING_PROVENANCE_KEY, None)
            mapping.environment_status = environment_status
        for relation, spec in zip(
            legacy_relations,
            upgrade_medical_audit.RELATION_SPECS,
            strict=True,
        ):
            relation.description = upgrade_medical_audit._relation_description(spec)
        self.db.commit()
        legacy_entity_ids = {item.id for item in legacy_entities.values()}
        legacy_property_ids = {item.id for item in legacy_properties}
        legacy_relation_ids = {item.id for item in legacy_relations}
        legacy_mapping_ids = {item.id for item in legacy_mappings.values()}
        deprecated_legacy_ids = {item.id for item in deprecated_legacy_entities}
        llm = LLMConfig(
            id="llm-medical-task",
            tenant_id=self.scenario.tenant_id,
            name="医保测试模型",
            provider="openai",
            model="test-model",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        agent = Agent(
            id="agent-medical-task",
            tenant_id=self.scenario.tenant_id,
            name=upgrade_medical_audit.AGENT_NAME,
            scenario_id=self.scenario.id,
            llm_config_id=llm.id,
            system_prompt="你是医保审计助手。",
            data_source_ids=[],
            capability_scope={},
        )
        self.db.add_all([llm, agent])
        self.db.commit()

        first = upgrade_medical_audit.upgrade(self.db)
        second = upgrade_medical_audit.upgrade(self.db)
        self.assertEqual(first["audit_version"], medical_audit_service.AUDIT_VERSION)
        self.assertEqual(second["audit_version"], medical_audit_service.AUDIT_VERSION)
        self.db.refresh(agent)
        self.assertEqual(agent.system_prompt.count(upgrade_medical_audit.PROMPT_MARKER), 1)
        self.assertEqual(agent.data_source_ids.count(self.source.id), 1)
        self.assertEqual(agent.capability_scope, {})
        self.assertEqual(
            legacy_entity_ids,
            set(self.db.scalars(
                select(OntologyEntity.id).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.namespace == upgrade_medical_audit.NAMESPACE,
                )
            )),
        )
        self.assertEqual(
            legacy_property_ids,
            set(self.db.scalars(
                select(OntologyProperty.id)
                .join(OntologyEntity, OntologyProperty.entity_id == OntologyEntity.id)
                .where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.namespace == upgrade_medical_audit.NAMESPACE,
                )
            )),
        )
        self.assertEqual(
            legacy_relation_ids,
            set(self.db.scalars(
                select(OntologyRelation.id).where(
                    OntologyRelation.scenario_id == self.scenario.id
                )
            )),
        )
        refreshed_mappings = list(self.db.scalars(
            select(DataMapping).where(DataMapping.scenario_id == self.scenario.id)
        ))
        self.assertEqual(legacy_mapping_ids, {item.id for item in refreshed_mappings})
        self.assertTrue(all(
            (item.environment_status or {}).get(
                upgrade_medical_audit.MAPPING_PROVENANCE_KEY
            ) == upgrade_medical_audit.MAPPING_PROVENANCE_VALUE
            for item in refreshed_mappings
        ))
        refreshed_legacy_entities = [
            self.db.get(OntologyEntity, entity_id)
            for entity_id in deprecated_legacy_ids
        ]
        self.assertTrue(all(
            item is not None
            and item.lifecycle_status == "deprecated"
            and item.description.splitlines().count(
                upgrade_medical_audit.LEGACY_RETIREMENT_MARKER
            ) == 1
            for item in refreshed_legacy_entities
        ))
        for resource in [
            *self.db.scalars(
                select(OntologyEntity).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.namespace == upgrade_medical_audit.NAMESPACE,
                )
            ),
            *self.db.scalars(
                select(OntologyProperty).join(
                    OntologyEntity,
                    OntologyProperty.entity_id == OntologyEntity.id,
                ).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.namespace == upgrade_medical_audit.NAMESPACE,
                )
            ),
            *self.db.scalars(
                select(OntologyRelation).where(
                    OntologyRelation.scenario_id == self.scenario.id
                )
            ),
        ]:
            self.assertEqual(
                (resource.description or "").splitlines().count(
                    upgrade_medical_audit.RECOVERY_MARKER
                ),
                1,
            )

        settlement = self.db.execute(
            select(OntologyEntity).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.api_name == "medical_settlement",
            )
        ).scalars().one()
        settlement_mapping = self.db.execute(
            select(DataMapping).where(DataMapping.entity_id == settlement.id)
        ).scalars().one()
        self.assertEqual(settlement_mapping.status, "ready")
        self.assertEqual(settlement_mapping.table_name, "结算表")
        self.assertEqual(settlement_mapping.column_map["符合范围金额"], "符合范围金额")
        encounter_settlement = self.db.execute(
            select(OntologyRelation).where(
                OntologyRelation.scenario_id == self.scenario.id,
                OntologyRelation.api_name == "encounter_settlements",
            )
        ).scalars().one()
        self.assertEqual(encounter_settlement.data_mapping.status, "ready")
        self.assertEqual(encounter_settlement.data_mapping.foreign_key_column, "就诊ID")

        context = AgentContext(self.db, agent, llm)
        tool_names = {
            item["function"]["name"]
            for item in context.build_tools()
        }
        self.assertIn("run_medical_audit", tool_names)
        payload = json.loads(
            context.execute_tool(
                "run_medical_audit",
                {
                    "strategy": "charge_threshold",
                    "facility_name": "贵阳泰康乐综合医院",
                    "service_name": "刮痧治疗",
                    "threshold": 2,
                    "limit": 2,
                },
            )
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["violation_count"], 10)
        self.assertEqual(payload["summary"]["violation_amount"], 379.08)
        self.assertTrue(payload["truncated"])
        self.assertIn("patient_id", payload["records"][0])
        self.assertTrue(
            context.authorize_historic_tool_result(
                "run_medical_audit",
                {
                    "strategy": "charge_threshold",
                    "facility_name": "贵阳泰康乐综合医院",
                    "service_name": "刮痧治疗",
                    "threshold": 2,
                    "limit": 2,
                },
                json.dumps(payload, ensure_ascii=False),
            )
        )

        rejected = json.loads(
            context.execute_tool(
                "run_medical_audit",
                {
                    "strategy": "charge_threshold",
                    "service_name": "刮痧治疗",
                    "threshold": 2,
                    "table": "项目明细表",
                },
            )
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "INVALID_TOOL_ARGUMENTS")

        self.scenario.namespace = "bookkeeping_audit"
        self.assertNotIn(
            "run_medical_audit",
            {item["function"]["name"] for item in context.build_tools()},
        )
        forged = json.loads(context.execute_tool("run_medical_audit", {"strategy": "charge_threshold"}))
        self.assertEqual(forged["error"]["code"], "DIRECT_TOOL_DISABLED")

    def test_medical_audit_property_acl_is_fail_closed_for_dependencies_and_outputs(self) -> None:
        llm = LLMConfig(
            id="llm-medical-acl",
            tenant_id=self.scenario.tenant_id,
            name="医保字段权限测试模型",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        agent = Agent(
            id="agent-medical-acl",
            tenant_id=self.scenario.tenant_id,
            name=upgrade_medical_audit.AGENT_NAME,
            scenario_id=self.scenario.id,
            llm_config_id=llm.id,
            data_source_ids=[],
            capability_scope={},
        )
        self.db.add_all([llm, agent])
        self.db.commit()
        upgrade_medical_audit.upgrade(
            self.db, agent_id=agent.id, source_id=self.source.id
        )
        self.db.refresh(agent)
        owner_context = AgentContext(self.db, agent, llm)
        args = {
            "strategy": "charge_threshold",
            "facility_name": "贵阳泰康乐综合医院",
            "service_name": "刮痧治疗",
            "threshold": 2,
            "limit": 2,
        }
        owner_payload = json.loads(
            owner_context.execute_tool("run_medical_audit", args)
        )

        properties = {
            (entity_api, property_api): property_id
            for entity_api, property_api, property_id in self.db.execute(
                select(
                    OntologyEntity.api_name,
                    OntologyProperty.api_name,
                    OntologyProperty.id,
                ).join(OntologyProperty, OntologyProperty.entity_id == OntologyEntity.id)
                .where(OntologyEntity.scenario_id == self.scenario.id)
            ).all()
        }
        for property_api in ("patient_id", "occurred_at"):
            self.db.add(
                AuthorizationGrant(
                    organization_id=self.organization.id,
                    user_id="user-medical-task",
                    resource_type="property",
                    resource_id=properties[("medical_charge_line", property_api)],
                    verb="read",
                    effect="deny",
                    created_by_user_id="user-medical-task",
                )
            )
        self.db.commit()

        context = AgentContext(self.db, agent, llm)
        payload = json.loads(context.execute_tool("run_medical_audit", args))
        self.assertTrue(payload["ok"])
        self.assertNotIn("affected_patient_count", payload["summary"])
        self.assertNotIn("patient_id", payload["records"][0])
        self.assertNotIn("occurred_at", payload["records"][0])
        self.assertNotIn(
            medical_audit_service.C_PATIENT_ID,
            payload["evidence"]["resolved_columns"],
        )
        self.assertNotIn(
            medical_audit_service.C_OCCURRED_AT,
            payload["evidence"]["resolved_columns"],
        )
        self.assertNotIn(
            medical_audit_service.C_PATIENT_ID,
            payload["lineage"]["property_refs"],
        )
        self.assertNotIn(
            medical_audit_service.C_OCCURRED_AT,
            payload["lineage"]["property_refs"],
        )
        self.assertFalse(
            context.authorize_historic_tool_result(
                "run_medical_audit",
                args,
                json.dumps(owner_payload, ensure_ascii=False),
            )
        )

        for property_api in ("total_amount", "charge_line_id"):
            with self.subTest(required_property=property_api):
                grant = AuthorizationGrant(
                    organization_id=self.organization.id,
                    user_id="user-medical-task",
                    resource_type="property",
                    resource_id=properties[("medical_charge_line", property_api)],
                    verb="read",
                    effect="deny",
                    created_by_user_id="user-medical-task",
                )
                self.db.add(grant)
                self.db.commit()
                denied_context = AgentContext(self.db, agent, llm)
                with patch.object(
                    medical_audit_service,
                    "_source_schema",
                    side_effect=AssertionError("ACL must be checked before querying"),
                ):
                    denied = json.loads(
                        denied_context.execute_tool("run_medical_audit", args)
                    )
                self.assertFalse(denied["ok"])
                self.assertEqual(denied["error"]["code"], "INVALID_QUERY")
                self.assertNotIn("records", denied)
                self.assertNotIn("summary", denied)
                self.db.delete(grant)
                self.db.commit()

    def test_medical_audit_history_uses_lineage_and_never_reexecutes_query(self) -> None:
        llm = LLMConfig(
            id="llm-medical-history",
            tenant_id=self.scenario.tenant_id,
            name="医保历史授权测试模型",
            capabilities=["chat", "tool"],
            enabled=True,
        )
        agent = Agent(
            id="agent-medical-history",
            tenant_id=self.scenario.tenant_id,
            name=upgrade_medical_audit.AGENT_NAME,
            scenario_id=self.scenario.id,
            llm_config_id=llm.id,
            data_source_ids=[],
            capability_scope={},
        )
        self.db.add_all([llm, agent])
        self.db.commit()
        upgrade_medical_audit.upgrade(
            self.db, agent_id=agent.id, source_id=self.source.id
        )
        self.db.refresh(agent)
        context = AgentContext(self.db, agent, llm)
        cases = [
            {
                "strategy": "charge_threshold",
                "facility_name": "贵阳泰康乐综合医院",
                "service_name": "刮痧治疗",
                "threshold": 2,
                "limit": 2,
            },
            {
                "strategy": "daily_overstay",
                "facility_name": "贵阳泰康乐综合医院",
                "service_names": ["Ⅰ级护理"],
            },
            {
                "strategy": "included_service_duplicate",
                "included_service": "电子结肠镜检查",
                "duplicate_service": "电子乙状结肠镜检查",
            },
            {
                "strategy": "limited_drug_duration",
                "drug_name": "天麻素注射液",
                "max_days": 14,
            },
        ]
        payloads = [
            json.loads(context.execute_tool("run_medical_audit", case))
            for case in cases
        ]
        args = cases[0]
        payload = payloads[0]

        with patch.object(
            medical_audit_service,
            "run_medical_audit",
            side_effect=AssertionError("historic authorization must not query"),
        ):
            for case, historic_payload in zip(cases, payloads, strict=True):
                self.assertTrue(
                    context.authorize_historic_tool_result(
                        "run_medical_audit",
                        case,
                        json.dumps(historic_payload, ensure_ascii=False),
                    ),
                    case["strategy"],
                )

        legacy = dict(payload)
        legacy.pop("lineage")
        self.assertFalse(
            context.authorize_historic_tool_result(
                "run_medical_audit", args, json.dumps(legacy, ensure_ascii=False)
            )
        )
        malformed = json.loads(json.dumps(payload, ensure_ascii=False))
        malformed["records"][0]["undeclared_field"] = "must stay hidden"
        self.assertFalse(
            context.authorize_historic_tool_result(
                "run_medical_audit",
                args,
                json.dumps(malformed, ensure_ascii=False),
            )
        )
        bound_source = next(
            source for source in context.data_sources if source.id == self.source.id
        )
        bound_source.config = {
            **dict(bound_source.config or {}),
            "history_test_revision": "changed",
        }
        self.db.commit()
        self.assertFalse(
            context.authorize_historic_tool_result(
                "run_medical_audit", args, json.dumps(payload, ensure_ascii=False)
            )
        )

    def test_upgrade_ownership_conflicts_abort_before_source_migration(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-conflict")
        user_entity = OntologyEntity(
            id="user-medical-facility",
            scenario_id=self.scenario.id,
            name="医疗机构",
            api_name="user_owned_facility",
            namespace=upgrade_medical_audit.NAMESPACE,
            lifecycle_status="active",
            description="用户维护的同名医疗机构对象",
        )
        self.db.add(user_entity)
        self.db.commit()

        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("ownership preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "对象类型.*不会覆盖"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        preserved = self.db.get(OntologyEntity, user_entity.id)
        self.assertEqual(preserved.description, "用户维护的同名医疗机构对象")
        self.assertEqual(preserved.lifecycle_status, "active")
        self.assertNotIn(upgrade_medical_audit.RECOVERY_MARKER, preserved.description)
        self.assertEqual(self.db.get(Agent, agent.id).system_prompt, "升级前提示词")

        self.db.delete(preserved)
        legacy_name_conflict = OntologyEntity(
            id="user-medical-rule-legacy-name",
            scenario_id=self.scenario.id,
            name="规则",
            api_name="user_owned_rule",
            namespace=upgrade_medical_audit.NAMESPACE,
            lifecycle_status="active",
            description="用户维护的规则对象，不是旧演示模型",
        )
        self.db.add(legacy_name_conflict)
        self.db.commit()
        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("legacy preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "旧对象名称.*不会退役"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        preserved_legacy = self.db.get(OntologyEntity, legacy_name_conflict.id)
        self.assertEqual(preserved_legacy.lifecycle_status, "active")
        self.assertEqual(preserved_legacy.description, "用户维护的规则对象，不是旧演示模型")

    def test_upgrade_rejects_unmarked_property_and_relation_conflicts(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-definition-conflict")
        entities, _, relations = self._seed_unmarked_v1_medical_ontology()
        entity_spec = upgrade_medical_audit.ENTITY_SPECS[0]
        entity = entities[entity_spec["name"]]
        entity.description = upgrade_medical_audit._marked_description(
            entity_spec["description"]
        )
        property_spec = entity_spec["properties"][0]
        prop = next(
            item for item in entity.properties if item.name == property_spec["name"]
        )
        prop.description = "用户修改过的同名属性"
        self.db.commit()

        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("property preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "属性.*不会覆盖"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(OntologyProperty, prop.id).description,
            "用户修改过的同名属性",
        )

        entity = self.db.get(OntologyEntity, entity.id)
        prop = self.db.get(OntologyProperty, prop.id)
        entity.description = entity_spec["description"]
        prop.description = property_spec.get("description", "")
        relation = self.db.get(OntologyRelation, relations[0].id)
        relation.relation_type = "1:1"
        self.db.commit()
        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("relation preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "关系类型.*不会覆盖"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(OntologyRelation, relation.id).relation_type,
            "1:1",
        )
        self.assertEqual(self.db.get(Agent, agent.id).system_prompt, "升级前提示词")

    def test_upgrade_mapping_conflict_aborts_before_source_migration(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-mapping-conflict")
        self.db.commit()
        upgrade_medical_audit.upgrade(
            self.db,
            agent_id=agent.id,
            scenario_id=self.scenario.id,
            source_id=self.source.id,
        )
        facility = self.db.scalar(select(OntologyEntity).where(
            OntologyEntity.scenario_id == self.scenario.id,
            OntologyEntity.api_name == "medical_facility",
        ))
        mapping = self.db.scalar(select(DataMapping).where(
            DataMapping.entity_id == facility.id,
            DataMapping.data_source_id == self.source.id,
        ))
        environment_status = dict(mapping.environment_status or {})
        environment_status.pop(upgrade_medical_audit.MAPPING_PROVENANCE_KEY, None)
        mapping.environment_status = environment_status
        mapping.column_map = {"医疗机构ID": "用户自定义列"}
        self.db.commit()

        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("mapping preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "数据映射是未标记的现有资源"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(DataMapping, mapping.id).column_map,
            {"医疗机构ID": "用户自定义列"},
        )

    def test_upgrade_rejects_target_entity_mapping_bound_to_another_source(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-cross-source-mapping")
        entities, _, _ = self._seed_unmarked_v1_medical_ontology()
        other_source_path = Path(self.temp.name) / "medical-audit-other.db"
        self._build_source(other_source_path)
        other_source = DataSource(
            id="source-medical-task-other-mapping",
            tenant_id=self.scenario.tenant_id,
            scenario_id=self.scenario.id,
            name="用户维护的医保副库",
            type="sqlite",
            config={"path": str(other_source_path)},
            status="ok",
        )
        cross_source_mapping = DataMapping(
            id="mapping-medical-user-cross-source",
            scenario_id=self.scenario.id,
            entity_id=entities["收费明细"].id,
            data_source_id=other_source.id,
            data_source_binding_key="user:medical:other-source",
            data_source_binding_ref={"adapter": "sqlite"},
            table_name="项目明细表",
            column_map={"收费明细ID": "记账流水号"},
            transform_rules={},
            status="ready",
            last_error="",
            environment_status={"dev": {"status": "ready", "last_error": ""}},
        )
        self.db.add_all([other_source, cross_source_mapping])
        self.db.commit()

        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("cross-source preflight must run first"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "指向其他数据源.*不会迁移或覆盖",
            ):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

        self.db.expire_all()
        preserved = self.db.get(DataMapping, cross_source_mapping.id)
        self.assertEqual(preserved.data_source_id, other_source.id)
        self.assertEqual(preserved.data_source_binding_key, "user:medical:other-source")
        self.assertEqual(preserved.column_map, {"收费明细ID": "记账流水号"})
        self.assertEqual(self.db.get(Agent, agent.id).system_prompt, "升级前提示词")
        with closing(sqlite3.connect(self.source_path)) as connection:
            created = set(connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
            ))
        self.assertTrue({
            ("医疗机构视图",),
            ("医保服务项目视图",),
            ("idx_medical_audit_charge_service",),
            ("idx_medical_audit_charge_encounter",),
            ("idx_medical_audit_encounter_id",),
        }.isdisjoint(created))

    def test_upgrade_relation_mapping_conflict_aborts_before_source_migration(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-relation-mapping-conflict")
        entities, _, relations = self._seed_unmarked_v1_medical_ontology()
        upgrade_medical_audit._ensure_domain_views(self.source)
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
        for mapping in mappings.values():
            environment_status = dict(mapping.environment_status or {})
            environment_status.pop(upgrade_medical_audit.MAPPING_PROVENANCE_KEY, None)
            mapping.environment_status = environment_status
        for relation, spec in zip(
            relations,
            upgrade_medical_audit.RELATION_SPECS,
            strict=True,
        ):
            relation.description = upgrade_medical_audit._relation_description(spec)
        target_relation = relations[1]
        binding = self.db.scalar(select(RelationDataMapping).where(
            RelationDataMapping.relation_id == target_relation.id
        ))
        binding.foreign_key_column = "用户自定义列"
        self.db.commit()

        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("relation mapping preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "数据映射定义与恢复包不一致"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(RelationDataMapping, binding.id).foreign_key_column,
            "用户自定义列",
        )
        self.assertEqual(self.db.get(Agent, agent.id).system_prompt, "升级前提示词")

    def test_upgrade_workflow_and_prompt_conflicts_abort_before_source_migration(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-complete-preflight")
        workflow = OntologyWorkflow(
            id=sorted(upgrade_medical_audit.LEGACY_WORKFLOW_IDS)[0],
            scenario_id=self.scenario.id,
            name="用户占用固定 ID 的工作流",
            description="用户定义",
            nodes=[{"id": "user", "type": "script", "data": {}}],
            status="active",
            enabled=True,
        )
        self.db.add(workflow)
        self.db.commit()
        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("workflow preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "不符合历史精确契约"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.delete(workflow)
        agent.system_prompt = upgrade_medical_audit.PROMPT_MARKER
        self.db.commit()
        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("prompt preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "提示词标记不完整"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

    def test_upgrade_source_object_conflict_aborts_before_ddl(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-source-conflict")
        self.db.commit()
        with closing(sqlite3.connect(self.source_path)) as connection:
            connection.execute(
                'CREATE TABLE "医疗机构视图" ("用户字段" TEXT)'
            )
            connection.commit()
        with patch.object(
            upgrade_medical_audit,
            "_ensure_domain_views",
            side_effect=AssertionError("source preflight must run first"),
        ):
            with self.assertRaisesRegex(RuntimeError, "不是恢复包可认领的领域视图"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

    def test_upgrade_rolls_back_platform_writes_after_late_failure(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-rollback")
        self.db.commit()
        with patch.object(
            upgrade_medical_audit,
            "_upsert_relations",
            side_effect=RuntimeError("forced late failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced late failure"):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.db.expire_all()
        self.assertEqual(
            self.db.scalar(select(OntologyEntity).where(
                OntologyEntity.scenario_id == self.scenario.id
            )),
            None,
        )
        self.assertEqual(
            self.db.scalar(select(DataMapping).where(
                DataMapping.scenario_id == self.scenario.id
            )),
            None,
        )
        self.assertEqual(self.db.get(Agent, agent.id).system_prompt, "升级前提示词")
        self.assertEqual(self.db.get(BusinessScenario, self.scenario.id).description, "")
        with closing(sqlite3.connect(self.source_path)) as connection:
            source_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertTrue({
            "医疗机构视图",
            "医保服务项目视图",
            "idx_medical_audit_charge_service",
            "idx_medical_audit_charge_encounter",
            "idx_medical_audit_encounter_id",
        }.isdisjoint(source_objects))

    def test_upgrade_compensates_committed_source_ddl_when_platform_commit_fails(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-commit-compensation")
        self.db.commit()
        with closing(sqlite3.connect(self.source_path)) as connection:
            plan = upgrade_medical_audit._domain_source_plan(connection)
            facility_select = plan["view_selects"]["医疗机构视图"]
            connection.execute(
                'CREATE VIEW "医疗机构视图" AS\n' + facility_select
            )
            connection.execute(
                'CREATE INDEX "user_preserved_service_index" '
                'ON "项目明细表" ("医保目录名称")'
            )
            connection.commit()
            source_before = list(connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('view', 'index') ORDER BY type, name"
            ))

        with patch.object(
            self.db,
            "commit",
            side_effect=RuntimeError("forced medical platform commit failure"),
        ):
            with self.assertRaisesRegex(
                RuntimeError, "forced medical platform commit failure"
            ):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

        self.db.expire_all()
        self.assertIsNone(self.db.scalar(select(OntologyEntity).where(
            OntologyEntity.scenario_id == self.scenario.id
        )))
        self.assertIsNone(self.db.scalar(select(DataMapping).where(
            DataMapping.scenario_id == self.scenario.id
        )))
        self.assertEqual(self.db.get(Agent, agent.id).system_prompt, "升级前提示词")
        self.assertEqual(self.db.get(BusinessScenario, self.scenario.id).description, "")
        with closing(sqlite3.connect(self.source_path)) as connection:
            source_after = list(connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('view', 'index') ORDER BY type, name"
            ))
        self.assertEqual(source_after, source_before)

    def test_upgrade_mutex_uses_postgres_table_and_resource_row_locks(self) -> None:
        statements: list[tuple[str, str]] = []

        class FakePostgresSession:
            @staticmethod
            def get_bind():
                return SimpleNamespace(
                    dialect=SimpleNamespace(name="postgresql")
                )

            @staticmethod
            def execute(statement, _params=None):
                statements.append(("execute", str(statement)))
                return SimpleNamespace(rowcount=1)

            @staticmethod
            def scalar(statement):
                statements.append(("scalar", str(statement)))
                return "locked"

        tenant_id = "tenant-medical-postgres-lock"
        dialect = upgrade_medical_audit._acquire_upgrade_mutex(
            FakePostgresSession(),
            SimpleNamespace(
                id="agent-medical-postgres-lock",
                tenant_id=tenant_id,
                scenario_id="scenario-medical-postgres-lock",
            ),
            SimpleNamespace(
                id="scenario-medical-postgres-lock",
                tenant_id=tenant_id,
            ),
            SimpleNamespace(
                id="source-medical-postgres-lock",
                tenant_id=tenant_id,
            ),
        )

        self.assertEqual(dialect, "postgresql")
        self.assertEqual(
            statements[0],
            (
                "execute",
                "LOCK TABLE data_mappings IN SHARE ROW EXCLUSIVE MODE",
            ),
        )
        row_lock_sql = [sql for kind, sql in statements if kind == "scalar"]
        self.assertEqual(len(row_lock_sql), 3)
        self.assertIn("FROM agents", row_lock_sql[0])
        self.assertIn("FROM business_scenarios", row_lock_sql[1])
        self.assertIn("FROM data_sources", row_lock_sql[2])
        self.assertTrue(all("FOR UPDATE" in sql for sql in row_lock_sql))

    def test_upgrade_mutex_rejects_unverified_platform_dialect(self) -> None:
        class FakeMysqlSession:
            @staticmethod
            def get_bind():
                return SimpleNamespace(dialect=SimpleNamespace(name="mysql"))

        tenant_id = "tenant-medical-mysql-lock"
        with self.assertRaisesRegex(
            RuntimeError,
            "不支持平台数据库方言.*仅 SQLite 与 PostgreSQL",
        ):
            upgrade_medical_audit._acquire_upgrade_mutex(
                FakeMysqlSession(),
                SimpleNamespace(
                    id="agent-medical-mysql-lock",
                    tenant_id=tenant_id,
                    scenario_id="scenario-medical-mysql-lock",
                ),
                SimpleNamespace(
                    id="scenario-medical-mysql-lock",
                    tenant_id=tenant_id,
                ),
                SimpleNamespace(
                    id="source-medical-mysql-lock",
                    tenant_id=tenant_id,
                ),
            )

    def test_platform_commit_verifier_distinguishes_absent_and_complete_contract(
        self,
    ) -> None:
        agent = self._add_upgrade_agent("agent-medical-commit-verifier")
        self.db.commit()
        self.assertEqual(
            upgrade_medical_audit._verify_platform_commit_after_error(
                self.db,
                agent_id=agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                attempt_id="attempt-that-was-never-written",
            ),
            upgrade_medical_audit.PLATFORM_COMMIT_NOT_COMMITTED,
        )

        result = upgrade_medical_audit.upgrade(
            self.db,
            agent_id=agent.id,
            scenario_id=self.scenario.id,
            source_id=self.source.id,
        )
        self.assertEqual(
            upgrade_medical_audit._verify_platform_commit_after_error(
                self.db,
                agent_id=agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
                attempt_id=result["attempt_id"],
            ),
            upgrade_medical_audit.PLATFORM_COMMIT_CONFIRMED,
        )

    def test_old_complete_contract_cannot_confirm_a_failed_repair_attempt(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-attempt-bound-verifier")
        self.db.commit()
        first = upgrade_medical_audit.upgrade(
            self.db,
            agent_id=agent.id,
            scenario_id=self.scenario.id,
            source_id=self.source.id,
        )
        self.db.expire_all()
        agent = self.db.get(Agent, agent.id)
        agent.max_tokens = 1
        self.db.commit()
        with closing(sqlite3.connect(self.source_path)) as connection:
            connection.execute('DROP VIEW "医疗机构视图"')
            connection.commit()

        with patch.object(
            self.db,
            "commit",
            side_effect=RuntimeError("repair commit failed before apply"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "repair commit failed before apply",
            ):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

        self.db.expire_all()
        self.assertEqual(self.db.get(Agent, agent.id).max_tokens, 1)
        mappings = list(self.db.scalars(
            select(DataMapping).where(
                DataMapping.scenario_id == self.scenario.id
            )
        ))
        self.assertTrue(mappings)
        self.assertEqual(
            {
                dict(mapping.environment_status or {}).get(
                    upgrade_medical_audit.MAPPING_ATTEMPT_KEY
                )
                for mapping in mappings
            },
            {first["attempt_id"]},
        )
        with closing(sqlite3.connect(self.source_path)) as connection:
            recreated_view = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'view' AND name = ?",
                ("医疗机构视图",),
            ).fetchone()
        self.assertIsNone(recreated_view)

    def test_upgrade_confirms_sqlite_commit_that_succeeded_before_raise(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-sqlite-commit-confirmed")
        self.db.commit()
        real_commit = self.db.commit

        def commit_then_lose_acknowledgement() -> None:
            real_commit()
            raise RuntimeError("lost sqlite commit acknowledgement")

        with patch.object(
            self.db,
            "commit",
            side_effect=commit_then_lose_acknowledgement,
        ):
            result = upgrade_medical_audit.upgrade(
                self.db,
                agent_id=agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
            )

        self.db.expire_all()
        mappings = list(self.db.scalars(
            select(DataMapping).where(
                DataMapping.scenario_id == self.scenario.id
            )
        ))
        self.assertEqual(len(mappings), len(upgrade_medical_audit.MAPPING_SPECS))
        self.assertEqual(
            {
                dict(mapping.environment_status or {}).get(
                    upgrade_medical_audit.MAPPING_ATTEMPT_KEY
                )
                for mapping in mappings
            },
            {result["attempt_id"]},
        )
        with closing(sqlite3.connect(self.source_path)) as connection:
            source_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertIn("医疗机构视图", source_objects)
        self.assertIn("医保服务项目视图", source_objects)

    def test_upgrade_preserves_source_ddl_after_committed_entity_identity_damage(
        self,
    ) -> None:
        agent = self._add_upgrade_agent("agent-medical-identity-damaged-commit")
        self.db.commit()
        real_commit = self.db.commit
        damaged_entity_id: list[str] = []

        def commit_then_damage_identity_and_lose_acknowledgement() -> None:
            real_commit()
            with Session(self.engine) as concurrent_db:
                entity = concurrent_db.scalar(select(OntologyEntity).where(
                    OntologyEntity.scenario_id == self.scenario.id,
                    OntologyEntity.api_name == "medical_facility",
                ))
                self.assertIsNotNone(entity)
                damaged_entity_id.append(str(entity.id))
                entity.name = "并发改名的医疗机构对象"
                entity.api_name = "concurrently_renamed_medical_facility"
                concurrent_db.commit()
            raise RuntimeError("lost acknowledgement after identity damage")

        with patch.object(
            self.db,
            "commit",
            side_effect=commit_then_damage_identity_and_lose_acknowledgement,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "lost acknowledgement after identity damage",
            ) as raised:
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

        self.assertTrue(any(
            "已保留本次新增的医保源库对象" in note
            for note in getattr(raised.exception, "__notes__", [])
        ))
        self.assertEqual(len(damaged_entity_id), 1)
        self.db.expire_all()
        damaged = self.db.get(OntologyEntity, damaged_entity_id[0])
        self.assertEqual(damaged.name, "并发改名的医疗机构对象")
        with closing(sqlite3.connect(self.source_path)) as connection:
            source_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertTrue({
            "医疗机构视图",
            "医保服务项目视图",
            "idx_medical_audit_charge_service",
            "idx_medical_audit_charge_encounter",
            "idx_medical_audit_encounter_id",
        }.issubset(source_objects))

    def test_upgrade_preserves_source_ddl_when_sqlite_commit_is_unknown(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-unknown-sqlite-commit")
        self.db.commit()
        with (
            patch.object(
                upgrade_medical_audit,
                "_verify_platform_commit_after_error",
                return_value=upgrade_medical_audit.PLATFORM_COMMIT_UNKNOWN,
            ) as verify_commit,
            patch.object(
                self.db,
                "commit",
                side_effect=RuntimeError("forced ambiguous sqlite commit"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "forced ambiguous sqlite commit",
            ) as raised:
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

        verify_commit.assert_called_once()
        verification_attempt = verify_commit.call_args.kwargs["attempt_id"]
        self.assertEqual(len(verification_attempt), 32)
        self.assertTrue(any(
            "已保留本次新增的医保源库对象" in note
            for note in getattr(raised.exception, "__notes__", [])
        ))
        with closing(sqlite3.connect(self.source_path)) as connection:
            retained_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertTrue({
            "医疗机构视图",
            "医保服务项目视图",
            "idx_medical_audit_charge_service",
            "idx_medical_audit_charge_encounter",
            "idx_medical_audit_encounter_id",
        }.issubset(retained_objects))

    def test_upgrade_preserves_source_ddl_when_postgres_commit_is_unknown(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-unknown-postgres-commit")
        self.db.commit()
        with (
            patch.object(
                upgrade_medical_audit,
                "_acquire_upgrade_mutex",
                return_value="postgresql",
            ),
            patch.object(
                upgrade_medical_audit,
                "_verify_platform_commit_after_error",
                return_value=upgrade_medical_audit.PLATFORM_COMMIT_UNKNOWN,
            ),
            patch.object(
                self.db,
                "commit",
                side_effect=RuntimeError("forced ambiguous postgres commit"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "forced ambiguous postgres commit",
            ) as raised:
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )
        self.assertTrue(any(
            "已保留本次新增的医保源库对象" in note
            for note in getattr(raised.exception, "__notes__", [])
        ))
        with closing(sqlite3.connect(self.source_path)) as connection:
            retained_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        expected_objects = {
            "医疗机构视图",
            "医保服务项目视图",
            "idx_medical_audit_charge_service",
            "idx_medical_audit_charge_encounter",
            "idx_medical_audit_encounter_id",
        }
        self.assertTrue(expected_objects.issubset(retained_objects))

        # Retained objects are additive and can be adopted by an idempotent
        # retry after the operator has confirmed the platform state.
        upgrade_medical_audit.upgrade(
            self.db,
            agent_id=agent.id,
            scenario_id=self.scenario.id,
            source_id=self.source.id,
        )
        target_entities = list(self.db.scalars(
            select(OntologyEntity).where(
                OntologyEntity.scenario_id == self.scenario.id,
                OntologyEntity.api_name.in_([
                    str(spec["api_name"])
                    for spec in upgrade_medical_audit.ENTITY_SPECS
                ]),
            )
        ))
        target_mappings = list(self.db.scalars(
            select(DataMapping).where(
                DataMapping.scenario_id == self.scenario.id,
                DataMapping.entity_id.in_([
                    entity.id for entity in target_entities
                ]),
            )
        ))
        self.assertEqual(
            len(target_mappings),
            len(upgrade_medical_audit.MAPPING_SPECS),
        )
        self.assertEqual(
            {mapping.data_source_id for mapping in target_mappings},
            {self.source.id},
        )

    def test_upgrade_treats_verified_postgres_commit_as_success(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-verified-postgres-commit")
        self.db.commit()
        with (
            patch.object(
                upgrade_medical_audit,
                "_acquire_upgrade_mutex",
                return_value="postgresql",
            ),
            patch.object(
                upgrade_medical_audit,
                "_verify_platform_commit_after_error",
                return_value=upgrade_medical_audit.PLATFORM_COMMIT_CONFIRMED,
            ) as verify_commit,
            patch.object(
                self.db,
                "commit",
                side_effect=RuntimeError("lost postgres commit acknowledgement"),
            ),
        ):
            result = upgrade_medical_audit.upgrade(
                self.db,
                agent_id=agent.id,
                scenario_id=self.scenario.id,
                source_id=self.source.id,
            )

        verify_commit.assert_called_once()
        self.assertEqual(result["scenario_id"], self.scenario.id)
        with closing(sqlite3.connect(self.source_path)) as connection:
            retained_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertIn("医疗机构视图", retained_objects)
        self.assertIn("医保服务项目视图", retained_objects)

    def test_upgrade_compensates_only_verified_uncommitted_postgres_ddl(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-uncommitted-postgres")
        self.db.commit()
        with (
            patch.object(
                upgrade_medical_audit,
                "_acquire_upgrade_mutex",
                return_value="postgresql",
            ),
            patch.object(
                upgrade_medical_audit,
                "_verify_platform_commit_after_error",
                return_value=(
                    upgrade_medical_audit.PLATFORM_COMMIT_NOT_COMMITTED
                ),
            ) as verify_commit,
            patch.object(
                self.db,
                "commit",
                side_effect=RuntimeError("postgres commit rejected before apply"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "postgres commit rejected before apply",
            ):
                upgrade_medical_audit.upgrade(
                    self.db,
                    agent_id=agent.id,
                    scenario_id=self.scenario.id,
                    source_id=self.source.id,
                )

        verify_commit.assert_called_once()
        with closing(sqlite3.connect(self.source_path)) as connection:
            source_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertTrue({
            "医疗机构视图",
            "医保服务项目视图",
            "idx_medical_audit_charge_service",
            "idx_medical_audit_charge_encounter",
            "idx_medical_audit_encounter_id",
        }.isdisjoint(source_objects))

    def test_upgrade_serializes_compensation_before_another_worker_adopts_source_objects(
        self,
    ) -> None:
        platform_path = Path(self.temp.name) / "medical-platform-concurrent.db"
        source_path = Path(self.temp.name) / "medical-source-concurrent.db"
        self._build_source(source_path)
        engine = create_engine(
            f"sqlite:///{platform_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 15},
        )
        self._concurrent_upgrade_engine = engine
        Base.metadata.create_all(engine)
        tenant_id = "tenant-medical-concurrent"
        scenario_id = "scenario-medical-concurrent"
        source_id = "source-medical-concurrent"
        agent_id = "agent-medical-concurrent"
        with Session(engine) as seed_db:
            seed_db.add_all([
                Tenant(id=tenant_id, name="医保并发升级租户"),
                User(
                    id="user-medical-concurrent",
                    tenant_id=tenant_id,
                    email="medical-concurrent@example.test",
                    password_hash="test-only",
                    status="active",
                ),
                BusinessScenario(
                    id=scenario_id,
                    tenant_id=tenant_id,
                    name=upgrade_medical_audit.SCENARIO_NAME,
                    namespace=upgrade_medical_audit.NAMESPACE,
                    status="active",
                ),
                DataSource(
                    id=source_id,
                    tenant_id=tenant_id,
                    scenario_id=scenario_id,
                    name="医保并发升级业务库",
                    type="sqlite",
                    config={"path": str(source_path)},
                    status="ok",
                ),
                Agent(
                    id=agent_id,
                    tenant_id=tenant_id,
                    name=upgrade_medical_audit.AGENT_NAME,
                    scenario_id=scenario_id,
                    system_prompt="升级前提示词",
                    data_source_ids=[source_id],
                    capability_scope={},
                ),
            ])
            seed_db.commit()

        first_reached_commit = threading.Event()
        allow_first_failure = threading.Event()
        second_started = threading.Event()
        second_finished = threading.Event()
        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []

        def first_worker() -> None:
            with Session(engine) as worker_db:
                def fail_platform_commit() -> None:
                    first_reached_commit.set()
                    if not allow_first_failure.wait(timeout=10):
                        raise TimeoutError("test did not release first worker")
                    raise RuntimeError("forced concurrent platform commit failure")

                try:
                    with patch.object(
                        worker_db,
                        "commit",
                        side_effect=fail_platform_commit,
                    ):
                        upgrade_medical_audit.upgrade(
                            worker_db,
                            agent_id=agent_id,
                            scenario_id=scenario_id,
                            source_id=source_id,
                        )
                except BaseException as exc:  # captured for the main test thread
                    first_errors.append(exc)

        def second_worker() -> None:
            if not first_reached_commit.wait(timeout=10):
                second_errors.append(
                    TimeoutError("first worker did not reach platform commit")
                )
                second_finished.set()
                return
            second_started.set()
            try:
                with Session(engine) as worker_db:
                    upgrade_medical_audit.upgrade(
                        worker_db,
                        agent_id=agent_id,
                        scenario_id=scenario_id,
                        source_id=source_id,
                    )
            except BaseException as exc:  # captured for the main test thread
                second_errors.append(exc)
            finally:
                second_finished.set()

        first_thread = threading.Thread(target=first_worker, daemon=True)
        second_thread = threading.Thread(target=second_worker, daemon=True)
        first_thread.start()
        self.assertTrue(first_reached_commit.wait(timeout=10))
        second_thread.start()
        self.assertTrue(second_started.wait(timeout=10))
        time.sleep(0.2)
        self.assertFalse(
            second_finished.is_set(),
            "another worker must not adopt source objects before compensation",
        )
        allow_first_failure.set()
        first_thread.join(timeout=15)
        second_thread.join(timeout=15)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertEqual(len(first_errors), 1)
        self.assertIsInstance(first_errors[0], RuntimeError)
        self.assertIn(
            "forced concurrent platform commit failure",
            str(first_errors[0]),
        )
        self.assertEqual(second_errors, [])

        with Session(engine) as verify_db:
            entities = list(verify_db.scalars(
                select(OntologyEntity).where(
                    OntologyEntity.scenario_id == scenario_id,
                    OntologyEntity.api_name.in_([
                        str(spec["api_name"])
                        for spec in upgrade_medical_audit.ENTITY_SPECS
                    ]),
                )
            ))
            self.assertEqual(len(entities), len(upgrade_medical_audit.ENTITY_SPECS))
            mappings = list(verify_db.scalars(
                select(DataMapping).where(
                    DataMapping.scenario_id == scenario_id,
                    DataMapping.entity_id.in_([entity.id for entity in entities]),
                )
            ))
            self.assertEqual(len(mappings), len(upgrade_medical_audit.MAPPING_SPECS))
            mapped_entity_names = {
                str(spec["entity"])
                for spec in upgrade_medical_audit.MAPPING_SPECS
            }
            self.assertEqual(
                {mapping.entity_id for mapping in mappings},
                {
                    entity.id
                    for entity in entities
                    if entity.name in mapped_entity_names
                },
            )
            self.assertEqual(
                {mapping.data_source_id for mapping in mappings},
                {source_id},
            )

        with closing(sqlite3.connect(source_path)) as connection:
            source_objects = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('view', 'index')"
                )
            }
        self.assertTrue({
            "医疗机构视图",
            "医保服务项目视图",
            "idx_medical_audit_charge_service",
            "idx_medical_audit_charge_encounter",
            "idx_medical_audit_encounter_id",
        }.issubset(source_objects))
        engine.dispose()

    def test_upgrade_retires_only_explicit_recovery_owned_workflows(self) -> None:
        agent = self._add_upgrade_agent("agent-medical-workflows")
        script_workflow = OntologyWorkflow(
            id="user-script-workflow",
            scenario_id=self.scenario.id,
            name="用户脚本工作流",
            description="用户自建脚本流程",
            nodes=[{"id": "script", "type": "script", "data": {}}],
            status="active",
            enabled=True,
        )
        http_workflow = OntologyWorkflow(
            id="user-http-workflow",
            scenario_id=self.scenario.id,
            name="用户 HTTP 工作流",
            description="用户自建 HTTP 流程",
            nodes=[{"id": "http", "type": "http", "data": {}}],
            status="active",
            enabled=True,
        )
        missing_reference_workflow = OntologyWorkflow(
            id="user-missing-ref-workflow",
            scenario_id=self.scenario.id,
            name="用户待修复引用工作流",
            description="用户流程仍需保留",
            nodes=[
                {
                    "id": "action",
                    "type": "action",
                    "data": {"action_id": "missing-user-action"},
                }
            ],
            status="active",
            enabled=True,
        )
        legacy_workflow = OntologyWorkflow(
            id=sorted(upgrade_medical_audit.LEGACY_WORKFLOW_IDS)[0],
            scenario_id=self.scenario.id,
            name="旧医保演示工作流",
            description=(
                "旧演示流程\n" + upgrade_medical_audit.WORKFLOW_RETIREMENT_MARKER
            ),
            nodes=[{"id": "legacy", "type": "script", "data": {}}],
            status="active",
            enabled=True,
        )
        custom_workflows = (
            script_workflow,
            http_workflow,
            missing_reference_workflow,
        )
        self.db.add_all([agent, *custom_workflows, legacy_workflow])
        self.db.commit()

        first = upgrade_medical_audit.upgrade(
            self.db,
            agent_id=agent.id,
            scenario_id=self.scenario.id,
            source_id=self.source.id,
        )
        second = upgrade_medical_audit.upgrade(
            self.db,
            agent_id=agent.id,
            scenario_id=self.scenario.id,
            source_id=self.source.id,
        )
        self.assertEqual(first["retired_workflows"], 1)
        self.assertEqual(second["retired_workflows"], 0)
        self.db.expire_all()
        for workflow in custom_workflows:
            preserved = self.db.get(OntologyWorkflow, workflow.id)
            self.assertTrue(preserved.enabled)
            self.assertEqual(preserved.status, "active")
            self.assertNotIn(
                upgrade_medical_audit.WORKFLOW_RETIREMENT_MARKER,
                preserved.description,
            )
        retired = self.db.get(OntologyWorkflow, legacy_workflow.id)
        self.assertFalse(retired.enabled)
        self.assertEqual(retired.status, "disabled")
        self.assertEqual(
            retired.description.splitlines().count(
                upgrade_medical_audit.WORKFLOW_RETIREMENT_MARKER
            ),
            1,
        )

    def test_upgrade_resolution_rejects_ambiguous_agents_and_sources(self) -> None:
        first_agent = Agent(
            id="agent-medical-resolution-a",
            tenant_id=self.scenario.tenant_id,
            name=upgrade_medical_audit.AGENT_NAME,
            scenario_id=self.scenario.id,
            system_prompt="医保审计助手 A",
            data_source_ids=[self.source.id],
            capability_scope={},
        )
        second_agent = Agent(
            id="agent-medical-resolution-b",
            tenant_id=self.scenario.tenant_id,
            name=upgrade_medical_audit.AGENT_NAME,
            scenario_id=self.scenario.id,
            system_prompt="医保审计助手 B",
            data_source_ids=[self.source.id],
            capability_scope={},
        )
        second_source = DataSource(
            id="source-medical-task-second",
            tenant_id=self.scenario.tenant_id,
            scenario_id=self.scenario.id,
            name="医保审计业务库副本绑定",
            type="sqlite",
            config={"path": str(self.source_path)},
            status="ok",
        )
        wrong_agent = Agent(
            id="agent-bookkeeping-resolution",
            tenant_id=self.scenario.tenant_id,
            name="代理记账助手",
            scenario_id=self.scenario.id,
            system_prompt="错误业务 Agent",
            data_source_ids=[self.source.id],
            capability_scope={},
        )
        wrong_business_scenario = BusinessScenario(
            id="scenario-bookkeeping-resolution",
            tenant_id=self.scenario.tenant_id,
            name="代理记账",
            namespace="bookkeeping_audit",
            status="active",
        )
        other_medical_scenario = BusinessScenario(
            id="scenario-medical-resolution-other",
            tenant_id=self.scenario.tenant_id,
            name=upgrade_medical_audit.SCENARIO_NAME,
            namespace=upgrade_medical_audit.NAMESPACE,
            status="active",
        )
        wrong_scenario_source = DataSource(
            id="source-bookkeeping-resolution",
            tenant_id=self.scenario.tenant_id,
            scenario_id=wrong_business_scenario.id,
            name="代理记账数据源",
            type="sqlite",
            config={"path": str(self.source_path)},
            status="ok",
        )
        self.db.add_all([
            first_agent,
            second_agent,
            second_source,
            wrong_agent,
            wrong_business_scenario,
            other_medical_scenario,
            wrong_scenario_source,
        ])
        self.db.commit()

        with self.assertRaisesRegex(RuntimeError, "显式传入 agent_id"):
            upgrade_medical_audit._find_agent_and_scenario(self.db)
        selected_agent, selected_scenario = (
            upgrade_medical_audit._find_agent_and_scenario(
                self.db,
                agent_id=first_agent.id,
            )
        )
        self.assertEqual(selected_agent.id, first_agent.id)
        self.assertEqual(selected_scenario.id, self.scenario.id)
        with self.assertRaisesRegex(RuntimeError, "不是.*医保违规审计助手"):
            upgrade_medical_audit._find_agent_and_scenario(
                self.db,
                agent_id=wrong_agent.id,
                scenario_id=self.scenario.id,
            )
        with self.assertRaisesRegex(RuntimeError, "不是医保违规审计场景"):
            upgrade_medical_audit._find_agent_and_scenario(
                self.db,
                agent_id=first_agent.id,
                scenario_id=wrong_business_scenario.id,
            )
        with self.assertRaisesRegex(RuntimeError, "Agent 与业务场景不匹配"):
            upgrade_medical_audit._find_agent_and_scenario(
                self.db,
                agent_id=first_agent.id,
                scenario_id=other_medical_scenario.id,
            )

        with self.assertRaisesRegex(RuntimeError, "显式传入 source_id"):
            upgrade_medical_audit._sqlite_source(self.db, self.scenario)
        selected_source = upgrade_medical_audit._sqlite_source(
            self.db,
            self.scenario,
            source_id=self.source.id,
        )
        self.assertEqual(selected_source.id, self.source.id)
        with self.assertRaisesRegex(RuntimeError, "数据源与业务场景不匹配"):
            upgrade_medical_audit._sqlite_source(
                self.db,
                self.scenario,
                source_id=wrong_scenario_source.id,
            )


class MedicalAuditRealDataBaselineTests(unittest.TestCase):
    source_path = Path(__file__).resolve().parents[1] / "data" / "yibao_audit.db"

    @staticmethod
    def _explicit_contract(source):
        resolved = upgrade_medical_audit._resolved_mapping_contracts(
            source, read_only=True
        )
        entities: dict[str, SimpleNamespace] = {}
        mappings: list[SimpleNamespace] = []
        for logical_name, entity_name in (
            ("encounter", "就诊"),
            ("charge", "收费明细"),
        ):
            spec = next(
                item
                for item in upgrade_medical_audit.ENTITY_SPECS
                if item["name"] == entity_name
            )
            entity_id = f"real-{logical_name}-entity"
            entity = SimpleNamespace(
                id=entity_id,
                api_name=spec["api_name"],
                properties=[
                    SimpleNamespace(name=item["name"], api_name=item["api_name"])
                    for item in spec["properties"]
                ],
            )
            mapping = resolved[entity_name]
            entities[entity_id] = entity
            mappings.append(SimpleNamespace(
                id=f"real-{logical_name}-mapping",
                entity_id=entity_id,
                data_source_id=source.id,
                data_source_binding_key="test:medical-baseline:sqlite",
                data_source_binding_ref={"adapter": "sqlite"},
                table_name=mapping["table_name"],
                column_map=mapping["column_map"],
                transform_rules=mapping["transform_rules"],
            ))
        definition = SimpleNamespace(
            source="live",
            environment="dev",
            definition_hash="0" * 64,
            snapshot_id=None,
            release_id=None,
            entities=entities,
        )
        return medical_audit_service.resolve_mapping_contract(
            [source], mappings, definition=definition
        )

    @unittest.skipUnless(source_path.is_file(), "本地未提供只读医保示例库")
    def test_real_demo_facility_scope_lookup_and_truth_guard(self) -> None:
        source = SimpleNamespace(
            id="real-medical-facility-scope",
            name="医保真实示例库",
            type="sqlite",
            config={"path": str(self.source_path)},
        )
        contract = self._explicit_contract(source)
        policy = medical_audit_service.access_policy(
            [medical_audit_service.C_FACILITY_NAME]
        )
        service_station = (
            "贵阳市观山湖区长岭街道金融城社区卫生服务站"
        )
        shorter_service_station = (
            "观山湖区长岭街道金融城社区卫生服务站"
        )
        dental_clinic = (
            "贵阳市观山湖区信义口腔门诊部有限公司世纪城口腔门诊部"
        )
        station_request = (
            f"请审计{service_station}和{shorter_service_station}"
            "刮痧治疗收费大于两次的违规记录"
        )
        station_matches = medical_audit_service.find_facility_names_in_text(
            contract,
            station_request,
            property_access=policy,
        )
        self.assertEqual(
            station_matches,
            [service_station, shorter_service_station],
        )
        dental_request = (
            f"请审计{dental_clinic}刮痧治疗收费大于两次的违规记录"
        )
        dental_matches = medical_audit_service.find_facility_names_in_text(
            contract,
            dental_request,
            property_access=policy,
        )
        self.assertEqual(dental_matches, [dental_clinic])

        with self.assertRaises(medical_audit_service.MedicalAuditError) as denied:
            medical_audit_service.find_facility_names_in_text(
                contract,
                f"请审计{service_station}的违规记录",
                property_access=medical_audit_service.access_policy([]),
            )
        self.assertEqual(denied.exception.code, "INVALID_QUERY")

        def outcome(facility_name: str | None) -> list[dict[str, object]]:
            arguments: dict[str, object] = {
                "strategy": "charge_threshold",
                "service_name": "刮痧治疗",
                "threshold": 2,
            }
            if facility_name is not None:
                arguments["facility_name"] = facility_name
            return [{
                "name": "run_medical_audit",
                "arguments": arguments,
                "result": json.dumps({
                    "ok": True,
                    "audit_version": "medical-audit-v1",
                    "strategy": "charge_threshold",
                    "summary": {
                        "violation_count": 1,
                        "violation_amount": 10.0,
                    },
                    "records": [{"charge_line_id": "real-line-1"}],
                    "row_count": 1,
                    "offset": 0,
                    "limit": 10,
                    "truncated": False,
                    "next_offset": None,
                    "evidence": {
                        "source_id": source.id,
                        "parameters": {
                            "facility_name": facility_name,
                            "service_name": "刮痧治疗",
                            "threshold": 2,
                        },
                    },
                }, ensure_ascii=False),
            }]

        station_single_result = agent_engine._truthful_final_content(
            "审计完成。",
            user_message=station_request,
            tool_outcomes=outcome(service_station),
            controlled_medical_audit=True,
            authoritative_medical_facilities=station_matches,
            medical_facility_lookup_succeeded=True,
        )
        dental_matched = agent_engine._truthful_final_content(
            "审计完成。",
            user_message=dental_request,
            tool_outcomes=outcome(dental_clinic),
            controlled_medical_audit=True,
            authoritative_medical_facilities=dental_matches,
            medical_facility_lookup_succeeded=True,
        )
        self.assertIn("未形成可验证审计结论", station_single_result)
        self.assertNotIn("医保确定性汇总", station_single_result)
        self.assertIn("医保确定性汇总", dental_matched)
        self.assertNotIn("未形成可验证审计结论", dental_matched)

    @unittest.skipUnless(source_path.is_file(), "本地未提供只读医保示例库")
    def test_real_demo_database_baselines_and_latency(self) -> None:
        source = SimpleNamespace(
            id="real-medical-baseline",
            name="医保真实示例库",
            type="sqlite",
            config={"path": str(self.source_path)},
        )
        contract = self._explicit_contract(source)
        cases = [
            (
                {
                    "strategy": "charge_threshold",
                    "facility_name": "贵阳泰康乐综合医院",
                    "service_name": "刮痧治疗",
                    "threshold": 2,
                    "limit": 20,
                },
                10,
                379.08,
            ),
            (
                {
                    "strategy": "daily_overstay",
                    "service_names": [
                        "诊查费",
                        "特殊疾病护理",
                        "Ⅰ级护理",
                        "Ⅱ级护理",
                        "Ⅲ级护理",
                        "精神病护理",
                    ],
                },
                12,
                195,
            ),
            (
                {
                    "strategy": "included_service_duplicate",
                    "included_service": "电子结肠镜检查",
                    "duplicate_service": "电子乙状结肠镜检查",
                },
                20,
                955.85,
            ),
            (
                {
                    "strategy": "limited_drug_duration",
                    "drug_name": "天麻素注射液",
                    "max_days": 14,
                },
                0,
                0,
            ),
        ]
        for args, expected_count, expected_amount in cases:
            with self.subTest(strategy=args["strategy"]):
                started = time.perf_counter()
                result = medical_audit_service.run_medical_audit(contract, args)
                elapsed = time.perf_counter() - started
                self.assertEqual(result["summary"]["violation_count"], expected_count)
                self.assertAlmostEqual(
                    result["summary"]["violation_amount"], expected_amount, places=2
                )
                self.assertLess(elapsed, 10, f"{args['strategy']} took {elapsed:.2f}s")


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
