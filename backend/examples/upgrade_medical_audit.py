"""Idempotently upgrade the demo medical-audit scenario to a domain ontology.

The original demo modeled upload mechanics (tables/fields/business-data) and
then labelled raw charge rows as already-confirmed violations.  This upgrade is
additive: it keeps historic objects intact, adds business-facing object/link
types, binds them to governed SQLite views/tables, and retires only explicitly
identified legacy demo resources.  It can be run repeatedly against
``backend/data/platform.db``.

Run from ``backend``::

    python -m examples.upgrade_medical_audit --agent-id <agent-id> --scenario-id <scenario-id> --source-id <source-id>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import uuid
from contextlib import closing, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

from app.database import SessionLocal, init_db
from app.models import (
    Agent,
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    OntologyWorkflow,
    RelationDataMapping,
)


AGENT_NAME = "医保违规审计助手"
SCENARIO_NAME = "医保违规审计"
NAMESPACE = "medical_audit"
RECOVERY_MARKER = "[recovery-pack:medical-audit-v2]"
LEGACY_RETIREMENT_MARKER = "[recovery-pack:medical-audit-v2:legacy-retired]"
WORKFLOW_RETIREMENT_MARKER = "[recovery-pack:medical-audit-v2:workflow-retired]"
MAPPING_PROVENANCE_KEY = "__recovery_pack__"
MAPPING_PROVENANCE_VALUE = "medical-audit-v2"
MAPPING_ATTEMPT_KEY = "__medical_audit_upgrade_attempt__"
PLATFORM_COMMIT_CONFIRMED = "committed"
PLATFORM_COMMIT_NOT_COMMITTED = "not_committed"
PLATFORM_COMMIT_UNKNOWN = "unknown"
OLD_WORKFLOW_RETIREMENT_LINE = (
    "已由领域本体升级停用：包含缺失资源或未受治理的脚本/HTTP 节点。"
)
SCENARIO_DESCRIPTION_APPENDIX = (
    "领域对象包括医疗机构、就诊、收费明细、医保结算、医保服务项目、审计规则和审计发现；"
    "原始事实与审计结论分离。"
)
LEGACY_WORKFLOW_IDS = frozenset(
    {
        "48968bf1066d453898f61e58e30fc904",
        "fdca6ca5b70d4015a8c34002fd108eea",
    }
)
LEGACY_ENTITY_NAMES = {
    "业务数据",
    "表格",
    "字段",
    "规则",
    "违规记录",
    "药品",
    "知识库请求",
}
LEGACY_ENTITY_SHAPES: dict[str, dict[str, Any]] = {
    "业务数据": {
        "description": "用户上传的业务数据实体",
        "properties": (
            ("业务数据ID", "string", True, True, ""),
            ("数据名称", "string", False, True, ""),
            ("数据内容", "json", False, False, ""),
            ("上传时间", "datetime", False, True, ""),
            ("关联客户", "string", False, False, ""),
            ("状态", "string", False, False, ""),
        ),
    },
    "表格": {
        "description": "业务数据中的表格元数据",
        "properties": (
            ("表格ID", "string", True, True, ""),
            ("表格名称", "string", False, True, ""),
            ("所属业务数据", "string", False, True, ""),
            ("表格结构", "json", False, True, ""),
            ("创建时间", "datetime", False, True, ""),
        ),
    },
    "字段": {
        "description": "表格中的字段定义",
        "properties": (
            ("字段ID", "string", True, True, ""),
            ("字段名称", "string", False, True, ""),
            ("字段类型", "string", False, True, ""),
            ("所属表格", "string", False, True, ""),
            ("是否必需", "boolean", False, False, ""),
            ("数据格式", "string", False, False, ""),
        ),
    },
    "规则": {
        "description": "违规判断规则定义",
        "properties": (
            ("规则ID", "string", True, True, ""),
            ("规则名称", "string", False, True, ""),
            ("规则类型", "string", False, True, ""),
            ("规则内容", "string", False, True, ""),
            ("涉及表格", "json", False, False, ""),
            ("涉及字段", "json", False, False, ""),
            ("优先级", "string", False, False, ""),
            ("是否启用", "boolean", False, False, ""),
        ),
    },
    "违规记录": {
        "description": "规则匹配产生的违规记录",
        "properties": (
            ("违规ID", "string", True, True, ""),
            ("关联业务数据", "string", False, True, ""),
            ("关联规则", "string", False, True, ""),
            ("违规类型", "string", False, True, ""),
            ("违规详情", "float", False, True, ""),
            ("违规时间", "datetime", False, True, ""),
            ("是否已处理", "boolean", False, False, ""),
            ("辅助药品ID", "string", False, False, ""),
            ("医院名称", "string", False, False, "医疗机构名称，用于按医院维度进行审计统计和筛选"),
        ),
    },
    "药品": {
        "description": "从外部知识库获取的药品信息",
        "properties": (
            ("药品ID", "string", True, True, ""),
            ("药品名称", "string", False, True, ""),
            ("批准文号", "string", False, False, ""),
            ("生产厂家", "string", False, False, ""),
            ("药品规格", "json", False, False, ""),
            ("更新时间", "datetime", False, False, ""),
        ),
    },
    "知识库请求": {
        "description": "外部知识库或爬虫调用记录",
        "properties": (
            ("请求ID", "string", True, True, ""),
            ("请求类型", "string", False, True, ""),
            ("请求参数", "json", False, True, ""),
            ("返回结果", "json", False, False, ""),
            ("状态", "string", False, True, ""),
            ("请求时间", "datetime", False, True, ""),
            ("响应时间", "float", False, False, ""),
        ),
    },
}
PROMPT_MARKER = "## 医保收费审计执行约定（受控策略 v2）"


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
            {"name": "患者性别", "api_name": "patient_sex", "data_type": "string", "is_sensitive": True},
            {"name": "患者年龄", "api_name": "patient_age", "data_type": "integer", "is_sensitive": True},
            {"name": "住院天数", "api_name": "hospitalization_days", "data_type": "number", "description": "日计价审计使用的实际住院天数。"},
            {"name": "入院诊断", "api_name": "admission_diagnosis", "data_type": "text", "is_sensitive": True},
            {"name": "诊断编码", "api_name": "diagnosis_code", "data_type": "string", "is_sensitive": True},
            {"name": "诊断名称", "api_name": "diagnosis_name", "data_type": "string", "is_sensitive": True},
            {"name": "手术操作编码", "api_name": "procedure_code", "data_type": "string", "is_sensitive": True},
            {"name": "手术操作名称", "api_name": "procedure_name", "data_type": "string", "is_sensitive": True},
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
            {"name": "结算ID", "api_name": "settlement_id", "data_type": "string"},
            {"name": "机构目录编码", "api_name": "provider_service_code", "data_type": "string"},
            {"name": "机构目录名称", "api_name": "provider_service_name", "data_type": "string"},
            {"name": "商品名", "api_name": "product_name", "data_type": "string"},
            {"name": "收费数量", "api_name": "quantity", "data_type": "number", "description": "本条收费明细计费的服务次数或数量。"},
            {"name": "单价", "api_name": "unit_price", "data_type": "number"},
            {"name": "收费金额", "api_name": "total_amount", "data_type": "number"},
            {"name": "符合范围金额", "api_name": "eligible_amount", "data_type": "number"},
            {"name": "发生时间", "api_name": "occurred_at", "data_type": "datetime"},
            {"name": "目录类别", "api_name": "catalog_category", "data_type": "string"},
            {"name": "收费项目类别", "api_name": "charge_category", "data_type": "string"},
            {"name": "规格", "api_name": "specification", "data_type": "string"},
            {"name": "剂型", "api_name": "dosage_form", "data_type": "string"},
            {"name": "使用频次", "api_name": "usage_frequency", "data_type": "string"},
            {"name": "周期天数", "api_name": "cycle_days", "data_type": "number"},
            {"name": "开单科室", "api_name": "ordering_department", "data_type": "string"},
            {"name": "开单医师", "api_name": "ordering_doctor", "data_type": "string", "is_sensitive": True},
        ),
    },
    {
        "name": "医保结算",
        "api_name": "medical_settlement",
        "description": "一次就诊对应的医保结算事实，记录费用总额、符合范围金额、基金和个人支付。",
        "icon": "wallet-cards",
        "color": "#0f766e",
        "properties": (
            {"name": "结算ID", "api_name": "settlement_id", "data_type": "string", "is_key": True, "is_required": True},
            {"name": "就诊ID", "api_name": "encounter_id", "data_type": "string", "is_required": True},
            {"name": "医疗机构ID", "api_name": "facility_id", "data_type": "string"},
            {"name": "医疗机构名称", "api_name": "facility_name", "data_type": "string", "is_title": True},
            {"name": "患者ID", "api_name": "patient_id", "data_type": "string", "is_sensitive": True},
            {"name": "患者姓名", "api_name": "patient_name", "data_type": "string", "is_sensitive": True},
            {"name": "结算时间", "api_name": "settled_at", "data_type": "datetime"},
            {"name": "医疗费总额", "api_name": "medical_total_amount", "data_type": "number"},
            {"name": "符合范围金额", "api_name": "eligible_amount", "data_type": "number"},
            {"name": "统筹基金支出", "api_name": "pooled_fund_payment", "data_type": "number"},
            {"name": "基金支付总额", "api_name": "fund_payment_total", "data_type": "number"},
            {"name": "个人支付金额", "api_name": "personal_payment", "data_type": "number"},
            {"name": "现金支付金额", "api_name": "cash_payment", "data_type": "number"},
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
        "required_properties": ("医疗机构ID", "医疗机构名称"),
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
            "患者性别": "性别",
            "患者年龄": "年龄",
            "住院天数": "住院天数",
            "入院诊断": "入院诊断描述",
            "诊断编码": "住院主诊断代码",
            "诊断名称": "住院主诊断名称",
            "手术操作编码": "手术操作代码",
            "手术操作名称": "手术操作名称",
        },
        "column_candidates": {
            "住院天数": ("实际住院天数",),
            "入院诊断": ("入院诊断名称",),
            "诊断编码": ("主诊断代码",),
            "诊断名称": ("主诊断名称", "诊断名称"),
        },
        "required_properties": ("就诊ID", "医疗机构ID"),
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
            "结算ID": "结算ID",
            "机构目录编码": "医药机构目录编码",
            "机构目录名称": "医药机构目录名称",
            "商品名": "商品名",
            "收费数量": "数量",
            "单价": "单价",
            "收费金额": "明细项目费用总额",
            "符合范围金额": "符合范围金额",
            "发生时间": "费用发生时间",
            "目录类别": "目录类别",
            "收费项目类别": "医疗收费项目类别",
            "规格": "规格",
            "剂型": "剂型名称",
            "使用频次": "使用频次描述",
            "周期天数": "周期天数",
            "开单科室": "开单科室名称",
            "开单医师": "开单医师姓名",
        },
        "column_candidates": {
            "收费明细ID": ("费用明细流水号", "收费明细ID"),
            "服务项目名称": ("医疗目录名称", "项目名称"),
            "收费金额": ("项目费用总额", "收费金额"),
            "符合范围金额": ("医保范围金额",),
        },
        "required_properties": ("收费明细ID", "服务项目名称", "就诊ID", "收费数量", "收费金额"),
        "transforms": {"收费明细ID": [{"op": "to_string"}]},
    },
    {
        "entity": "医保结算",
        "table": "结算表",
        "table_candidates": ("医保结算表", "结算信息表"),
        "columns": {
            "结算ID": "结算ID",
            "就诊ID": "就诊ID",
            "医疗机构ID": "定点医药机构编号",
            "医疗机构名称": "定点医药机构名称",
            "患者ID": "人员编号",
            "患者姓名": "人员姓名",
            "结算时间": "结算时间",
            "医疗费总额": "医疗费总额",
            "符合范围金额": "符合范围金额",
            "统筹基金支出": "统筹基金支出",
            "基金支付总额": "基金支付总额",
            "个人支付金额": "个人支付金额",
            "现金支付金额": "现金支付金额",
        },
        "required_properties": ("结算ID", "就诊ID"),
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
        "required_properties": ("服务项目编码", "服务项目名称"),
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
        "required_properties": ("规则ID", "规则名称"),
        "transforms": {"规则ID": [{"op": "to_string"}]},
    },
)


HISTORIC_DOMAIN_MAPPING_CONTRACTS: dict[str, dict[str, Any]] = {
    "医疗机构": {
        "id": "c93d6fdcc165415c903f34efe1ba893b",
        "entity_id": "b6ad3ca702eb4f1baad93cba22a5429d",
        "created_at": "2026-08-25 12:00:47.766866",
        "binding_key": "data_source:医保审计业务库:sqlite",
        "binding_ref": {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
    },
    "就诊": {
        "id": "977d2919009d4ed59c8320dcc3f3bb2c",
        "entity_id": "97d00873473a4c479f7946c28fb2f359",
        "created_at": "2026-08-23 17:57:26.305641",
        "binding_key": "data_source:医保审计业务库:sqlite",
        "binding_ref": {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
    },
    "收费明细": {
        "id": "249218f9fcaa4c92bf6291cb9184d6ce",
        "entity_id": "3695a8e9dd54494192a28d1694cf61c4",
        "created_at": "2026-08-23 17:57:26.306946",
        "binding_key": "data_source:医保审计业务库:sqlite",
        "binding_ref": {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
    },
    "医保结算": {
        "id": "a12f372680f646f6bae9f029f00e517d",
        "entity_id": "e23a65e045d84c57973bf5f6d8241fbd",
        "created_at": "2026-08-25 14:14:39.372178",
        "binding_key": "",
        "binding_ref": {},
    },
    "医保服务项目": {
        "id": "46f15a0b686e4cc584c5271e89054ca5",
        "entity_id": "b9da8302d2624919baae18be02730fc7",
        "created_at": "2026-08-25 11:59:06.559805",
        "binding_key": "data_source:医保审计业务库:sqlite",
        "binding_ref": {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
    },
    "审计规则": {
        "id": "05408cb9c06c4e81a1fd84deb9d039d9",
        "entity_id": "e58637aa7fd84394968586e585b35178",
        "created_at": "2026-08-23 17:57:26.310395",
        "binding_key": "data_source:医保审计业务库:sqlite",
        "binding_ref": {"adapter": "sqlite", "required_capabilities": ["sql_read"]},
    },
}
HISTORIC_SCENARIO_ID = "cc5d3ff36d2a468596dfa9f8ef2995da"
HISTORIC_SOURCE_ID = "a2d20a398ed744e7839acb910f377d6a"


# These rows were created by the original medical-audit upgrade.  Runtime
# refresh telemetry is intentionally excluded; all ownership and definition
# fields are fixed so a same-named user binding cannot be claimed.
HISTORIC_RELATION_MAPPING_CONTRACTS: dict[str, dict[str, Any]] = {
    "facility_encounters": {
        "id": "70178f7e1c7c41d187d24f3f002358be",
        "relation_id": "316fae707b2f4b08b45c043682fba2a3",
        "source_mapping_id": "c93d6fdcc165415c903f34efe1ba893b",
        "target_mapping_id": "977d2919009d4ed59c8320dcc3f3bb2c",
        "created_at": "2026-08-25 13:28:29.505307",
        "binding_key": "",
        "binding_ref": {},
    },
    "encounter_charge_lines": {
        "id": "70f417c5f02e43968eec694501da9f7e",
        "relation_id": "caf17e8a84234b92ae913aa7ef250196",
        "source_mapping_id": "977d2919009d4ed59c8320dcc3f3bb2c",
        "target_mapping_id": "249218f9fcaa4c92bf6291cb9184d6ce",
        "created_at": "2026-08-23 17:57:26.345774",
        "binding_key": "data_source:医保审计业务库:sqlite",
        "binding_ref": {
            "adapter": "sqlite",
            "required_capabilities": ["sql_read"],
        },
    },
    "encounter_settlements": {
        "id": "4d364e0abcf04bccbc75ea59d7465d88",
        "relation_id": "35c379f24a064bf4a1c004a105570ee6",
        "source_mapping_id": "977d2919009d4ed59c8320dcc3f3bb2c",
        "target_mapping_id": "a12f372680f646f6bae9f029f00e517d",
        "created_at": "2026-08-25 14:14:39.393987",
        "binding_key": "",
        "binding_ref": {},
    },
    "charge_line_service": {
        "id": "69541133800c412dadf5e842894fdcae",
        "relation_id": "e772919d81e146a280079970265c1c94",
        "source_mapping_id": "249218f9fcaa4c92bf6291cb9184d6ce",
        "target_mapping_id": "46f15a0b686e4cc584c5271e89054ca5",
        "created_at": "2026-08-25 13:28:29.505330",
        "binding_key": "",
        "binding_ref": {},
    },
}


# The digest covers every workflow definition field after removing only the
# two known retirement lines.  Stable IDs and creation times tie the rows to
# the historic demo; status may be active before, or disabled after, retirement.
HISTORIC_WORKFLOW_CONTRACTS: dict[str, dict[str, str]] = {
    "48968bf1066d453898f61e58e30fc904": {
        "created_at": "2026-08-18 03:51:03.471230",
        "definition_sha256": "5e7fe9e5e7e4e37352a8504ece53258a98e8a721d4a4ca7158570d9a000130ba",
    },
    "fdca6ca5b70d4015a8c34002fd108eea": {
        "created_at": "2026-08-18 04:52:51.845417",
        "definition_sha256": "92954b5f3622fc3b802bfce1aeeb309354041f1305b82d6f12d32b24b18af610",
    },
}


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
        "foreign_key_property": "医疗机构ID",
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
        "foreign_key_property": "就诊ID",
    },
    {
        "name": "就诊对应医保结算",
        "api_name": "encounter_settlements",
        "source": "就诊",
        "target": "医保结算",
        "relation_type": "1:N",
        "source_display_name": "医保结算",
        "source_api_name": "settlements",
        "target_display_name": "所属就诊",
        "target_api_name": "encounter",
        "mapping_mode": "target_fk",
        "foreign_key_column": "就诊ID",
        "foreign_key_property": "就诊ID",
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
        "foreign_key_property": "服务项目编码",
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
- 违规筛查优先调用内建 `run_medical_audit`；它不属于 Action scope，也不接收 SQL、表名、列名或数据源 id。
- 单条收费数量阈值使用 `charge_threshold`（service_name、threshold，可选 facility_name）；“刮痧治疗”数量大于 2 的条件必须精确匹配，不能混入“中医刮痧”。
- 日计价超过住院天数使用 `daily_overstay`（service_names）；项目清单必须来自用户或规则依据，不能用“包含护理字样”替代精确目录项目。
- 同一就诊包含项目仍另收子项目使用 `included_service_duplicate`（included_service、duplicate_service）；金额只汇总另收费子项目明细。
- 限制用药天数使用 `limited_drug_duration`（drug_name、max_days）；0 命中是审计成功，明确回答“本次未发现符合条件的违规明细”。
- 工具返回 `truncated=true` 时保持相同策略参数，以 `next_offset` 继续读取；最终回答使用 summary 的全量计数/金额并引用 evidence 口径，不能只统计当前页。
- 只有不属于上述四类的普通业务查询才使用 `query_business_data`；任何情况下都不要生成 SQL，也不要要求用户提供内部 UUID。
""".strip()


