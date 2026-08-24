"""Install the governed annual-audit delivery slice for the bookkeeping demo.

This migration is intentionally additive and idempotent.  It repairs missing
semantic mappings for the AP001 audit objects, adds navigable project links,
uploads verified DOCX/XLSX templates, and exposes three confirmation-gated
template Actions.  It never re-enables arbitrary Agent SQL.

Run from ``backend``::

    python examples/upgrade_bookkeeping_audit.py
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import (
    Agent,
    ArtifactTemplate,
    ArtifactTemplateVersion,
    BucketFile,
    BusinessScenario,
    DataMapping,
    DataSource,
    OntologyAction,
    OntologyEntity,
    OntologyProperty,
    OntologyRelation,
    OntologyWorkflow,
    RelationDataMapping,
)
from app.services import (
    agent_capability_service,
    datasource_service,
    template_artifact_service,
    template_catalog_service,
)


AGENT_NAME = "AI 代理记账助手"
PROMPT_MARKER = "## 年度审计完整交付约定（模板动作版）"
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "bookkeeping_audit"


# name, api_name, data_type, physical column, key, title
PropertySpec = tuple[str, str, str, str, bool, bool]


AUDIT_OBJECT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "审计项目",
        "api_name": "audit_project",
        "description": "年度审计项目，是底稿、函证、调整、报告、报表、附注和复核的业务聚合根。",
        "table": "audit_project_view",
        "state_property": "项目状态",
        "properties": (
            ("项目ID", "project_id", "string", "project_id", True, False),
            ("被审计单位", "company_name", "string", "company_name", False, True),
            ("客户ID", "customer_id", "string", "customer_id", False, False),
            ("审计年度", "audit_year", "string", "audit_year", False, False),
            ("项目状态", "status", "string", "status", False, False),
            ("约定书签署日期", "engagement_date", "date", "sign_date", False, False),
            ("重要性水平", "materiality", "number", "materiality", False, False),
            ("风险等级", "risk_level", "string", "risk_level", False, False),
            ("审计意见类型", "opinion_type", "string", "opinion_type", False, False),
            ("主审会计师", "lead_auditor", "string", "lead_auditor", False, False),
            ("报告文号", "report_no", "string", "report_no", False, False),
        ),
    },
    {
        "name": "审计底稿",
        "api_name": "audit_workpaper",
        "description": "审计程序、证据、结论及编制复核状态的可追溯工作底稿。",
        "table": "audit_papers",
        "state_property": "复核状态",
        "properties": (
            ("底稿ID", "workpaper_id", "string", "paper_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("底稿类型", "workpaper_type", "string", "paper_type", False, True),
            ("科目名称", "account_name", "string", "account_name", False, False),
            ("底稿内容", "content", "text", "content", False, False),
            ("编制人", "preparer", "string", "preparer", False, False),
            ("编制日期", "prepared_at", "date", "prepare_date", False, False),
            ("复核状态", "review_status", "string", "review_status", False, False),
        ),
    },
    {
        "name": "函证",
        "api_name": "audit_confirmation",
        "description": "银行、往来方等外部函证的发函、回函和差异事实。",
        "table": "confirmations",
        "state_property": "回函状态",
        "properties": (
            ("函证ID", "confirmation_id", "string", "conf_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("函证对象", "target", "string", "target", False, True),
            ("函证类型", "confirmation_type", "string", "conf_type", False, False),
            ("发函日期", "sent_at", "date", "send_date", False, False),
            ("回函日期", "replied_at", "date", "reply_date", False, False),
            ("回函状态", "reply_status", "string", "reply_status", False, False),
            ("回函金额", "reply_amount", "number", "reply_amount", False, False),
            ("差异金额", "difference_amount", "number", "diff_amount", False, False),
        ),
    },
    {
        "name": "审计调整",
        "api_name": "audit_adjustment",
        "description": "审计识别并与管理层沟通的会计或税务调整事项。",
        "table": "audit_adjustments",
        "state_property": "处理状态",
        "properties": (
            ("调整ID", "adjustment_id", "string", "adj_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("调整科目", "account", "string", "account", False, True),
            ("调整方向", "direction", "string", "direction", False, False),
            ("调整金额", "amount", "number", "amount", False, False),
            ("调整原因", "reason", "text", "reason", False, False),
            ("客户是否接受", "accepted", "string", "accepted", False, False),
            ("处理状态", "status", "string", "status", False, False),
        ),
    },
    {
        "name": "审计报告",
        "api_name": "audit_report",
        "description": "正式审计意见、报告文号、签发状态及报告摘要。",
        "table": "audit_reports",
        "state_property": "状态",
        "properties": (
            ("报告ID", "report_id", "string", "report_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("报告文号", "report_no", "string", "report_no", False, True),
            ("报告类型", "report_type", "string", "report_type", False, False),
            ("审计意见类型", "opinion_type", "string", "opinion_type", False, False),
            ("报告日期", "report_date", "date", "report_date", False, False),
            ("编制人", "preparer", "string", "preparer", False, False),
            ("复核人", "reviewer", "string", "reviewer", False, False),
            ("复核日期", "review_date", "date", "review_date", False, False),
            ("状态", "status", "string", "status", False, False),
            ("内容摘要", "content_summary", "text", "content_summary", False, False),
        ),
    },
    {
        "name": "经审计财务报表",
        "api_name": "audited_financial_statement",
        "description": "审定后的资产负债表、利润表、现金流量表和所有者权益变动表摘要。",
        "table": "audited_statements",
        "state_property": "状态",
        "properties": (
            ("报表ID", "statement_id", "string", "statement_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("报表类型", "statement_type", "string", "statement_type", False, True),
            ("会计期间", "period", "string", "period", False, False),
            ("资产总计", "total_assets", "number", "total_assets", False, False),
            ("负债总计", "total_liabilities", "number", "total_liabilities", False, False),
            ("权益总计", "total_equity", "number", "total_equity", False, False),
            ("营业收入", "total_revenue", "number", "total_revenue", False, False),
            ("净利润", "net_profit", "number", "net_profit", False, False),
            ("状态", "status", "string", "status", False, False),
        ),
    },
    {
        "name": "报表附注",
        "api_name": "financial_statement_note",
        "description": "财务报表项目、会计政策、税项及其他披露的审定附注。",
        "table": "statement_notes",
        "state_property": "状态",
        "properties": (
            ("附注ID", "note_id", "string", "note_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("附注编号", "note_no", "string", "note_no", False, False),
            ("附注标题", "note_title", "string", "note_title", False, True),
            ("附注内容", "note_content", "text", "note_content", False, False),
            ("状态", "status", "string", "status", False, False),
        ),
    },
    {
        "name": "复核记录",
        "api_name": "audit_review",
        "description": "项目、部门和主任会计师三级复核的结论及关注事项。",
        "table": "review_records",
        "state_property": "状态",
        "properties": (
            ("复核ID", "review_id", "string", "review_id", True, False),
            ("项目ID", "project_id", "string", "project_id", False, False),
            ("复核级别", "review_level", "string", "review_level", False, True),
            ("复核人", "reviewer", "string", "reviewer", False, False),
            ("复核日期", "review_date", "date", "review_date", False, False),
            ("复核结果", "review_result", "string", "review_result", False, False),
            ("发现问题", "issues_found", "text", "issues_found", False, False),
            ("状态", "status", "string", "status", False, False),
        ),
    },
)


AUDIT_RELATION_SPECS: tuple[dict[str, str], ...] = (
    {"name": "客户委托审计", "api_name": "customer_audit_projects", "source": "客户", "target": "审计项目", "forward": "审计项目", "forward_api": "audit_projects", "reverse": "委托客户", "reverse_api": "customer", "foreign_key": "customer_id"},
    {"name": "项目编制底稿", "api_name": "project_workpapers", "source": "审计项目", "target": "审计底稿", "forward": "审计底稿", "forward_api": "workpapers", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
    {"name": "项目发函证", "api_name": "project_confirmations", "source": "审计项目", "target": "函证", "forward": "函证", "forward_api": "confirmations", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
    {"name": "项目产生调整", "api_name": "project_adjustments", "source": "审计项目", "target": "审计调整", "forward": "审计调整", "forward_api": "adjustments", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
    {"name": "项目出具审计报告", "api_name": "project_reports", "source": "审计项目", "target": "审计报告", "forward": "审计报告", "forward_api": "reports", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
    {"name": "项目形成经审计财务报表", "api_name": "project_audited_statements", "source": "审计项目", "target": "经审计财务报表", "forward": "经审计财务报表", "forward_api": "audited_statements", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
    {"name": "项目披露报表附注", "api_name": "project_statement_notes", "source": "审计项目", "target": "报表附注", "forward": "报表附注", "forward_api": "statement_notes", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
    {"name": "项目执行三级复核", "api_name": "project_reviews", "source": "审计项目", "target": "复核记录", "forward": "复核记录", "forward_api": "reviews", "reverse": "所属审计项目", "reverse_api": "audit_project", "foreign_key": "project_id"},
)


TEMPLATE_ACTION_SPECS: tuple[dict[str, str], ...] = (
    {
        "name": "生成年度审计报告（DOCX）",
        "template_name": "年度审计报告模板",
        "asset": "audit_report.docx",
        "output": "{{project.project_id}}-年度审计报告.docx",
        "description": "生成正式年度审计报告 DOCX。先查询审计项目、报告、调整、函证和复核记录，再严格按 input_schema 填写；未知事实标明“未提供”，不得编造。",
    },
    {
        "name": "生成财务报表附注（DOCX）",
        "template_name": "财务报表附注模板",
        "asset": "financial_statement_notes.docx",
        "output": "{{project.project_id}}-财务报表附注.docx",
        "description": "生成财务报表附注 DOCX。先查询报表附注和经审计财务报表；未取得的披露事项必须明确标为未提供或不适用。",
    },
    {
        "name": "生成经审计财务报表（XLSX）",
        "template_name": "经审计财务报表模板",
        "asset": "audited_financial_statements.xlsx",
        "output": "{{project.project_id}}-经审计财务报表.xlsx",
        "description": "生成包含资产负债表、利润表、现金流量表、所有者权益变动表和 Checks 的 XLSX；金额必须来自已查询数据，缺失项目留空并在说明中披露。",
    },
)


PROMPT_APPENDIX = f"""

