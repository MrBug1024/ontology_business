"""Idempotently upgrade the demo medical-audit scenario to a domain ontology.

The original demo modeled upload mechanics (tables/fields/business-data) and
then labelled raw charge rows as already-confirmed violations.  This upgrade is
additive: it keeps historic objects intact, adds business-facing object/link
types, binds them to governed SQLite views/tables, and retires only invalid demo
workflows.  It can be run repeatedly against ``backend/data/platform.db``.

Run from ``backend``::

    python examples/upgrade_medical_audit.py
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    Agent,
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
    RelationDataMapping,
)


AGENT_NAME = "医保违规审计助手"
LEGACY_ENTITY_NAMES = {
    "业务数据",
    "表格",
    "字段",
    "规则",
    "违规记录",
    "药品",
    "知识库请求",
}
PROMPT_MARKER = "## 医保收费审计执行约定（领域本体版）"


PROPERTY = dict[str, Any]


ENTITY_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "医疗机构",
        "api_name": "medical_facility",
        "description": "提供医保结算服务的定点医疗机构；机构编码是稳定身份，机构名称用于展示。",
        "icon": "building",
        "color": "#2563eb",
        "properties": (
            {"name": "医疗机构ID", "api_name": "facility_id", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "医疗机构名称", "api_name": "facility_name", "data_type": "string", "is_title": True, "is_required": True},
            {"name": "医院等级", "api_name": "hospital_level", "data_type": "string"},
            {"name": "医保区划", "api_name": "insurance_region", "data_type": "string"},
        ),
    },
    {
        "name": "就诊",
        "api_name": "medical_encounter",
        "description": "一次门诊或住院就诊，是收费频次、诊断和患者信息的业务归集边界。",
        "icon": "calendar",
        "color": "#0891b2",
        "properties": (
            {"name": "就诊ID", "api_name": "encounter_id", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "就诊凭证编号", "api_name": "encounter_credential_no", "data_type": "string", "is_title": True},
            {"name": "医疗机构ID", "api_name": "facility_id", "data_type": "string", "is_required": True},
            {"name": "医疗机构名称", "api_name": "facility_name", "data_type": "string", "is_required": True},
            {"name": "患者ID", "api_name": "patient_id", "data_type": "string", "is_sensitive": True},
            {"name": "患者姓名", "api_name": "patient_name", "data_type": "string", "is_sensitive": True},
            {"name": "开始时间", "api_name": "started_at", "data_type": "datetime"},
            {"name": "结束时间", "api_name": "ended_at", "data_type": "datetime"},
            {"name": "医疗类别", "api_name": "care_type", "data_type": "string"},
            {"name": "诊断名称", "api_name": "diagnosis_name", "data_type": "string"},
        ),
    },
    {
        "name": "收费明细",
        "api_name": "medical_charge_line",
        "description": "原始收费事实；审计条件应在此对象的项目、数量、单价和金额上判定，不能预先标记为违规。",
        "icon": "receipt",
        "color": "#7c3aed",
        "properties": (
            {"name": "收费明细ID", "api_name": "charge_line_id", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "服务项目名称", "api_name": "service_name", "data_type": "string", "is_title": True, "is_required": True},
            {"name": "就诊ID", "api_name": "encounter_id", "data_type": "string", "is_required": True},
            {"name": "医疗机构ID", "api_name": "facility_id", "data_type": "string"},
            {"name": "医疗机构名称", "api_name": "facility_name", "data_type": "string", "is_required": True},
            {"name": "患者ID", "api_name": "patient_id", "data_type": "string", "is_sensitive": True},
            {"name": "服务项目编码", "api_name": "service_code", "data_type": "string"},
            {"name": "收费数量", "api_name": "quantity", "data_type": "number", "description": "本条收费明细计费的服务次数或数量。"},
            {"name": "单价", "api_name": "unit_price", "data_type": "number"},
            {"name": "收费金额", "api_name": "total_amount", "data_type": "number"},
            {"name": "发生时间", "api_name": "occurred_at", "data_type": "datetime"},
            {"name": "收费项目类别", "api_name": "charge_category", "data_type": "string"},
            {"name": "开单科室", "api_name": "ordering_department", "data_type": "string"},
            {"name": "开单医师", "api_name": "ordering_doctor", "data_type": "string", "is_sensitive": True},
        ),
    },
    {
        "name": "医保服务项目",
        "api_name": "medical_service",
        "description": "医保目录中的药品、诊疗或服务项目，和具体收费事实分离。",
        "icon": "catalog",
        "color": "#059669",
        "properties": (
            {"name": "服务项目编码", "api_name": "service_code", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "服务项目名称", "api_name": "service_name", "data_type": "string", "is_title": True, "is_required": True},
            {"name": "目录类别", "api_name": "catalog_category", "data_type": "string"},
            {"name": "收费项目类别", "api_name": "charge_category", "data_type": "string"},
            {"name": "规格", "api_name": "specification", "data_type": "string"},
            {"name": "参考单价", "api_name": "reference_unit_price", "data_type": "number"},
        ),
    },
    {
        "name": "审计规则",
        "api_name": "medical_audit_rule",
        "description": "可复用的监管政策和审计规则。用户在对话中给出的明确阈值也是本次任务条件，不要求预先存在此对象。",
        "icon": "shield-check",
        "color": "#d97706",
        "properties": (
            {"name": "规则ID", "api_name": "rule_id", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "规则名称", "api_name": "rule_name", "data_type": "string", "is_title": True, "is_required": True},
            {"name": "所属领域", "api_name": "domain", "data_type": "string"},
            {"name": "政策依据", "api_name": "policy_basis", "data_type": "text"},
            {"name": "违规类型", "api_name": "violation_type", "data_type": "string"},
            {"name": "参考示例", "api_name": "reference_example", "data_type": "text"},
            {"name": "首次进入年份", "api_name": "first_listed_year", "data_type": "string"},
            {"name": "用途", "api_name": "purpose", "data_type": "string"},
        ),
    },
    {
        "name": "审计发现",
        "api_name": "medical_audit_finding",
        "description": "审计执行后形成的发现对象；与原始收费事实、规则依据和处理状态分别建模。",
        "icon": "alert-triangle",
        "color": "#dc2626",
        "state_property": "处理状态",
        "properties": (
            {"name": "发现ID", "api_name": "finding_id", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "发现标题", "api_name": "finding_title", "data_type": "string", "is_title": True, "is_required": True},
            {"name": "规则ID", "api_name": "rule_id", "data_type": "string"},
            {"name": "收费明细ID", "api_name": "charge_line_id", "data_type": "string"},
            {"name": "违规原因", "api_name": "reason", "data_type": "text"},
            {"name": "涉及金额", "api_name": "amount", "data_type": "number"},
            {"name": "严重程度", "api_name": "severity", "data_type": "string", "is_enum": True, "enum_values": ["提示", "一般", "严重"]},
            {"name": "处理状态", "api_name": "status", "data_type": "string", "is_enum": True, "enum_values": ["待复核", "已确认", "已排除", "已整改"]},
            {"name": "发现时间", "api_name": "found_at", "data_type": "datetime"},
        ),
    },
)


MAPPING_SPECS: tuple[dict[str, Any], ...] = (
    {
        "entity": "医疗机构",
        "table": "医疗机构视图",
        "columns": {
            "医疗机构ID": "定点医药机构编号",
            "医疗机构名称": "定点医药机构名称",
            "医院等级": "医院等级",
            "医保区划": "定点归属医保区划",
        },
    },
    {
        "entity": "就诊",
        "table": "就诊表",
        "columns": {
            "就诊ID": "就诊ID",
            "就诊凭证编号": "就诊凭证编号",
            "医疗机构ID": "定点医药机构编号",
            "医疗机构名称": "定点医药机构名称",
            "患者ID": "人员编号",
            "患者姓名": "人员姓名",
            "开始时间": "开始时间",
            "结束时间": "结束时间",
            "医疗类别": "医疗类别",
            "诊断名称": "住院主诊断名称",
        },
    },
    {
        "entity": "收费明细",
        "table": "项目明细表",
        "columns": {
            "收费明细ID": "记账流水号",
            "服务项目名称": "医保目录名称",
            "就诊ID": "就诊ID",
            "医疗机构ID": "定点医药机构编号",
            "医疗机构名称": "定点医药机构名称",
            "患者ID": "人员编号",
            "服务项目编码": "医保目录编码",
            "收费数量": "数量",
            "单价": "单价",
            "收费金额": "明细项目费用总额",
            "发生时间": "费用发生时间",
            "收费项目类别": "医疗收费项目类别",
            "开单科室": "开单科室名称",
            "开单医师": "开单医师姓名",
        },
        "transforms": {"收费明细ID": [{"op": "to_string"}]},
    },
    {
        "entity": "医保服务项目",
        "table": "医保服务项目视图",
        "columns": {
            "服务项目编码": "医保目录编码",
            "服务项目名称": "医保目录名称",
            "目录类别": "目录类别",
            "收费项目类别": "医疗收费项目类别",
            "规格": "规格",
            "参考单价": "参考单价",
        },
    },
    {
        "entity": "审计规则",
        "table": "规则表",
        "columns": {
            "规则ID": "序号",
            "规则名称": "国家问题清单",
            "所属领域": "所属领域",
            "政策依据": "有关依据",
            "违规类型": "违规类型",
            "参考示例": "国家违规参考示例",
            "首次进入年份": "首次进入问题清单年份",
            "用途": "用途",
        },
        "transforms": {"规则ID": [{"op": "to_string"}]},
    },
)


RELATION_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "机构提供就诊",
        "api_name": "facility_encounters",
        "source": "医疗机构",
        "target": "就诊",
        "relation_type": "1:N",
        "source_display_name": "就诊记录",
        "source_api_name": "encounters",
        "target_display_name": "就诊机构",
        "target_api_name": "facility",
        "mapping_mode": "target_fk",
        "foreign_key_column": "定点医药机构编号",
    },
    {
        "name": "就诊包含收费明细",
        "api_name": "encounter_charge_lines",
        "source": "就诊",
        "target": "收费明细",
        "relation_type": "1:N",
        "source_display_name": "收费明细",
        "source_api_name": "charge_lines",
        "target_display_name": "所属就诊",
        "target_api_name": "encounter",
        "mapping_mode": "target_fk",
        "foreign_key_column": "就诊ID",
    },
    {
        "name": "收费明细对应服务项目",
        "api_name": "charge_line_service",
        "source": "收费明细",
        "target": "医保服务项目",
        "relation_type": "N:1",
        "source_display_name": "服务项目",
        "source_api_name": "service",
        "target_display_name": "收费明细",
        "target_api_name": "charge_lines",
        "mapping_mode": "source_fk",
        "foreign_key_column": "医保目录编码",
    },
    {
        "name": "规则判定审计发现",
        "api_name": "rule_findings",
        "source": "审计规则",
        "target": "审计发现",
        "relation_type": "1:N",
        "source_display_name": "审计发现",
        "source_api_name": "findings",
        "target_display_name": "依据规则",
        "target_api_name": "rule",
    },
    {
        "name": "审计发现涉及收费明细",
        "api_name": "finding_charge_line",
        "source": "审计发现",
        "target": "收费明细",
        "relation_type": "N:1",
        "source_display_name": "涉及收费明细",
        "source_api_name": "charge_line",
        "target_display_name": "审计发现",
        "target_api_name": "findings",
    },
)


PROMPT_APPENDIX = f"""