def _marked_description(description: str, marker: str = RECOVERY_MARKER) -> str:
    lines = [line.rstrip() for line in str(description or "").rstrip().splitlines()]
    if marker not in {line.strip() for line in lines}:
        lines.append(marker)
    return "\n".join(lines).strip()


def _has_marker(value: Any, marker: str = RECOVERY_MARKER) -> bool:
    return marker in {
        line.strip()
        for line in str(getattr(value, "description", "") or "").splitlines()
    }


def _identity_match(
    values: list[Any],
    *,
    name: str,
    api_name: str,
    label: str,
) -> Any | None:
    matches = {
        str(value.id): value
        for value in values
        if str(getattr(value, "name", "") or "") == name
        or str(getattr(value, "api_name", "") or "") == api_name
    }
    if len(matches) > 1:
        raise RuntimeError(
            f"{label}身份冲突：name={name!r} 与 api_name={api_name!r} 指向不同记录"
        )
    return next(iter(matches.values()), None)


def _generated_api_name(value: Any, resource_id: Any, *prefixes: str) -> bool:
    text = str(value or "")
    identity = str(resource_id or "")
    if len(identity) != 32 or not all(
        char in "0123456789abcdef" for char in identity.lower()
    ):
        return False
    return any(text == f"{prefix}{identity}" for prefix in prefixes)