{PROMPT_MARKER}
- 年度审计任务的业务键是用户给出的项目编号（例如 AP001）；直接按 `审计项目.项目ID` 查询，绝不要求用户提供内部 UUID。
- 对“完成某项目年度审计全部工作任务”，必须查询并核对：审计项目、被审计单位、审计底稿、函证、审计调整、审计报告、经审计财务报表、报表附注、三级复核。查询不到某类记录时说明缺口，不能把“工具缺 ID”当作业务结论。
- 正文至少给出项目概况、风险与重要性、底稿完成情况、函证差异、调整事项、四表关键数与资产负债勾稽、附注披露、三级复核、审计意见和数据质量例外。
- 正文完成后先调用 `list_actions`，再分别调用“生成年度审计报告（DOCX）”“生成财务报表附注（DOCX）”“生成经审计财务报表（XLSX）”。严格使用其 input_schema；不要调用旧的 save_deliverable。
- 三个模板动作会返回人工确认卡片。未生成三个预演/附件时，不得宣称“全部交付完成”；确认成功后附件必须保留原 DOCX/XLSX 格式。
- 对 AP001，需显式核验：资产总计 = 负债总计 + 权益总计；函证差异、两项审计调整、四类经审计报表、五项附注和三级复核均应进入结论。源数据日期存在跨期矛盾时要如实列为数据质量例外。
""".strip()


def _find_agent_and_scenario(db) -> tuple[Agent, BusinessScenario]:
    agent = db.execute(select(Agent).where(Agent.name == AGENT_NAME)).scalars().first()
    if not agent or not agent.scenario_id:
        raise RuntimeError(f"未找到已绑定场景的 Agent：{AGENT_NAME}")
    scenario = db.get(BusinessScenario, agent.scenario_id)
    if not scenario:
        raise RuntimeError("代理记账 Agent 绑定的业务场景不存在")
    db.info["tenant_id"] = scenario.tenant_id or agent.tenant_id
    return agent, scenario


def _sqlite_source(db, scenario: BusinessScenario) -> DataSource:
    for source in db.execute(
        select(DataSource).where(
            DataSource.scenario_id == scenario.id,
            DataSource.type == "sqlite",
        )
    ).scalars():
        path = Path(str((source.config or {}).get("path") or ""))
        if not path.is_file():
            continue
        with sqlite3.connect(path) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                )
            }
        required = {str(spec["table"]) for spec in AUDIT_OBJECT_SPECS} - {"audit_project_view"}
        if required.issubset(tables):
            return source
    raise RuntimeError("代理记账场景的数据源缺少年度审计示例表")


def _ensure_project_view(source: DataSource) -> None:
    path = Path(str((source.config or {}).get("path") or "")).resolve()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE VIEW IF NOT EXISTS audit_project_view AS
            SELECT p.*, c.company_name
            FROM audit_projects p
            LEFT JOIN customers c ON c.customer_id = p.customer_id;
            """
        )