{PROMPT_MARKER}
- 先调用 `list_ontology_model`，再用 `query_business_data` 按业务对象和属性查询；不要生成 SQL，也不要要求用户提供内部 UUID。
- 用户已明确给出机构、项目、数量阈值等条件时，这些条件就是本次任务的临时审计规则。规则库只用于补充政策依据；没有同名持久化规则不能阻塞审计。
- “某服务收费大于 N 次/数量”默认按 `收费明细.收费数量 > N` 判断；只有用户明确说“同一就诊出现 N 条以上”时，才按就诊分组后计算记录条数。
- 查询结果为 0 行是成功结果，必须明确回答“本次未发现符合条件的违规明细”，不能回答缺少规则、缺少 ID 或无法审计。
- 命中时返回全部可用明细（受平台分页上限约束时说明分页），至少包括收费明细ID、就诊ID、机构、服务项目、收费数量、单价、收费金额、发生时间，并汇总条数和金额。
- 示例验收口径：贵阳泰康乐综合医院 + 服务项目名称等于“刮痧治疗” + 收费数量大于 2；不要把“中医刮痧”或数量等于 1 混入结果。
""".strip()


def _find_agent_and_scenario(db) -> tuple[Agent, BusinessScenario]:
    agent = db.execute(select(Agent).where(Agent.name == AGENT_NAME)).scalars().first()
    if not agent or not agent.scenario_id:
        raise RuntimeError(f"未找到已绑定场景的 Agent：{AGENT_NAME}")
    scenario = db.get(BusinessScenario, agent.scenario_id)
    if not scenario:
        raise RuntimeError("医保 Agent 绑定的业务场景不存在")
    db.info["tenant_id"] = scenario.tenant_id or agent.tenant_id
    return agent, scenario


def _sqlite_source(db, scenario: BusinessScenario) -> DataSource:
    sources = db.execute(
        select(DataSource).where(
            DataSource.scenario_id == scenario.id,
            DataSource.type == "sqlite",
        )
    ).scalars().all()
    source = next(
        (
            item for item in sources
            if "项目明细表" in _source_tables(Path(str((item.config or {}).get("path") or "")))
        ),
        None,
    )
    if source is None:
        raise RuntimeError("医保场景没有包含“项目明细表”的 SQLite 数据源")
    return source


def _source_tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }


def _ensure_domain_views(source: DataSource) -> None:
    path = Path(str((source.config or {}).get("path") or "")).resolve()
    if not path.is_file():
        raise RuntimeError(f"医保 SQLite 数据源不存在：{path}")
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE VIEW IF NOT EXISTS "医疗机构视图" AS
            SELECT
                "定点医药机构编号",
                MAX("定点医药机构名称") AS "定点医药机构名称",
                MAX("医院等级") AS "医院等级",
                MAX("定点归属医保区划") AS "定点归属医保区划"
            FROM "就诊表"
            WHERE "定点医药机构编号" IS NOT NULL
              AND TRIM(CAST("定点医药机构编号" AS TEXT)) <> ''
            GROUP BY "定点医药机构编号";

            CREATE VIEW IF NOT EXISTS "医保服务项目视图" AS
            SELECT
                "医保目录编码",
                MAX("医保目录名称") AS "医保目录名称",
                MAX("目录类别") AS "目录类别",
                MAX("医疗收费项目类别") AS "医疗收费项目类别",
                MAX("规格") AS "规格",
                MAX(CAST("单价" AS REAL)) AS "参考单价"
            FROM "项目明细表"
            WHERE "医保目录编码" IS NOT NULL
              AND TRIM(CAST("医保目录编码" AS TEXT)) <> ''
            GROUP BY "医保目录编码";
            """
        )