def _legacy_property_matches(prop: OntologyProperty, spec: dict[str, Any]) -> bool:
    return (
        prop.name == spec["name"]
        and prop.api_name == spec["api_name"]
        and prop.data_type == spec["data_type"]
        and (prop.description or "") == spec.get("description", "")
        and bool(prop.is_key) is bool(spec.get("is_key"))
        and bool(prop.is_title) is bool(spec.get("is_title"))
        and bool(prop.is_required) is bool(spec.get("is_required"))
        and bool(prop.is_sensitive) is bool(spec.get("is_sensitive"))
        and bool(prop.is_enum) is bool(spec.get("is_enum"))
        and list(prop.enum_values or []) == list(spec.get("enum_values") or [])
        and prop.default_value == ""
        and dict(prop.constraints or {}) == {}
    )


def _legacy_domain_entity_matches(entity: OntologyEntity, spec: dict[str, Any]) -> bool:
    properties = list(entity.properties or [])
    expected = list(spec["properties"])
    if (
        entity.name != spec["name"]
        or entity.api_name != spec["api_name"]
        or entity.namespace != NAMESPACE
        or (entity.lifecycle_status or "active") != "active"
        or (entity.description or "") != spec["description"]
        or entity.icon != spec["icon"]
        or entity.color != spec["color"]
        or bool(entity.is_abstract)
        or (entity.state_property or "") != spec.get("state_property", "")
        or len(properties) != len(expected)
    ):
        return False
    by_name = {prop.name: prop for prop in properties}
    return len(by_name) == len(properties) and all(
        property_spec["name"] in by_name
        and _legacy_property_matches(by_name[property_spec["name"]], property_spec)
        for property_spec in expected
    )


def _legacy_retired_entity_matches(entity: OntologyEntity) -> bool:
    shape = LEGACY_ENTITY_SHAPES.get(entity.name)
    if shape is None:
        return False
    if (
        entity.namespace != "default"
        or (entity.description or "") != shape["description"]
        or (entity.lifecycle_status or "active") not in {"active", "deprecated"}
        or entity.icon != "box"
        or entity.color != "#4f46e5"
        or bool(entity.is_abstract)
        or (entity.state_property or "") != ""
        or not _generated_api_name(entity.api_name, entity.id, "", "entity_")
    ):
        return False
    properties = list(entity.properties or [])
    expected = {item[0]: item for item in shape["properties"]}
    if len(properties) != len(expected) or len({prop.name for prop in properties}) != len(
        properties
    ):
        return False
    for prop in properties:
        property_shape = expected.get(prop.name)
        if property_shape is None:
            return False
        name, data_type, is_key, is_required, description = property_shape
        if not (
            prop.name == name
            and prop.data_type == data_type
            and bool(prop.is_key) is bool(is_key)
            and bool(prop.is_title) is bool(is_key)
            and bool(prop.is_required) is bool(is_required)
            and (prop.description or "") == description
            and _generated_api_name(prop.api_name, prop.id, "", "property_")
            and not bool(prop.is_sensitive)
            and not bool(prop.is_enum)
            and list(prop.enum_values or []) == []
            and prop.default_value in (None, "")
            and dict(prop.constraints or {}) == {}
        ):
            return False
    return True


def _relation_description(spec: dict[str, Any]) -> str:
    return f"{spec['source']} 与 {spec['target']} 的双向可导航业务关系。"


def _legacy_relation_matches(
    relation: OntologyRelation,
    spec: dict[str, Any],
    entities: dict[str, OntologyEntity | None],
) -> bool:
    source = entities.get(spec["source"])
    target = entities.get(spec["target"])
    if source is None or target is None:
        return False
    return (
        relation.name == spec["name"]
        and relation.api_name == spec["api_name"]
        and relation.namespace == NAMESPACE
        and relation.source_entity_id == source.id
        and relation.target_entity_id == target.id
        and relation.source_display_name == spec["source_display_name"]
        and relation.source_api_name == spec["source_api_name"]
        and relation.target_display_name == spec["target_display_name"]
        and relation.target_api_name == spec["target_api_name"]
        and relation.relation_type == spec["relation_type"]
        and relation.storage_kind == ("foreign_key" if spec.get("mapping_mode") else "none")
        and dict(relation.constraints or {}) == {}
        and (relation.description or "") == _relation_description(spec)
    )


def _domain_entity_claims(
    db,
    scenario: BusinessScenario,
) -> dict[str, OntologyEntity | None]:
    existing = list(db.scalars(
        select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
    ))
    claims: dict[str, OntologyEntity | None] = {}
    for spec in ENTITY_SPECS:
        entity = _identity_match(
            existing,
            name=spec["name"],
            api_name=spec["api_name"],
            label="医保恢复包对象类型",
        )
        if (
            entity is not None
            and not _has_marker(entity)
            and not _legacy_domain_entity_matches(entity, spec)
        ):
            raise RuntimeError(
                f"对象类型“{spec['name']}”已被未标记资源占用；恢复包不会覆盖现有定义"
            )
        if entity is not None:
            properties = list(entity.properties or [])
            for property_spec in spec["properties"]:
                prop = _identity_match(
                    properties,
                    name=property_spec["name"],
                    api_name=property_spec["api_name"],
                    label=f"对象类型“{entity.name}”的属性",
                )
                if (
                    prop is not None
                    and not _has_marker(prop)
                    and not _legacy_property_matches(prop, property_spec)
                ):
                    raise RuntimeError(
                        f"属性“{entity.name}.{property_spec['name']}”已被未标记资源占用；"
                        "恢复包不会覆盖现有定义"
                    )
        claims[spec["name"]] = entity
    return claims


def _resolved_mapping_contracts(
    source: DataSource,
    *,
    connection: sqlite3.Connection | None = None,
    read_only: bool = False,
) -> dict[str, dict[str, Any]]:
    path = Path(str((source.config or {}).get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"医保 SQLite 数据源不存在：{path}")
    if connection is not None:
        manager = nullcontext(connection)
    elif read_only:
        manager = closing(sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        ))
    else:
        manager = closing(sqlite3.connect(path))
    with manager as source_connection:
        source_tables = {
            str(row[0])
            for row in source_connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }
        columns_by_table = {
            table_name: _table_columns(source_connection, table_name)
            for table_name in source_tables
        }
    # These two views are deterministic outputs of _ensure_domain_views.  A
    # clean install can therefore preflight mappings before creating them.
    for spec in MAPPING_SPECS:
        if spec["table"] in {"医疗机构视图", "医保服务项目视图"}:
            columns_by_table.setdefault(spec["table"], set(spec["columns"].values()))
    contracts: dict[str, dict[str, Any]] = {}
    for spec in MAPPING_SPECS:
        table_name = next(
            (
                candidate
                for candidate in (spec["table"], *spec.get("table_candidates", ()))
                if candidate in source_tables or candidate in columns_by_table
            ),
            spec["table"],
        )
        available_columns = columns_by_table.get(table_name, set())
        column_map: dict[str, str] = {}
        candidate_overrides = spec.get("column_candidates") or {}
        for property_name, preferred_column in spec["columns"].items():
            candidates = (preferred_column, *candidate_overrides.get(property_name, ()))
            resolved_column = _first_column(available_columns, *candidates)
            if resolved_column:
                column_map[property_name] = resolved_column
        missing_required = [
            property_name
            for property_name in spec.get("required_properties", ())
            if property_name not in column_map
        ]
        status = "error" if missing_required else "ready"
        last_error = (
            f"源表缺少必需映射属性：{', '.join(missing_required)}"
            if missing_required else ""
        )
        contracts[spec["entity"]] = {
            "table_name": table_name,
            "column_map": column_map,
            "transform_rules": {
                property_name: transforms
                for property_name, transforms in (spec.get("transforms") or {}).items()
                if property_name in column_map
            },
            "status": status,
            "last_error": last_error,
        }
    return contracts


def _mapping_definition_matches(
    mapping: DataMapping,
    contract: dict[str, Any],
) -> bool:
    expected_environment = {
        "dev": {
            "status": contract["status"],
            "last_error": contract["last_error"],
        }
    }
    return (
        mapping.table_name == contract["table_name"]
        and dict(mapping.column_map or {}) == contract["column_map"]
        and dict(mapping.transform_rules or {}) == contract["transform_rules"]
        and mapping.status == contract["status"]
        and (mapping.last_error or "") == contract["last_error"]
        and dict(mapping.environment_status or {}) == expected_environment
    )


def _historic_domain_mapping_matches(
    mapping: DataMapping,
    entity: OntologyEntity,
    contract: dict[str, Any],
) -> bool:
    historic = HISTORIC_DOMAIN_MAPPING_CONTRACTS.get(entity.name)
    if historic is None:
        return False
    return (
        mapping.id == historic["id"]
        and mapping.entity_id == historic["entity_id"] == entity.id
        and mapping.scenario_id == HISTORIC_SCENARIO_ID
        and mapping.data_source_id == HISTORIC_SOURCE_ID
        and str(mapping.created_at) == historic["created_at"]
        and mapping.data_source_binding_key == historic["binding_key"]
        and dict(mapping.data_source_binding_ref or {}) == historic["binding_ref"]
        and _mapping_definition_matches(mapping, contract)
    )