def _upsert_audit_objects(
    db,
    scenario: BusinessScenario,
    source: DataSource,
) -> tuple[dict[str, OntologyEntity], dict[str, DataMapping]]:
    entity_by_name = {
        item.name: item
        for item in db.execute(
            select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
        ).scalars()
    }
    entities: dict[str, OntologyEntity] = {}
    mappings: dict[str, DataMapping] = {}
    for spec in AUDIT_OBJECT_SPECS:
        entity = entity_by_name.get(spec["name"])
        if entity is None:
            entity = OntologyEntity(scenario_id=scenario.id, name=spec["name"])
            db.add(entity)
            db.flush()
        entity.api_name = spec["api_name"]
        entity.namespace = "bookkeeping_audit"
        entity.description = spec["description"]
        entity.state_property = spec["state_property"]
        props = {item.name: item for item in entity.properties}
        column_map: dict[str, str] = {}
        for name, api_name, data_type, column, is_key, is_title in spec["properties"]:
            prop = props.get(name)
            if prop is None:
                prop = OntologyProperty(entity_id=entity.id, name=name)
                db.add(prop)
            prop.api_name = api_name
            prop.data_type = data_type
            prop.is_key = is_key
            prop.is_title = is_title
            prop.is_required = is_key
            column_map[name] = column
        mapping = db.execute(
            select(DataMapping).where(
                DataMapping.scenario_id == scenario.id,
                DataMapping.entity_id == entity.id,
                DataMapping.data_source_id == source.id,
            )
        ).scalars().first()
        if mapping is None:
            mapping = DataMapping(
                scenario_id=scenario.id,
                entity_id=entity.id,
                data_source_id=source.id,
            )
            db.add(mapping)
        mapping.table_name = spec["table"]
        mapping.column_map = column_map
        mapping.transform_rules = {}
        mapping.status = "ready"
        mapping.last_error = ""
        mapping.environment_status = {
            **(mapping.environment_status or {}),
            "dev": {"status": "ready", "last_error": ""},
        }
        entities[entity.name] = entity
        mappings[entity.name] = mapping
    db.flush()

    # The customer already exists in the base bookkeeping ontology.  Give it a
    # human title and include its mapping in the link-type catalog.
    customer = entity_by_name.get("客户")
    if not customer:
        raise RuntimeError("代理记账场景缺少“客户”对象")
    customer.api_name = "customer"
    customer_id = next((item for item in customer.properties if item.name == "客户ID"), None)
    if customer_id:
        customer_id.api_name = "customer_id"
        customer_id.is_title = False
    company_name = next(
        (
            item
            for item in customer.properties
            if item.name in {"企业名称", "客户名称"}
        ),
        None,
    )
    if company_name:
        company_name.is_title = True
        company_name.api_name = "company_name"
    customer_mapping = db.execute(
        select(DataMapping).where(
            DataMapping.entity_id == customer.id,
            DataMapping.data_source_id == source.id,
        )
    ).scalars().first()
    if not customer_mapping:
        raise RuntimeError("“客户”对象尚未绑定代理记账业务库")
    entities[customer.name] = customer
    mappings[customer.name] = customer_mapping
    db.flush()
    return entities, mappings