def _upsert_entities(db, scenario: BusinessScenario) -> dict[str, OntologyEntity]:
    existing = {
        item.name: item
        for item in db.execute(
            select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
        ).scalars()
    }
    result: dict[str, OntologyEntity] = {}
    for spec in ENTITY_SPECS:
        entity = existing.get(spec["name"])
        if entity is None:
            entity = OntologyEntity(scenario_id=scenario.id, name=spec["name"])
            db.add(entity)
            db.flush()
        entity.api_name = spec["api_name"]
        entity.namespace = "medical_audit"
        entity.description = spec["description"]
        entity.icon = spec["icon"]
        entity.color = spec["color"]
        entity.state_property = spec.get("state_property", "")
        properties = {item.name: item for item in entity.properties}
        for property_spec in spec["properties"]:
            prop = properties.get(property_spec["name"])
            if prop is None:
                prop = OntologyProperty(entity_id=entity.id, name=property_spec["name"])
                db.add(prop)
            prop.api_name = property_spec["api_name"]
            prop.data_type = property_spec["data_type"]
            prop.description = property_spec.get("description", "")
            prop.is_key = bool(property_spec.get("is_key"))
            prop.is_title = bool(property_spec.get("is_title"))
            prop.is_required = bool(property_spec.get("is_required"))
            prop.is_sensitive = bool(property_spec.get("is_sensitive"))
            prop.is_enum = bool(property_spec.get("is_enum"))
            prop.enum_values = list(property_spec.get("enum_values") or [])
        result[entity.name] = entity
    db.flush()
    return result