def _assert_mapping_owned(
    mapping: DataMapping,
    entity: OntologyEntity,
    contract: dict[str, Any],
) -> None:
    environment_status = dict(mapping.environment_status or {})
    if environment_status.get(MAPPING_PROVENANCE_KEY) == MAPPING_PROVENANCE_VALUE:
        return
    unmarked_v1 = (
        mapping.data_source_binding_key == ""
        and dict(mapping.data_source_binding_ref or {}) == {}
        and _mapping_definition_matches(mapping, contract)
    )
    if unmarked_v1 or _historic_domain_mapping_matches(mapping, entity, contract):
        return
    raise RuntimeError(
        f"对象“{entity.name}”的数据映射是未标记的现有资源；恢复包不会覆盖它"
    )


def _preflight_mapping_ownership(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    entities: dict[str, OntologyEntity | None],
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, DataMapping | None], dict[str, dict[str, Any]]]:
    contracts = _resolved_mapping_contracts(
        source,
        connection=connection,
        read_only=connection is None,
    )
    target_entity_ids = {
        str(entity.id)
        for entity in entities.values()
        if entity is not None
    }
    existing_by_entity: dict[str, list[DataMapping]] = {}
    for mapping in db.scalars(
        select(DataMapping).where(
            DataMapping.scenario_id == scenario.id,
            DataMapping.entity_id.in_(target_entity_ids),
        )
    ):
        existing_by_entity.setdefault(mapping.entity_id, []).append(mapping)
    claims: dict[str, DataMapping | None] = {}
    for spec in MAPPING_SPECS:
        entity = entities.get(spec["entity"])
        if entity is None:
            claims[spec["entity"]] = None
            continue
        candidates = existing_by_entity.get(entity.id, [])
        if len(candidates) > 1:
            raise RuntimeError(f"对象“{entity.name}”的数据映射不唯一，拒绝自动认领")
        if candidates:
            if str(candidates[0].data_source_id) != str(source.id):
                raise RuntimeError(
                    f"对象“{entity.name}”已存在指向其他数据源的映射；"
                    "恢复包不会迁移或覆盖跨数据源用户映射"
                )
            _assert_mapping_owned(candidates[0], entity, contracts[entity.name])
        claims[spec["entity"]] = candidates[0] if candidates else None
    return claims, contracts


def _relation_mapping_contract(
    spec: dict[str, Any],
    scenario: BusinessScenario,
    source: DataSource,
    relation: OntologyRelation,
    source_mapping: DataMapping,
    target_mapping: DataMapping,
) -> dict[str, Any]:
    mode = str(spec["mapping_mode"])
    table_name = (
        source_mapping.table_name if mode == "source_fk" else target_mapping.table_name
    )
    foreign_key_mapping = source_mapping if mode == "source_fk" else target_mapping
    foreign_key_column = (
        (foreign_key_mapping.column_map or {}).get(
            spec.get("foreign_key_property", "")
        )
        or spec["foreign_key_column"]
    )
    binding_ready = (
        source_mapping.status == "ready"
        and target_mapping.status == "ready"
        and foreign_key_column in (foreign_key_mapping.column_map or {}).values()
    )
    return {
        "scenario_id": scenario.id,
        "relation_id": relation.id,
        "source_mapping_id": source_mapping.id,
        "target_mapping_id": target_mapping.id,
        "mode": mode,
        "data_source_id": source.id,
        "table_name": table_name,
        "foreign_key_column": foreign_key_column,
        "source_key_column": "",
        "target_key_column": "",
        "status": "ready" if binding_ready else "error",
        "last_error": "" if binding_ready else "关系依赖的对象映射或外键字段未就绪",
    }


def _relation_mapping_definition_matches(
    binding: RelationDataMapping,
    contract: dict[str, Any],
) -> bool:
    return all(
        getattr(binding, field) == expected
        for field, expected in contract.items()
    )


def _historic_relation_mapping_matches(
    binding: RelationDataMapping,
    relation: OntologyRelation,
    contract: dict[str, Any],
) -> bool:
    historic = HISTORIC_RELATION_MAPPING_CONTRACTS.get(relation.api_name)
    if historic is None:
        return False
    return (
        binding.id == historic["id"]
        and relation.id == historic["relation_id"]
        and binding.relation_id == historic["relation_id"]
        and binding.source_mapping_id == historic["source_mapping_id"]
        and binding.target_mapping_id == historic["target_mapping_id"]
        and binding.scenario_id == HISTORIC_SCENARIO_ID
        and binding.data_source_id == HISTORIC_SOURCE_ID
        and str(binding.created_at) == historic["created_at"]
        and binding.data_source_binding_key == historic["binding_key"]
        and dict(binding.data_source_binding_ref or {}) == historic["binding_ref"]
        and _relation_mapping_definition_matches(binding, contract)
    )


def _assert_relation_mapping_owned(
    binding: RelationDataMapping,
    relation: OntologyRelation,
    contract: dict[str, Any],
    *,
    relation_was_marked: bool | None = None,
) -> None:
    if not _relation_mapping_definition_matches(binding, contract):
        raise RuntimeError(
            f"关系“{relation.name}”的数据映射定义与恢复包不一致；恢复包不会覆盖它"
        )
    if relation_was_marked is None:
        relation_was_marked = _has_marker(relation)
    if relation_was_marked:
        if not isinstance(binding.data_source_binding_key, str) or not isinstance(
            binding.data_source_binding_ref or {}, dict
        ):
            raise RuntimeError(f"关系“{relation.name}”的数据源绑定格式无效")
        return
    unmarked_v1 = (
        binding.data_source_binding_key == ""
        and dict(binding.data_source_binding_ref or {}) == {}
    )
    if unmarked_v1 or _historic_relation_mapping_matches(
        binding, relation, contract
    ):
        return
    raise RuntimeError(
        f"关系“{relation.name}”的数据映射是未标记的现有资源；恢复包不会覆盖它"
    )


def _preflight_relation_mapping_ownership(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    relations: dict[str, OntologyRelation | None],
    mappings: dict[str, DataMapping | None],
) -> None:
    for spec in RELATION_SPECS:
        relation = relations.get(spec["name"])
        if relation is None:
            continue
        bindings = list(db.scalars(
            select(RelationDataMapping).where(
                RelationDataMapping.relation_id == relation.id
            )
        ))
        if len(bindings) > 1:
            raise RuntimeError(f"关系“{relation.name}”的数据映射不唯一")
        mode = spec.get("mapping_mode")
        if not mode:
            if bindings:
                raise RuntimeError(
                    f"关系“{relation.name}”不应存在数据映射，拒绝自动处理"
                )
            continue
        if not bindings:
            continue
        source_mapping = mappings.get(spec["source"])
        target_mapping = mappings.get(spec["target"])
        if source_mapping is None or target_mapping is None:
            raise RuntimeError(
                f"关系“{relation.name}”已有数据映射，但依赖的对象映射不属于恢复包"
            )
        contract = _relation_mapping_contract(
            spec,
            scenario,
            source,
            relation,
            source_mapping,
            target_mapping,
        )
        _assert_relation_mapping_owned(bindings[0], relation, contract)