def _upsert_audit_relations(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    entities: dict[str, OntologyEntity],
    mappings: dict[str, DataMapping],
) -> None:
    relations = list(db.execute(
        select(OntologyRelation).where(OntologyRelation.scenario_id == scenario.id)
    ).scalars())
    for spec in AUDIT_RELATION_SPECS:
        source_entity = entities[spec["source"]]
        target_entity = entities[spec["target"]]
        relation = next(
            (
                item for item in relations
                if item.api_name == spec["api_name"]
                or (
                    item.source_entity_id == source_entity.id
                    and item.target_entity_id == target_entity.id
                )
            ),
            None,
        )
        if relation is None:
            relation = OntologyRelation(
                scenario_id=scenario.id,
                source_entity_id=source_entity.id,
                target_entity_id=target_entity.id,
            )
            db.add(relation)
            relations.append(relation)
        relation.name = spec["name"]
        relation.api_name = spec["api_name"]
        relation.namespace = "bookkeeping_audit"
        relation.source_entity_id = source_entity.id
        relation.target_entity_id = target_entity.id
        relation.source_display_name = spec["forward"]
        relation.source_api_name = spec["forward_api"]
        relation.target_display_name = spec["reverse"]
        relation.target_api_name = spec["reverse_api"]
        relation.storage_kind = "foreign_key"
        relation.relation_type = "1:N"
        relation.constraints = {}
        relation.description = f"{spec['source']}到{spec['target']}的双向可导航链接。"
        db.flush()
        binding = db.execute(
            select(RelationDataMapping).where(
                RelationDataMapping.relation_id == relation.id
            )
        ).scalars().first()
        if binding is None:
            binding = RelationDataMapping(
                scenario_id=scenario.id,
                relation_id=relation.id,
                source_mapping_id=mappings[spec["source"]].id,
                target_mapping_id=mappings[spec["target"]].id,
                mode="target_fk",
                data_source_id=source.id,
            )
            db.add(binding)
        binding.source_mapping_id = mappings[spec["source"]].id
        binding.target_mapping_id = mappings[spec["target"]].id
        binding.mode = "target_fk"
        binding.data_source_id = source.id
        binding.table_name = mappings[spec["target"]].table_name
        binding.foreign_key_column = spec["foreign_key"]
        binding.source_key_column = ""
        binding.target_key_column = ""
        binding.status = "ready"
        binding.last_error = ""
    db.flush()