def _upsert_mappings(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    entities: dict[str, OntologyEntity],
) -> dict[str, DataMapping]:
    existing = {
        item.entity_id: item
        for item in db.execute(
            select(DataMapping).where(
                DataMapping.scenario_id == scenario.id,
                DataMapping.data_source_id == source.id,
            )
        ).scalars()
    }
    result: dict[str, DataMapping] = {}
    for spec in MAPPING_SPECS:
        entity = entities[spec["entity"]]
        mapping = existing.get(entity.id)
        if mapping is None:
            mapping = DataMapping(
                scenario_id=scenario.id,
                entity_id=entity.id,
                data_source_id=source.id,
            )
            db.add(mapping)
            db.flush()
        mapping.table_name = spec["table"]
        mapping.column_map = dict(spec["columns"])
        mapping.transform_rules = dict(spec.get("transforms") or {})
        mapping.status = "ready"
        mapping.last_error = ""
        mapping.environment_status = {
            **(mapping.environment_status or {}),
            "dev": {"status": "ready", "last_error": ""},
        }
        result[entity.name] = mapping
    db.flush()
    return result


def _upsert_relations(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    entities: dict[str, OntologyEntity],
    mappings: dict[str, DataMapping],
) -> None:
    existing = {
        item.api_name: item
        for item in db.execute(
            select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
        ).scalars()
    }
    for spec in RELATION_SPECS:
        relation = existing.get(spec["api_name"])
        if relation is None:
            relation = db.execute(
                select(OntologyRelation).where(
                    OntologyRelation.scenario_id == scenario.id,
                    OntologyRelation.name == spec["name"],
                )
            ).scalars().first()
        if relation is None:
            relation = OntologyRelation(
                scenario_id=scenario.id,
                name=spec["name"],
                source_entity_id=entities[spec["source"]].id,
                target_entity_id=entities[spec["target"]].id,
            )
            db.add(relation)
            db.flush()
        relation.name = spec["name"]
        relation.api_name = spec["api_name"]
        relation.namespace = "medical_audit"
        relation.source_entity_id = entities[spec["source"]].id
        relation.target_entity_id = entities[spec["target"]].id
        relation.source_display_name = spec["source_display_name"]
        relation.source_api_name = spec["source_api_name"]
        relation.target_display_name = spec["target_display_name"]
        relation.target_api_name = spec["target_api_name"]
        relation.relation_type = spec["relation_type"]
        relation.storage_kind = "foreign_key" if spec.get("mapping_mode") else "none"
        relation.constraints = {}
        relation.description = (
            f"{spec['source']} 与 {spec['target']} 的双向可导航业务关系。"
        )
        mode = spec.get("mapping_mode")
        if not mode:
            continue
        source_mapping = mappings[spec["source"]]
        target_mapping = mappings[spec["target"]]
        binding = db.execute(
            select(RelationDataMapping).where(
                RelationDataMapping.relation_id == relation.id
            )
        ).scalars().first()
        if binding is None:
            binding = RelationDataMapping(
                scenario_id=scenario.id,
                relation_id=relation.id,
                source_mapping_id=source_mapping.id,
                target_mapping_id=target_mapping.id,
                mode=mode,
                data_source_id=source.id,
            )
            db.add(binding)
        binding.source_mapping_id = source_mapping.id
        binding.target_mapping_id = target_mapping.id
        binding.mode = mode
        binding.data_source_id = source.id
        binding.table_name = (
            source_mapping.table_name if mode == "source_fk" else target_mapping.table_name
        )
        binding.foreign_key_column = spec["foreign_key_column"]
        binding.source_key_column = ""
        binding.target_key_column = ""
        binding.status = "ready"
        binding.last_error = ""
    db.flush()