def _workflow_definition_sha256(workflow: OntologyWorkflow) -> str:
    description_lines = [
        line
        for line in str(workflow.description or "").splitlines()
        if line.strip()
        not in {OLD_WORKFLOW_RETIREMENT_LINE, WORKFLOW_RETIREMENT_MARKER}
    ]
    payload = {
        "name": workflow.name,
        "description": "\n".join(description_lines),
        "trigger_type": workflow.trigger_type,
        "trigger_config": workflow.trigger_config,
        "steps": workflow.steps,
        "nodes": workflow.nodes,
        "edges": workflow.edges,
        "access_scope": workflow.access_scope,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _historic_workflow_matches(workflow: OntologyWorkflow) -> bool:
    contract = HISTORIC_WORKFLOW_CONTRACTS.get(str(workflow.id))
    if contract is None:
        return False
    lines = [line.strip() for line in str(workflow.description or "").splitlines()]
    if (
        lines.count(OLD_WORKFLOW_RETIREMENT_LINE) > 1
        or lines.count(WORKFLOW_RETIREMENT_MARKER) > 0
        or (bool(workflow.enabled), str(workflow.status or ""))
        not in {(True, "active"), (False, "disabled")}
        or workflow.scenario_id != HISTORIC_SCENARIO_ID
        or str(workflow.created_at) != contract["created_at"]
    ):
        return False
    try:
        return _workflow_definition_sha256(workflow) == contract["definition_sha256"]
    except (TypeError, ValueError):
        return False


def _assert_workflow_owned(workflow: OntologyWorkflow) -> None:
    lines = [line.strip() for line in str(workflow.description or "").splitlines()]
    marker_count = lines.count(WORKFLOW_RETIREMENT_MARKER)
    if marker_count == 1:
        return
    if marker_count > 1:
        raise RuntimeError(
            f"工作流“{workflow.name}”的恢复包退役标记重复，拒绝继续修改"
        )
    if str(workflow.id) in LEGACY_WORKFLOW_IDS and _historic_workflow_matches(workflow):
        return
    raise RuntimeError(
        f"工作流“{workflow.name}”使用待退役 legacy ID，但不符合历史精确契约"
    )


def _preflight_workflow_ownership(db, scenario: BusinessScenario) -> None:
    for workflow in db.scalars(
        select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
    ):
        if str(workflow.id) in LEGACY_WORKFLOW_IDS or _has_marker(
            workflow, WORKFLOW_RETIREMENT_MARKER
        ):
            _assert_workflow_owned(workflow)


def _preflight_agent_updates(
    agent: Agent,
    scenario: BusinessScenario,
    source: DataSource,
) -> None:
    prompt = agent.system_prompt
    if not isinstance(prompt, str):
        raise RuntimeError("医保 Agent 的 system_prompt 不是文本，拒绝修改")
    prompt_marker_count = prompt.count(PROMPT_MARKER)
    if prompt_marker_count > 1:
        raise RuntimeError("医保 Agent 的 v2 提示词标记重复，拒绝修改")
    if prompt_marker_count == 1 and PROMPT_APPENDIX not in prompt:
        raise RuntimeError("医保 Agent 的 v2 提示词标记不完整，拒绝修改")
    source_ids = agent.data_source_ids
    if source_ids is None:
        source_ids = []
    if not isinstance(source_ids, list) or any(
        not isinstance(item, str) or not item for item in source_ids
    ):
        raise RuntimeError("医保 Agent 的 data_source_ids 格式无效，拒绝修改")
    if len(source_ids) != len(set(source_ids)):
        raise RuntimeError("医保 Agent 的 data_source_ids 存在重复绑定，拒绝修改")
    if isinstance(agent.max_tokens, bool):
        raise RuntimeError("医保 Agent 的 max_tokens 格式无效，拒绝修改")
    try:
        max_tokens = int(agent.max_tokens or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("医保 Agent 的 max_tokens 格式无效，拒绝修改") from exc
    if max_tokens < 0:
        raise RuntimeError("医保 Agent 的 max_tokens 不能为负数")
    description = scenario.description
    if description is not None and not isinstance(description, str):
        raise RuntimeError("医保场景 description 不是文本，拒绝修改")
    if str(description or "").count(SCENARIO_DESCRIPTION_APPENDIX) > 1:
        raise RuntimeError("医保场景领域说明重复，拒绝继续修改")
    if source.id in source_ids and source_ids.count(source.id) != 1:
        raise RuntimeError("医保 Agent 的目标数据源绑定不唯一")


def _preflight_recovery_ownership(
    db,
    scenario: BusinessScenario,
    *,
    agent: Agent | None = None,
    source: DataSource | None = None,
    connection: sqlite3.Connection | None = None,
) -> None:
    entities = _domain_entity_claims(db, scenario)
    existing_relations = list(db.scalars(
        select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
    ))
    relation_claims: dict[str, OntologyRelation | None] = {}
    for spec in RELATION_SPECS:
        relation = _identity_match(
            existing_relations,
            name=spec["name"],
            api_name=spec["api_name"],
            label="医保恢复包关系类型",
        )
        if (
            relation is not None
            and not _has_marker(relation)
            and not _legacy_relation_matches(relation, spec, entities)
        ):
            raise RuntimeError(
                f"关系类型“{spec['name']}”已被未标记资源占用；恢复包不会覆盖现有定义"
            )
        relation_claims[spec["name"]] = relation
    legacy_entities = list(db.scalars(
        select(OntologyEntity).where(
            OntologyEntity.scenario_id == scenario.id,
            OntologyEntity.name.in_(LEGACY_ENTITY_NAMES),
        )
    ))
    counts: dict[str, int] = {}
    for entity in legacy_entities:
        counts[entity.name] = counts.get(entity.name, 0) + 1
        if counts[entity.name] > 1:
            raise RuntimeError(f"待退役旧对象类型名称不唯一：{entity.name}")
        if (
            not _has_marker(entity, LEGACY_RETIREMENT_MARKER)
            and not _legacy_retired_entity_matches(entity)
        ):
            raise RuntimeError(
                f"旧对象名称“{entity.name}”被未标记的非演示资源占用；恢复包不会退役它"
            )
    _preflight_workflow_ownership(db, scenario)
    if source is not None:
        _preflight_source_schema(source, connection=connection)
        mappings, _ = _preflight_mapping_ownership(
            db,
            scenario,
            source,
            entities,
            connection=connection,
        )
        _preflight_relation_mapping_ownership(
            db,
            scenario,
            source,
            relation_claims,
            mappings,
        )
        if agent is not None:
            _preflight_agent_updates(agent, scenario, source)


def _validate_business_scenario(scenario: BusinessScenario) -> None:
    if scenario.name != SCENARIO_NAME or scenario.namespace != NAMESPACE:
        raise RuntimeError(
            f"业务场景“{scenario.name}”不是医保违规审计场景，拒绝运行恢复包"
        )


def _find_agent_and_scenario(
    db,
    *,
    agent_id: str | None = None,
    scenario_id: str | None = None,
) -> tuple[Agent, BusinessScenario]:
    explicit_scenario = None
    if scenario_id:
        explicit_scenario = db.get(BusinessScenario, scenario_id)
        if explicit_scenario is None:
            raise RuntimeError(f"找不到显式业务场景：{scenario_id}")
        _validate_business_scenario(explicit_scenario)
    if agent_id:
        agent = db.get(Agent, agent_id)
        agents = [agent] if agent is not None else []
    else:
        query = select(Agent).where(Agent.name == AGENT_NAME)
        if scenario_id:
            query = query.where(Agent.scenario_id == scenario_id)
        agents = db.execute(query).scalars().all()
    if not agents:
        raise RuntimeError(f"未找到已绑定场景的 Agent：{AGENT_NAME}")
    if len(agents) > 1:
        raise RuntimeError(
            f"找到 {len(agents)} 个同名医保 Agent；请显式传入 agent_id 或 scenario_id"
        )
    agent = agents[0]
    if agent.name != AGENT_NAME:
        raise RuntimeError(
            f"显式 Agent“{agent.name}”不是“{AGENT_NAME}”，拒绝运行医保恢复包"
        )
    if not agent.scenario_id:
        raise RuntimeError(f"医保 Agent 未绑定业务场景：{agent.id}")
    if scenario_id and str(agent.scenario_id) != str(scenario_id):
        raise RuntimeError("显式 Agent 与业务场景不匹配")
    scenario = db.get(BusinessScenario, agent.scenario_id)
    if not scenario:
        raise RuntimeError("医保 Agent 绑定的业务场景不存在")
    _validate_business_scenario(scenario)
    if explicit_scenario is not None and explicit_scenario.id != scenario.id:
        raise RuntimeError("显式 Agent 与业务场景不匹配")
    if (
        not scenario.tenant_id
        or not agent.tenant_id
        or str(scenario.tenant_id) != str(agent.tenant_id)
    ):
        raise RuntimeError("医保 Agent 与业务场景不属于同一租户")
    db.info["tenant_id"] = scenario.tenant_id
    return agent, scenario


def _sqlite_source(
    db,
    scenario: BusinessScenario,
    *,
    source_id: str | None = None,
) -> DataSource:
    if source_id:
        source = db.get(DataSource, source_id)
        if source is None:
            raise RuntimeError(f"找不到显式数据源：{source_id}")
        if str(source.scenario_id or "") != str(scenario.id):
            raise RuntimeError("显式数据源与业务场景不匹配")
        if source.type != "sqlite":
            raise RuntimeError("显式数据源不是 SQLite 数据源")
        if (
            not source.tenant_id
            or not scenario.tenant_id
            or str(source.tenant_id) != str(scenario.tenant_id)
        ):
            raise RuntimeError("显式数据源与业务场景不属于同一租户")
        sources = [source]
    else:
        sources = db.execute(
            select(DataSource).where(
                DataSource.scenario_id == scenario.id,
                DataSource.type == "sqlite",
            )
        ).scalars().all()
        sources = [
            source
            for source in sources
            if str(source.tenant_id or "") == str(scenario.tenant_id or "")
        ]
    candidates = [
        item
        for item in sources
        if _source_tables(Path(str((item.config or {}).get("path") or "")))
        & {"项目明细表", "费用明细表", "收费明细表"}
    ]
    if not candidates:
        raise RuntimeError("医保场景没有包含“项目明细表”的 SQLite 数据源")
    if len(candidates) > 1:
        raise RuntimeError(
            f"医保场景存在 {len(candidates)} 个可审计 SQLite 数据源；请显式传入 source_id"
        )
    return candidates[0]


def _source_tables(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    resolved = path.expanduser().resolve()
    with closing(sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro",
        uri=True,
    )) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        }


def _domain_source_plan(connection: sqlite3.Connection) -> dict[str, Any]:
    objects = {
        str(row[1]): {
            "type": str(row[0]),
            "table_name": str(row[2] or ""),
            "sql": str(row[3] or ""),
        }
        for row in connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','view','index')"
        )
    }
    table_names = {
        name
        for name, item in objects.items()
        if item["type"] in {"table", "view"}
    }
    encounter_table = next(
        (name for name in ("就诊表", "就诊信息表", "就诊记录表") if name in table_names),
        None,
    )
    charge_table = next(
        (name for name in ("项目明细表", "费用明细表", "收费明细表") if name in table_names),
        None,
    )
    if not encounter_table or not charge_table:
        raise RuntimeError("医保数据源缺少就诊表或项目明细表")
    encounter_columns = _table_columns(connection, encounter_table)
    charge_columns = _table_columns(connection, charge_table)
    facility_id = _first_column(encounter_columns, "定点医药机构编号", "医疗机构编号")
    facility_name = _first_column(encounter_columns, "定点医药机构名称", "医疗机构名称")
    service_code = _first_column(charge_columns, "医保目录编码", "医疗目录编码", "项目编码")
    service_name = _first_column(charge_columns, "医保目录名称", "医疗目录名称", "项目名称")
    if not all((facility_id, facility_name, service_code, service_name)):
        raise RuntimeError("医保数据源缺少生成领域视图所需的机构或服务项目字段")
    hospital_level = _aggregate_or_null(encounter_columns, "医院等级")
    insurance_region = _aggregate_or_null(
        encounter_columns, "定点归属医保区划", "参保所属医保区划"
    )
    catalog_category = _aggregate_or_null(charge_columns, "目录类别", "医保目录类别")
    charge_category = _aggregate_or_null(
        charge_columns, "医疗收费项目类别", "收费项目类别"
    )
    specification = _aggregate_or_null(charge_columns, "规格", "药品规格")
    unit_price = _first_column(charge_columns, "单价", "项目单价")
    reference_price = (
        f"MAX(CAST({_quote_identifier(unit_price)} AS REAL))" if unit_price else "NULL"
    )
    view_selects = {
        "医疗机构视图": f"""
            SELECT
                {_quote_identifier(facility_id)} AS "定点医药机构编号",
                MAX({_quote_identifier(facility_name)}) AS "定点医药机构名称",
                {hospital_level} AS "医院等级",
                {insurance_region} AS "定点归属医保区划"
            FROM {_quote_identifier(encounter_table)}
            WHERE {_quote_identifier(facility_id)} IS NOT NULL
              AND TRIM(CAST({_quote_identifier(facility_id)} AS TEXT)) <> ''
            GROUP BY {_quote_identifier(facility_id)}
        """.strip(),
        "医保服务项目视图": f"""
            SELECT
                {_quote_identifier(service_code)} AS "医保目录编码",
                MAX({_quote_identifier(service_name)}) AS "医保目录名称",
                {catalog_category} AS "目录类别",
                {charge_category} AS "医疗收费项目类别",
                {specification} AS "规格",
                {reference_price} AS "参考单价"
            FROM {_quote_identifier(charge_table)}
            WHERE {_quote_identifier(service_code)} IS NOT NULL
              AND TRIM(CAST({_quote_identifier(service_code)} AS TEXT)) <> ''
            GROUP BY {_quote_identifier(service_code)}
        """.strip(),
    }
    indexes: list[tuple[str, list[str], str]] = [
        (charge_table, [str(service_name)], "idx_medical_audit_charge_service")
    ]
    encounter_join = _first_column(charge_columns, "就诊ID", "就诊编号")
    if encounter_join:
        indexes.append(
            (charge_table, [encounter_join], "idx_medical_audit_charge_encounter")
        )
    encounter_id = _first_column(encounter_columns, "就诊ID", "就诊编号")
    if encounter_id:
        indexes.append(
            (encounter_table, [encounter_id], "idx_medical_audit_encounter_id")
        )
    return {
        "objects": objects,
        "view_selects": view_selects,
        "indexes": tuple(indexes),
    }