def _template_bucket(db, scenario: BusinessScenario, agent: Agent) -> DataSource:
    buckets = db.execute(
        select(DataSource).where(
            DataSource.scenario_id == scenario.id,
            DataSource.type == "file_bucket",
        )
    ).scalars().all()
    if buckets:
        bucket = next((item for item in buckets if item.id in (agent.data_source_ids or [])), buckets[0])
    else:
        bucket = DataSource(
            tenant_id=scenario.tenant_id or agent.tenant_id,
            scenario_id=scenario.id,
            name="年度审计文档与附件",
            type="file_bucket",
            config={},
            status="ok",
        )
        db.add(bucket)
        db.flush()
    if not bucket.tenant_id:
        bucket.tenant_id = scenario.tenant_id or agent.tenant_id
    source_ids = list(agent.data_source_ids or [])
    if bucket.id not in source_ids:
        source_ids.append(bucket.id)
    agent.data_source_ids = source_ids
    return bucket


def _save_template(db, bucket: DataSource, path: Path) -> BucketFile:
    if not path.is_file():
        raise RuntimeError(f"缺少审计附件模板：{path}")
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    candidates = db.execute(
        select(BucketFile).where(
            BucketFile.data_source_id == bucket.id,
            BucketFile.filename.in_([path.name, f"{path.stem}-{digest[:8]}{path.suffix}"]),
        )
    ).scalars().all()
    for candidate in candidates:
        candidate_path = Path(candidate.stored_path)
        if candidate_path.is_file() and hashlib.sha256(candidate_path.read_bytes()).hexdigest() == digest:
            candidate.content_sha256 = digest
            candidate.status = "parsed"
            candidate.error = ""
            return candidate
    filename = path.name if not candidates else f"{path.stem}-{digest[:8]}{path.suffix}"
    saved = datasource_service.save_bucket_file(
        bucket,
        filename,
        content,
        mime=template_artifact_service.expected_mime(filename) or "application/octet-stream",
    )
    saved.status = "parsed"
    saved.error = ""
    db.add(saved)
    db.flush()
    return saved