def _retire_invalid_demo_workflows(db, scenario: BusinessScenario) -> int:
    retired = 0
    action_ids = set(db.scalars(
        select(OntologyAction.id).where(OntologyAction.scenario_id == scenario.id)
    ))
    rule_ids = set(db.scalars(
        select(OntologyRule.id).where(OntologyRule.scenario_id == scenario.id)
    ))
    event_ids = set(db.scalars(
        select(OntologyEvent.id).where(OntologyEvent.scenario_id == scenario.id)
    ))
    for workflow in db.execute(
        select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
    ).scalars():
        invalid = False
        for node in workflow.nodes or []:
            if not isinstance(node, dict):
                invalid = True
                break
            kind = str(node.get("type") or "")
            data = node.get("data") if isinstance(node.get("data"), dict) else {}
            if kind in {"script", "http"}:
                invalid = True
                break
            if kind == "action" and str(data.get("action_id") or "") not in action_ids:
                invalid = True
                break
            if kind == "rule" and str(data.get("rule_id") or "") not in rule_ids:
                invalid = True
                break
            if kind == "event" and str(data.get("event_id") or "") not in event_ids:
                invalid = True
                break
        if invalid and workflow.enabled:
            workflow.enabled = False
            workflow.status = "disabled"
            if "已由领域本体升级停用" not in (workflow.description or ""):
                workflow.description = (
                    (workflow.description or "").rstrip()
                    + "\n已由领域本体升级停用：包含缺失资源或未受治理的脚本/HTTP 节点。"
                ).strip()
            retired += 1
    return retired