def _normalized_sql(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _historic_domain_view_sqls() -> dict[str, str]:
    return {
        "医疗机构视图": """
            CREATE VIEW "医疗机构视图" AS
            SELECT
                "定点医药机构编号",
                MAX("定点医药机构名称") AS "定点医药机构名称",
                MAX("医院等级") AS "医院等级",
                MAX("定点归属医保区划") AS "定点归属医保区划"
            FROM "就诊表"
            WHERE "定点医药机构编号" IS NOT NULL
              AND TRIM(CAST("定点医药机构编号" AS TEXT)) <> ''
            GROUP BY "定点医药机构编号"
        """,
        "医保服务项目视图": """
            CREATE VIEW "医保服务项目视图" AS
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
            GROUP BY "医保目录编码"
        """,
    }


def _preflight_source_schema(
    source: DataSource,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    path = Path(str((source.config or {}).get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"医保 SQLite 数据源不存在：{path}")
    manager = (
        nullcontext(connection)
        if connection is not None
        else closing(sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        ))
    )
    with manager as source_connection:
        plan = _domain_source_plan(source_connection)
        objects = plan["objects"]
        historic_sql = _historic_domain_view_sqls()
        for view_name, select_sql in plan["view_selects"].items():
            existing = objects.get(view_name)
            if existing is None:
                continue
            if existing["type"] != "view":
                raise RuntimeError(
                    f"源对象“{view_name}”不是恢复包可认领的领域视图"
                )
            current_sql = (
                f"CREATE VIEW {_quote_identifier(view_name)} AS {select_sql}"
            )
            accepted = {
                _normalized_sql(current_sql),
                _normalized_sql(
                    current_sql.replace("CREATE VIEW ", "CREATE VIEW IF NOT EXISTS ", 1)
                ),
                _normalized_sql(historic_sql[view_name]),
                _normalized_sql(
                    historic_sql[view_name].replace(
                        "CREATE VIEW ", "CREATE VIEW IF NOT EXISTS ", 1
                    )
                ),
            }
            if _normalized_sql(existing["sql"]) not in accepted:
                raise RuntimeError(
                    f"源视图“{view_name}”是未标记且不符合历史精确契约的现有资源"
                )
        for table_name, columns, index_name in plan["indexes"]:
            equivalent = False
            for row in source_connection.execute(
                f"PRAGMA index_list({_quote_identifier(table_name)})"
            ):
                existing_columns = [
                    str(item[2])
                    for item in source_connection.execute(
                        f"PRAGMA index_info({_quote_identifier(str(row[1]))})"
                    )
                ]
                if existing_columns[: len(columns)] == columns:
                    equivalent = True
                    break
            if not equivalent and index_name in objects:
                raise RuntimeError(
                    f"源索引名“{index_name}”已被其他资源占用，拒绝执行 DDL"
                )


@dataclass(frozen=True)
class _SourceDDLChange:
    object_type: str
    name: str
    sql: str


def _ensure_domain_views(
    source: DataSource,
    *,
    connection: sqlite3.Connection | None = None,
) -> tuple[_SourceDDLChange, ...]:
    path = Path(str((source.config or {}).get("path") or "")).resolve()
    if not path.is_file():
        raise RuntimeError(f"医保 SQLite 数据源不存在：{path}")
    owns_connection = connection is None
    manager = (
        closing(sqlite3.connect(path))
        if owns_connection
        else nullcontext(connection)
    )
    with manager as connection:
        plan = _domain_source_plan(connection)
        changes: list[_SourceDDLChange] = []
        for view_name, select_sql in plan["view_selects"].items():
            existed = view_name in plan["objects"]
            connection.execute(
                f"CREATE VIEW IF NOT EXISTS {_quote_identifier(view_name)} AS\n{select_sql}"
            )
            if not existed:
                changes.append(_source_ddl_change(connection, "view", view_name))
        for table_name, columns, index_name in plan["indexes"]:
            if _ensure_index_prefix(connection, table_name, columns, index_name):
                changes.append(_source_ddl_change(connection, "index", index_name))
        if owns_connection:
            connection.commit()
        return tuple(changes)


def _source_ddl_change(
    connection: sqlite3.Connection,
    object_type: str,
    name: str,
) -> _SourceDDLChange:
    row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None or str(row[0]) != object_type or not str(row[1] or "").strip():
        raise RuntimeError(f"无法记录本次新建的医保源对象：{name}")
    return _SourceDDLChange(object_type=object_type, name=name, sql=str(row[1]))


def _compensate_source_ddl(
    source: DataSource,
    changes: tuple[_SourceDDLChange, ...],
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    if not changes:
        return
    path = Path(str((source.config or {}).get("path") or "")).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError(f"医保 SQLite 数据源不存在，无法补偿：{path}")
    manager = (
        closing(sqlite3.connect(path))
        if connection is None
        else nullcontext(connection)
    )
    with manager as source_connection:
        if not source_connection.in_transaction:
            source_connection.execute("BEGIN IMMEDIATE")
        try:
            for change in reversed(changes):
                row = source_connection.execute(
                    "SELECT type, sql FROM sqlite_master WHERE name = ?",
                    (change.name,),
                ).fetchone()
                if row is None:
                    continue
                if (
                    str(row[0]) != change.object_type
                    or _normalized_sql(row[1]) != _normalized_sql(change.sql)
                ):
                    raise RuntimeError(
                        f"源对象“{change.name}”在补偿前已变化，拒绝自动删除"
                    )
                keyword = "INDEX" if change.object_type == "index" else "VIEW"
                source_connection.execute(
                    f"DROP {keyword} {_quote_identifier(change.name)}"
                )
            source_connection.commit()
        except Exception:
            source_connection.rollback()
            raise


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        )
    }


def _first_column(columns: set[str], *candidates: str) -> str | None:
    return next((candidate for candidate in candidates if candidate in columns), None)


def _aggregate_or_null(columns: set[str], *candidates: str) -> str:
    column = _first_column(columns, *candidates)
    return f"MAX({_quote_identifier(column)})" if column else "NULL"


def _ensure_index_prefix(
    connection: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    index_name: str,
) -> bool:
    for row in connection.execute(
        f"PRAGMA index_list({_quote_identifier(table_name)})"
    ):
        existing_columns = [
            str(item[2])
            for item in connection.execute(
                f"PRAGMA index_info({_quote_identifier(str(row[1]))})"
            )
        ]
        if existing_columns[: len(columns)] == columns:
            return False
    joined = ", ".join(_quote_identifier(column) for column in columns)
    connection.execute(
        f"CREATE INDEX IF NOT EXISTS {_quote_identifier(index_name)} "
        f"ON {_quote_identifier(table_name)} ({joined})"
    )
    return True


def _upsert_entities(db, scenario: BusinessScenario) -> dict[str, OntologyEntity]:
    claims = _domain_entity_claims(db, scenario)
    result: dict[str, OntologyEntity] = {}
    for spec in ENTITY_SPECS:
        entity = claims[spec["name"]]
        if entity is None:
            entity = OntologyEntity(
                scenario_id=scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
            )
            db.add(entity)
            db.flush()
        entity.name = spec["name"]
        entity.api_name = spec["api_name"]
        entity.namespace = NAMESPACE
        entity.lifecycle_status = "active"
        entity.description = _marked_description(spec["description"])
        entity.icon = spec["icon"]
        entity.color = spec["color"]
        entity.is_abstract = False
        entity.state_property = spec.get("state_property", "")
        properties = list(entity.properties or [])
        for property_spec in spec["properties"]:
            prop = _identity_match(
                properties,
                name=property_spec["name"],
                api_name=property_spec["api_name"],
                label=f"对象类型“{entity.name}”的属性",
            )
            if prop is None:
                prop = OntologyProperty(
                    entity_id=entity.id,
                    name=property_spec["name"],
                    api_name=property_spec["api_name"],
                )
                db.add(prop)
                properties.append(prop)
            prop.name = property_spec["name"]
            prop.api_name = property_spec["api_name"]
            prop.data_type = property_spec["data_type"]
            prop.description = _marked_description(
                property_spec.get("description", "")
            )
            prop.is_key = bool(property_spec.get("is_key"))
            prop.is_title = bool(property_spec.get("is_title"))
            prop.is_required = bool(property_spec.get("is_required"))
            prop.is_sensitive = bool(property_spec.get("is_sensitive"))
            prop.is_enum = bool(property_spec.get("is_enum"))
            prop.enum_values = list(property_spec.get("enum_values") or [])
            prop.default_value = ""
            prop.constraints = {}
        result[entity.name] = entity
    db.flush()
    return result


def _upsert_mappings(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    entities: dict[str, OntologyEntity],
    *,
    connection: sqlite3.Connection | None = None,
) -> dict[str, DataMapping]:
    contracts = _resolved_mapping_contracts(source, connection=connection)
    target_entity_ids = {str(entity.id) for entity in entities.values()}
    existing_by_entity: dict[str, list[DataMapping]] = {}
    for item in db.scalars(
        select(DataMapping).where(
            DataMapping.scenario_id == scenario.id,
            DataMapping.entity_id.in_(target_entity_ids),
        )
    ):
        existing_by_entity.setdefault(item.entity_id, []).append(item)
    result: dict[str, DataMapping] = {}
    for spec in MAPPING_SPECS:
        entity = entities[spec["entity"]]
        contract = contracts[entity.name]
        table_name = contract["table_name"]
        column_map = contract["column_map"]
        transform_rules = contract["transform_rules"]
        status = contract["status"]
        last_error = contract["last_error"]
        candidates = existing_by_entity.get(entity.id, [])
        if len(candidates) > 1:
            raise RuntimeError(f"对象“{entity.name}”的数据映射不唯一，拒绝自动认领")
        mapping = candidates[0] if candidates else None
        if mapping is not None:
            if str(mapping.data_source_id) != str(source.id):
                raise RuntimeError(
                    f"对象“{entity.name}”已存在指向其他数据源的映射；"
                    "恢复包不会迁移或覆盖跨数据源用户映射"
                )
            _assert_mapping_owned(mapping, entity, contract)
        if mapping is None:
            mapping = DataMapping(
                scenario_id=scenario.id,
                entity_id=entity.id,
                data_source_id=source.id,
            )
            db.add(mapping)
            db.flush()
        mapping.table_name = table_name
        mapping.column_map = column_map
        mapping.transform_rules = transform_rules
        mapping.status = status
        mapping.last_error = last_error
        mapping.environment_status = {
            **(mapping.environment_status or {}),
            MAPPING_PROVENANCE_KEY: MAPPING_PROVENANCE_VALUE,
            "dev": {"status": mapping.status, "last_error": mapping.last_error},
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
    existing = list(db.scalars(
        select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
    ))
    for spec in RELATION_SPECS:
        relation = _identity_match(
            existing,
            name=spec["name"],
            api_name=spec["api_name"],
            label="医保恢复包关系类型",
        )
        relation_was_marked = relation is not None and _has_marker(relation)
        if relation is None:
            relation = OntologyRelation(
                scenario_id=scenario.id,
                name=spec["name"],
                source_entity_id=entities[spec["source"]].id,
                target_entity_id=entities[spec["target"]].id,
            )
            db.add(relation)
            existing.append(relation)
            db.flush()
        relation.name = spec["name"]
        relation.api_name = spec["api_name"]
        relation.namespace = NAMESPACE
        relation.source_entity_id = entities[spec["source"]].id
        relation.target_entity_id = entities[spec["target"]].id
        relation.source_display_name = spec["source_display_name"]
        relation.source_api_name = spec["source_api_name"]
        relation.target_display_name = spec["target_display_name"]
        relation.target_api_name = spec["target_api_name"]
        relation.relation_type = spec["relation_type"]
        relation.storage_kind = "foreign_key" if spec.get("mapping_mode") else "none"
        relation.constraints = {}
        relation.description = _marked_description(_relation_description(spec))
        mode = spec.get("mapping_mode")
        if not mode:
            continue
        source_mapping = mappings[spec["source"]]
        target_mapping = mappings[spec["target"]]
        bindings = list(db.scalars(
            select(RelationDataMapping).where(
                RelationDataMapping.relation_id == relation.id
            )
        ))
        if len(bindings) > 1:
            raise RuntimeError(f"关系“{relation.name}”的数据映射不唯一")
        binding = bindings[0] if bindings else None
        contract = _relation_mapping_contract(
            spec,
            scenario,
            source,
            relation,
            source_mapping,
            target_mapping,
        )
        if binding is not None:
            _assert_relation_mapping_owned(
                binding,
                relation,
                contract,
                relation_was_marked=relation_was_marked,
            )
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
        for field, expected in contract.items():
            setattr(binding, field, expected)
    db.flush()


def _retire_recovery_owned_legacy_workflows(db, scenario: BusinessScenario) -> int:
    retired = 0
    for workflow in db.scalars(
        select(OntologyWorkflow).where(OntologyWorkflow.scenario_id == scenario.id)
    ):
        recovery_owned = (
            str(workflow.id) in LEGACY_WORKFLOW_IDS
            or _has_marker(workflow, WORKFLOW_RETIREMENT_MARKER)
        )
        if not recovery_owned:
            continue
        _assert_workflow_owned(workflow)
        workflow.description = _marked_description(
            workflow.description or "已由医保领域本体恢复包停用的旧演示工作流。",
            WORKFLOW_RETIREMENT_MARKER,
        )
        if workflow.enabled or workflow.status != "disabled":
            workflow.enabled = False
            workflow.status = "disabled"
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
        entity.description = _marked_description(
            entity.description,
            LEGACY_RETIREMENT_MARKER,
        )
        if (entity.lifecycle_status or "active") != "deprecated":
            entity.lifecycle_status = "deprecated"
            retired += 1
    return retired


def _verify_platform_commit_after_error(
    db,
    *,
    agent_id: str,
    scenario_id: str,
    source_id: str,
    attempt_id: str,
) -> str:
    """Resolve an uncertain PostgreSQL commit through an independent session.

    A complete recovery contract is safe to treat as committed only when every
    target mapping carries this attempt's unique marker. No marker from this
    attempt proves that the transaction did not become visible after the
    original Session was rolled back. Partial state or any verification failure
    stays unknown and therefore must never trigger destructive source-DDL
    compensation.
    """

    try:
        bind = db.get_bind()
        verification_bind = getattr(bind, "engine", bind)
        with OrmSession(
            bind=verification_bind,
            autoflush=False,
            expire_on_commit=False,
        ) as verification_db:
            agent = verification_db.get(Agent, agent_id)
            scenario = verification_db.get(BusinessScenario, scenario_id)
            source = verification_db.get(DataSource, source_id)
            if agent is None or scenario is None or source is None:
                return PLATFORM_COMMIT_UNKNOWN
            tenant_id = str(scenario.tenant_id or "")
            if (
                not tenant_id
                or str(agent.tenant_id or "") != tenant_id
                or str(source.tenant_id or "") != tenant_id
                or str(agent.scenario_id or "") != scenario_id
                or str(source.scenario_id or "") != scenario_id
            ):
                return PLATFORM_COMMIT_UNKNOWN

            # Scan this attempt's durable evidence independently of mutable
            # entity identity. After the platform mutex is released, a normal
            # writer may rename a committed entity before verification; that
            # must degrade to UNKNOWN rather than look like an absent commit.
            scenario_mappings = list(verification_db.scalars(
                select(DataMapping).where(
                    DataMapping.scenario_id == scenario_id
                )
            ))
            attempt_marked_mappings = [
                mapping
                for mapping in scenario_mappings
                if dict(mapping.environment_status or {}).get(
                    MAPPING_ATTEMPT_KEY
                ) == attempt_id
            ]
            all_entities = list(verification_db.scalars(
                select(OntologyEntity).where(
                    OntologyEntity.scenario_id == scenario_id
                )
            ))
            target_entities = [
                entity
                for entity in all_entities
                if any(
                    entity.name == spec["name"]
                    or entity.api_name == spec["api_name"]
                    for spec in ENTITY_SPECS
                )
            ]
            target_ids = {str(entity.id) for entity in target_entities}
            target_mappings = [
                mapping
                for mapping in scenario_mappings
                if str(mapping.entity_id) in target_ids
            ]

            entities_by_name: dict[str, OntologyEntity] = {}
            entities_complete = True
            for spec in ENTITY_SPECS:
                matches = [
                    entity
                    for entity in target_entities
                    if entity.name == spec["name"]
                    or entity.api_name == spec["api_name"]
                ]
                if (
                    len(matches) != 1
                    or matches[0].name != spec["name"]
                    or matches[0].api_name != spec["api_name"]
                    or matches[0].namespace != NAMESPACE
                    or not _has_marker(matches[0])
                ):
                    entities_complete = False
                    break
                entities_by_name[spec["name"]] = matches[0]

            mappings_complete = entities_complete
            if mappings_complete:
                contracts = _resolved_mapping_contracts(source, read_only=True)
                for spec in MAPPING_SPECS:
                    entity = entities_by_name[spec["entity"]]
                    candidates = [
                        mapping
                        for mapping in target_mappings
                        if str(mapping.entity_id) == str(entity.id)
                    ]
                    contract = contracts[spec["entity"]]
                    if len(candidates) != 1:
                        mappings_complete = False
                        break
                    mapping = candidates[0]
                    environment_status = dict(mapping.environment_status or {})
                    if not (
                        str(mapping.data_source_id or "") == source_id
                        and mapping.table_name == contract["table_name"]
                        and dict(mapping.column_map or {}) == contract["column_map"]
                        and dict(mapping.transform_rules or {})
                        == contract["transform_rules"]
                        and mapping.status == contract["status"]
                        and (mapping.last_error or "") == contract["last_error"]
                        and environment_status.get(MAPPING_PROVENANCE_KEY)
                        == MAPPING_PROVENANCE_VALUE
                        and environment_status.get(MAPPING_ATTEMPT_KEY)
                        == attempt_id
                        and environment_status.get("dev")
                        == {
                            "status": contract["status"],
                            "last_error": contract["last_error"],
                        }
                    ):
                        mappings_complete = False
                        break

            agent_complete = (
                (agent.system_prompt or "").count(PROMPT_MARKER) == 1
                and source_id in {
                    str(item) for item in (agent.data_source_ids or []) if str(item)
                }
                and int(agent.max_tokens or 0) >= 8192
            )
            scenario_complete = (
                (scenario.description or "").count(
                    SCENARIO_DESCRIPTION_APPENDIX
                )
                == 1
            )
            if (
                entities_complete
                and mappings_complete
                and agent_complete
                and scenario_complete
            ):
                return PLATFORM_COMMIT_CONFIRMED
            if not attempt_marked_mappings:
                return PLATFORM_COMMIT_NOT_COMMITTED
            return PLATFORM_COMMIT_UNKNOWN
    except Exception:
        return PLATFORM_COMMIT_UNKNOWN


def _acquire_upgrade_mutex(
    db,
    agent: Agent,
    scenario: BusinessScenario,
    source: DataSource,
) -> str:
    """Serialize recovery ownership and cross-database commit compensation.

    PostgreSQL gets a table lock that excludes ordinary mapping writes plus row
    locks for the agent, scenario, and connector. SQLite ignores ``FOR UPDATE``,
    so a no-op scenario write acquires its database-wide writer lock without
    changing ``updated_at``. Other dialects are rejected because this recovery
    script cannot prove an equivalent transactional exclusion contract there.
    The caller keeps the transaction open until source DDL is either adopted or
    compensated.
    """

    tenant_id = str(scenario.tenant_id or "")
    if not tenant_id or str(source.tenant_id or "") != tenant_id:
        raise RuntimeError("医保升级锁定范围缺少一致的租户身份")
    bind = db.get_bind()
    dialect_name = str(bind.dialect.name or "").lower()
    if dialect_name == "sqlite":
        result = db.execute(
            text(
                "UPDATE business_scenarios SET id = id "
                "WHERE id = :scenario_id AND tenant_id = :tenant_id"
            ),
            {"scenario_id": scenario.id, "tenant_id": tenant_id},
        )
        if result.rowcount != 1:
            raise RuntimeError("医保升级场景锁获取失败")
        return dialect_name
    if dialect_name != "postgresql":
        raise RuntimeError(
            f"医保恢复脚本不支持平台数据库方言“{dialect_name or 'unknown'}”；"
            "仅 SQLite 与 PostgreSQL 具备已验证的事务互斥契约"
        )
    # SHARE ROW EXCLUSIVE conflicts with the ROW EXCLUSIVE lock taken by
    # INSERT/UPDATE/DELETE, so ordinary mapping routes cannot add a second
    # cross-source mapping between ownership preflight and platform commit.
    db.execute(text(
        "LOCK TABLE data_mappings IN SHARE ROW EXCLUSIVE MODE"
    ))
    locked_agent = db.scalar(
        select(Agent.id)
        .where(
            Agent.id == agent.id,
            Agent.tenant_id == tenant_id,
            Agent.scenario_id == scenario.id,
        )
        .with_for_update()
    )
    locked_scenario = db.scalar(
        select(BusinessScenario.id)
        .where(
            BusinessScenario.id == scenario.id,
            BusinessScenario.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    locked_source = db.scalar(
        select(DataSource.id)
        .where(
            DataSource.id == source.id,
            DataSource.scenario_id == scenario.id,
            DataSource.tenant_id == tenant_id,
        )
        .with_for_update()
    )
    if (
        locked_agent is None
        or locked_scenario is None
        or locked_source is None
    ):
        raise RuntimeError("医保升级 Agent、场景或数据源锁获取失败")
    return dialect_name


def upgrade(
    db,
    *,
    agent_id: str | None = None,
    scenario_id: str | None = None,
    source_id: str | None = None,
) -> dict[str, Any]:
    attempt_id = uuid.uuid4().hex
    source: DataSource | None = None
    source_changes: tuple[_SourceDDLChange, ...] = ()
    source_committed = False
    platform_committed = False
    compensation_attempted = False
    platform_dialect = ""
    try:
        agent, scenario = _find_agent_and_scenario(
            db,
            agent_id=agent_id,
            scenario_id=scenario_id,
        )
        source = _sqlite_source(db, scenario, source_id=source_id)
        resolved_agent_id = str(agent.id)
        resolved_scenario_id = str(scenario.id)
        resolved_source_id = str(source.id)
        platform_dialect = _acquire_upgrade_mutex(db, agent, scenario, source)
        # Resolution can block behind another worker. Reload every mutable
        # resource after acquiring the mutex before making ownership decisions.
        db.expire_all()
        agent, scenario = _find_agent_and_scenario(
            db,
            agent_id=resolved_agent_id,
            scenario_id=resolved_scenario_id,
        )
        source = _sqlite_source(db, scenario, source_id=resolved_source_id)
        # Ownership is checked before any additive source-view migration.
        _preflight_recovery_ownership(db, scenario, agent=agent, source=source)
        source_path = Path(
            str((source.config or {}).get("path") or "")
        ).expanduser().resolve()
        with closing(sqlite3.connect(source_path)) as source_connection:
            source_connection.execute("BEGIN IMMEDIATE")
            try:
                source_changes = _ensure_domain_views(
                    source, connection=source_connection
                )
                entities = _upsert_entities(db, scenario)
                mappings = _upsert_mappings(
                    db,
                    scenario,
                    source,
                    entities,
                    connection=source_connection,
                )
                _upsert_relations(db, scenario, source, entities, mappings)
                retired = _retire_recovery_owned_legacy_workflows(db, scenario)
                deprecated = _deprecate_replaced_legacy_model(db, scenario)
                if PROMPT_MARKER not in (agent.system_prompt or ""):
                    agent.system_prompt = (
                        (agent.system_prompt or "").rstrip()
                        + "\n\n"
                        + PROMPT_APPENDIX
                    ).strip()
                bound_source_ids = [
                    str(item) for item in (agent.data_source_ids or []) if str(item)
                ]
                if source.id not in bound_source_ids:
                    agent.data_source_ids = [*bound_source_ids, source.id]
                agent.max_tokens = max(int(agent.max_tokens or 0), 8192)
                if SCENARIO_DESCRIPTION_APPENDIX not in (scenario.description or ""):
                    scenario.description = (
                        (scenario.description or "").rstrip()
                        + "\n"
                        + SCENARIO_DESCRIPTION_APPENDIX
                    ).strip()
                # This is intentionally the final platform mutation. A fresh
                # verifier can distinguish this attempt from a previously
                # complete core contract after commit acknowledgement is lost.
                for mapping in mappings.values():
                    mapping.environment_status = {
                        **(mapping.environment_status or {}),
                        MAPPING_ATTEMPT_KEY: attempt_id,
                    }
                db.flush()
                source_connection.commit()
                source_committed = True
                # Keep a source writer mutex while the platform transaction is
                # committed. A second worker cannot adopt these DDL objects
                # before this worker either owns them durably or compensates.
                source_connection.execute("BEGIN IMMEDIATE")
                try:
                    db.commit()
                    platform_committed = True
                except Exception as exc:
                    compensation_attempted = True
                    rollback_succeeded = True
                    try:
                        # Resolve any still-open transaction and release the
                        # platform mutex before checking from a new connection.
                        db.rollback()
                    except Exception as rollback_exc:  # pragma: no cover
                        rollback_succeeded = False
                        if hasattr(exc, "add_note"):
                            exc.add_note(
                                f"原平台会话回滚失败：{rollback_exc}"
                            )
                    verification = _verify_platform_commit_after_error(
                        db,
                        agent_id=resolved_agent_id,
                        scenario_id=resolved_scenario_id,
                        source_id=resolved_source_id,
                        attempt_id=attempt_id,
                    )
                    if (
                        verification == PLATFORM_COMMIT_NOT_COMMITTED
                        and not rollback_succeeded
                    ):
                        verification = PLATFORM_COMMIT_UNKNOWN
                    if verification == PLATFORM_COMMIT_CONFIRMED:
                        platform_committed = True
                        source_connection.rollback()
                    elif verification == PLATFORM_COMMIT_NOT_COMMITTED:
                        try:
                            if source_changes:
                                _compensate_source_ddl(
                                    source,
                                    source_changes,
                                    connection=source_connection,
                                )
                            else:
                                source_connection.rollback()
                        except Exception as cleanup_exc:  # pragma: no cover
                            if hasattr(exc, "add_note"):
                                exc.add_note(
                                    f"医保源库 DDL 补偿失败：{cleanup_exc}"
                                )
                        raise
                    else:
                        source_connection.rollback()
                        if hasattr(exc, "add_note"):
                            exc.add_note(
                                f"{platform_dialect or 'unknown'} 平台提交结果无法确认；"
                                "为避免删除可能已被"
                                "持久化契约认领的对象，已保留本次新增的医保源库对象，"
                                "可在确认平台状态后幂等重试。"
                            )
                        raise
                else:
                    source_connection.rollback()
            except Exception:
                if source_connection.in_transaction:
                    source_connection.rollback()
                raise
    except Exception as exc:
        if (
            source_committed
            and not platform_committed
            and not compensation_attempted
            and source is not None
        ):
            compensation_attempted = True
            try:
                _compensate_source_ddl(source, source_changes)
            except Exception as cleanup_exc:  # pragma: no cover - best effort note
                if hasattr(exc, "add_note"):
                    exc.add_note(f"医保源库 DDL 补偿失败：{cleanup_exc}")
        db.rollback()
        raise
    return {
        "attempt_id": attempt_id,
        "scenario_id": scenario.id,
        "source_id": source.id,
        "entities": sorted(entities),
        "mappings": sorted(mappings),
        "audit_version": "medical-audit-v1",
        "retired_workflows": retired,
        "deprecated_legacy_entities": deprecated,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="幂等升级一个明确的医保审计场景")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--scenario-id", required=True)
    parser.add_argument("--source-id", required=True)
    args = parser.parse_args()
    init_db()
    db = SessionLocal()
    try:
        result = upgrade(
            db,
            agent_id=args.agent_id,
            scenario_id=args.scenario_id,
            source_id=args.source_id,
        )
        print("医保审计领域本体升级完成")
        print(f"场景: {result['scenario_id']}")
        print(f"业务对象: {', '.join(result['entities'])}")
        print(f"已停用无效演示工作流: {result['retired_workflows']}")
        print(f"已非破坏性退役旧本体对象: {result['deprecated_legacy_entities']}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