def _describe_template_schema(schema: dict[str, Any]) -> dict[str, Any]:
    descriptions = {
        "project": "审计项目和被审计单位的已核验事实",
        "report": "审计报告、函证、调整、复核和披露结论",
        "statements": "经审计财务报表金额及勾稽结果",
    }
    for key, value in (schema.get("properties") or {}).items():
        if isinstance(value, dict) and key in descriptions:
            value["description"] = descriptions[key]
    return schema


def _upsert_template_actions(
    db,
    scenario: BusinessScenario,
    project: OntologyEntity,
    bucket: DataSource,
) -> list[OntologyAction]:
    actions: list[OntologyAction] = []
    for spec in TEMPLATE_ACTION_SPECS:
        template_file = _save_template(db, bucket, ASSET_DIR / spec["asset"])
        catalog_key = f"bookkeeping_{Path(spec['asset']).stem}"
        catalog_version = db.scalar(
            select(ArtifactTemplateVersion)
            .join(ArtifactTemplate, ArtifactTemplate.id == ArtifactTemplateVersion.template_id)
            .where(
                ArtifactTemplate.tenant_id == scenario.tenant_id,
                ArtifactTemplateVersion.bucket_file_id == template_file.id,
            )
        )
        catalog = db.get(ArtifactTemplate, catalog_version.template_id) if catalog_version else None
        if catalog is None:
            catalog = db.scalar(select(ArtifactTemplate).where(
                ArtifactTemplate.tenant_id == scenario.tenant_id,
                ArtifactTemplate.key == catalog_key,
            ))
        if catalog is None:
            catalog = template_catalog_service.create_from_bucket_file(
                db,
                tenant_id=str(scenario.tenant_id),
                template_file=template_file,
                template_source=bucket,
                scenario_id=scenario.id,
                name=spec["template_name"],
                purpose=spec["name"],
                description=spec["description"],
                key=catalog_key,
                version_note="AP001 年度审计模板初始版本",
            )
            catalog_version = db.get(ArtifactTemplateVersion, catalog.current_version_id)
        else:
            duplicate_key = db.scalar(select(ArtifactTemplate.id).where(
                ArtifactTemplate.tenant_id == scenario.tenant_id,
                ArtifactTemplate.key == catalog_key,
                ArtifactTemplate.id != catalog.id,
            ))
            if not duplicate_key:
                catalog.key = catalog_key
            catalog.name = spec["template_name"]
            catalog.purpose = spec["name"]
            catalog.description = spec["description"]
            catalog.status = "active"
            catalog_version = template_catalog_service.add_version_from_bucket_file(
                db,
                catalog,
                template_file=template_file,
                template_source=bucket,
                version_note="AP001 年度审计模板更新",
                set_current=True,
                allow_deprecated=True,
            )
        if catalog_version is None:
            raise RuntimeError(f"模板目录未生成版本：{spec['asset']}")
        pinned = template_catalog_service.pinned_action_config(
            catalog,
            catalog_version,
            target_data_source_id=bucket.id,
            output_filename=spec["output"],
        )
        variable_paths = set(catalog_version.placeholder_paths or [])
        variable_paths.update(
            template_artifact_service.referenced_variable_paths(spec["output"])
        )
        input_schema = template_artifact_service.merge_template_input_schema({}, variable_paths)
        input_schema = _describe_template_schema(input_schema)
        action = db.execute(
            select(OntologyAction).where(
                OntologyAction.scenario_id == scenario.id,
                OntologyAction.name == spec["name"],
            )
        ).scalars().first()
        if action is None:
            action = OntologyAction(
                scenario_id=scenario.id,
                entity_id=project.id,
                name=spec["name"],
            )
            db.add(action)
            db.flush()
        action.entity_id = project.id
        action.description = spec["description"]
        action.input_schema = input_schema
        action.executor_type = "template"
        action.executor_config = {
            **pinned,
            "template_variable_paths": sorted(variable_paths),
        }
        action.precondition = ""
        action.postcondition = ""
        action.enabled = True
        action.requires_confirmation = True
        action.idempotency_required = True
        action.permission_scope = "scenario"
        action.access_scope = "tenant"
        actions.append(action)
    db.flush()
    return actions