def _deprecate_replaced_legacy_model(db, scenario: BusinessScenario) -> int:
    """Retire the old technical meta-model without deleting its history.

    The original demo mixed upload/table/field metadata and materialized rule
    snapshots into the operational ontology.  The replacement domain Object
    Types query the same source data directly, so these rows remain preserved
    for audit/history while active runtime definitions stop exposing them.
    """

    retired = 0
    legacy_entities = db.execute(
        select(OntologyEntity).where(
            OntologyEntity.scenario_id == scenario.id,
            OntologyEntity.name.in_(LEGACY_ENTITY_NAMES),
        )
    ).scalars().all()
    for entity in legacy_entities:
        if (entity.lifecycle_status or "active") != "deprecated":
            entity.lifecycle_status = "deprecated"
            retired += 1
    return retired


def upgrade(db) -> dict[str, Any]:
    agent, scenario = _find_agent_and_scenario(db)
    source = _sqlite_source(db, scenario)
    _ensure_domain_views(source)
    entities = _upsert_entities(db, scenario)
    mappings = _upsert_mappings(db, scenario, source, entities)
    _upsert_relations(db, scenario, source, entities, mappings)
    retired = _retire_invalid_demo_workflows(db, scenario)
    deprecated = _deprecate_replaced_legacy_model(db, scenario)
    if PROMPT_MARKER not in (agent.system_prompt or ""):
        agent.system_prompt = ((agent.system_prompt or "").rstrip() + "\n\n" + PROMPT_APPENDIX).strip()
    agent.max_tokens = max(int(agent.max_tokens or 0), 8192)
    scenario.namespace = "medical_audit"
    if "领域对象" not in (scenario.description or ""):
        scenario.description = (
            (scenario.description or "").rstrip()
            + "\n领域对象包括医疗机构、就诊、收费明细、医保服务项目、审计规则和审计发现；原始事实与审计结论分离。"
        ).strip()
    db.commit()
    return {
        "scenario_id": scenario.id,
        "source_id": source.id,
        "entities": sorted(entities),
        "mappings": sorted(mappings),
        "retired_workflows": retired,
        "deprecated_legacy_entities": deprecated,
    }


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = upgrade(db)
        print("医保审计领域本体升级完成")
        print(f"场景: {result['scenario_id']}")
        print(f"业务对象: {', '.join(result['entities'])}")
        print(f"已停用无效演示工作流: {result['retired_workflows']}")
        print(f"已非破坏性退役旧本体对象: {result['deprecated_legacy_entities']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