def _extend_explicit_action_scope(agent: Agent, actions: list[OntologyAction]) -> None:
    if agent.capability_scope is None:
        return
    scope = agent_capability_service.normalize_scope(
        agent.capability_scope,
        legacy_default=False,
        allow_all=True,
    )
    selected = list(scope["actions"]["selected_ids"])
    for action in actions:
        if action.id not in selected:
            selected.append(action.id)
    scope["actions"] = {"mode": "explicit", "selected_ids": selected}
    agent.capability_scope = scope


def _update_agent_contract(agent: Agent) -> None:
    prompt = agent.system_prompt or ""
    prompt = prompt.replace(
        "- 用 run_sql 查询具体数据（注意 LIMIT 控制行数）",
        "- 用 query_business_data 按业务对象、属性和关系查询；不要直接生成 SQL",
    )
    old_deliverable = (
        "6. **产出物附件**：当生成正式业务产出物（审计报告、经审计财务报表、报表附注、管理建议书、月度报告、函证、分析报告等）时，"
        "必须调用 save_deliverable 工具保存为附件，并在回答末尾以 Markdown 链接附上，格式：[📎 文件名.md](/api/data-sources/files/<file_id>/download)。"
        "用户可点击预览或下载。一次对话可生成多个附件（如审计报告+报表+附注分别保存）。"
    )
    prompt = prompt.replace(
        old_deliverable,
        "6. **产出物附件**：正式产出物必须使用 list_actions 中的 DOCX/XLSX 模板动作生成；不得调用旧的 save_deliverable。",
    )
    if PROMPT_MARKER not in prompt:
        prompt = (prompt.rstrip() + "\n\n" + PROMPT_APPENDIX).strip()
    agent.system_prompt = prompt
    agent.max_tokens = max(int(agent.max_tokens or 0), 8192)
    if "年度审计完整交付" not in (agent.description or ""):
        agent.description = (
            (agent.description or "").rstrip()
            + " 支持按审计项目业务编号完成年度审计完整交付，并生成正式 DOCX/XLSX 附件。"
        ).strip()


def _publish_workflow_input_contract(db, scenario: BusinessScenario) -> None:
    workflow = db.execute(
        select(OntologyWorkflow).where(
            OntologyWorkflow.scenario_id == scenario.id,
            OntologyWorkflow.name == "年度审计流程",
        )
    ).scalars().first()
    if not workflow:
        return
    workflow.trigger_config = {
        **(workflow.trigger_config or {}),
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {
                    "type": "string",
                    "description": "审计项目业务编号，例如 AP001",
                }
            },
            "required": ["project_id"],
            "additionalProperties": False,
        },
    }


def upgrade(db) -> dict[str, Any]:
    agent, scenario = _find_agent_and_scenario(db)
    source = _sqlite_source(db, scenario)
    _ensure_project_view(source)
    entities, mappings = _upsert_audit_objects(db, scenario, source)
    _upsert_audit_relations(db, scenario, source, entities, mappings)
    bucket = _template_bucket(db, scenario, agent)
    actions = _upsert_template_actions(db, scenario, entities["审计项目"], bucket)
    _extend_explicit_action_scope(agent, actions)
    _update_agent_contract(agent)
    _publish_workflow_input_contract(db, scenario)
    scenario.namespace = "bookkeeping_audit"
    if "年度审计域覆盖" not in (scenario.description or ""):
        scenario.description = (
            (scenario.description or "").rstrip()
            + "\n年度审计域覆盖审计项目、底稿、函证、调整、报告、经审计财务报表、报表附注和三级复核。"
        ).strip()
    db.commit()
    return {
        "scenario_id": scenario.id,
        "source_id": source.id,
        "bucket_id": bucket.id,
        "mapped_objects": sorted(name for name in mappings if name != "客户"),
        "template_actions": [action.name for action in actions],
    }


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        result = upgrade(db)
        print("代理记账年度审计交付升级完成")
        print(f"场景: {result['scenario_id']}")
        print(f"语义映射: {', '.join(result['mapped_objects'])}")
        print(f"模板动作: {', '.join(result['template_actions'])}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
