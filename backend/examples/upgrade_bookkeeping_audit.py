"""Legacy SQLite/local-file bookkeeping bootstrap/recovery helper.

This migration is intentionally additive and idempotent.  It repairs missing
semantic mappings for the AP001 audit objects, adds navigable project links,
uploads verified DOCX/XLSX templates, and exposes three confirmation-gated
template Actions.  It never re-enables arbitrary Agent SQL.  Its CLI is
deliberately disabled for the migrated MySQL/MinIO deployment; production
changes require a versioned MySQL/MinIO migration.

Run from ``backend``::

    python -m examples.upgrade_bookkeeping_audit
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import closing, nullcontext
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import select, text
from sqlalchemy.orm import Session as OrmSession

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
    RelationDataMapping,
)
from app.services import (
    agent_capability_service,
    datasource_service,
    template_artifact_service,
    template_catalog_service,
)


AGENT_NAME = "AI 代理记账助手"
SCENARIO_NAME = "代理记账业务"
PROMPT_MARKER = "## 年度审计完整交付约定（模板动作版）"
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "bookkeeping_audit"
RECOVERY_PACK_ID = "bookkeeping-audit:v1"
RECOVERY_MARKER = f"[recovery-pack:{RECOVERY_PACK_ID}]"
MAPPING_MARKER_KEY = "_recovery_pack"
MAPPING_ATTEMPT_KEY = "__bookkeeping_audit_upgrade_attempt__"
PLATFORM_COMMIT_CONFIRMED = "committed"
PLATFORM_COMMIT_NOT_COMMITTED = "not_committed"
PLATFORM_COMMIT_UNKNOWN = "unknown"
PROJECT_VIEW_NAME = "audit_project_view"
PROJECT_VIEW_SQL = """
SELECT p.*, c.company_name
FROM audit_projects p
LEFT JOIN customers c ON c.customer_id = p.customer_id
""".strip()
LEGACY_SCENARIO_NAMESPACES = {"", "default", "bookkeeping", "bookkeeping_audit"}


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


T = TypeVar("T")


def _one_or_none(values: Iterable[T], *, label: str) -> T | None:
    items = list(values)
    if len(items) > 1:
        identifiers = "、".join(str(getattr(item, "id", "")) for item in items)
        raise RuntimeError(f"{label}存在多个候选（{identifiers}），请显式指定 ID")
    return items[0] if items else None


def _marked_description(description: str) -> str:
    lines = [
        line for line in str(description or "").rstrip().splitlines()
        if line.strip() != RECOVERY_MARKER
    ]
    lines.append(RECOVERY_MARKER)
    return "\n".join(lines).strip()


def _has_marker(value: Any) -> bool:
    return RECOVERY_MARKER in {
        line.strip() for line in str(getattr(value, "description", "") or "").splitlines()
    }


def _identity_match(
    values: Iterable[T],
    *,
    name: str,
    api_name: str,
    label: str,
) -> T | None:
    matches = {
        str(getattr(item, "id", "")): item
        for item in values
        if getattr(item, "name", None) == name
        or getattr(item, "api_name", None) == api_name
    }
    if len(matches) > 1:
        raise RuntimeError(
            f"{label}身份冲突：name={name!r} 与 api_name={api_name!r} 指向不同记录"
        )
    return next(iter(matches.values()), None)


def _named_match(values: Iterable[T], *, name: str, label: str) -> T | None:
    return _one_or_none(
        (item for item in values if getattr(item, "name", None) == name),
        label=f"{label}“{name}”",
    )


def _normalized_sql(value: str) -> str:
    return " ".join(str(value or "").strip().rstrip(";").split()).lower()


def _expected_project_view_sql() -> str:
    return _normalized_sql(f"CREATE VIEW {PROJECT_VIEW_NAME} AS {PROJECT_VIEW_SQL}")


def _find_agent_and_scenario(
    db,
    *,
    agent_id: str | None = None,
    scenario_id: str | None = None,
) -> tuple[Agent, BusinessScenario]:
    if agent_id:
        agent = db.get(Agent, agent_id)
        if agent is None:
            raise RuntimeError(f"找不到 Agent：{agent_id}")
        if agent.name != AGENT_NAME:
            raise RuntimeError(f"Agent {agent_id} 不是目标 Agent“{AGENT_NAME}”")
    else:
        statement = select(Agent).where(Agent.name == AGENT_NAME)
        if scenario_id:
            statement = statement.where(Agent.scenario_id == scenario_id)
        agent = _one_or_none(
            db.scalars(statement).all(),
            label=f"Agent“{AGENT_NAME}”",
        )
        if agent is None:
            raise RuntimeError(f"未找到 Agent“{AGENT_NAME}”")
    if not agent.scenario_id:
        raise RuntimeError(f"Agent {agent.id} 尚未绑定业务场景")

    resolved_scenario_id = scenario_id or agent.scenario_id
    scenario = db.get(BusinessScenario, resolved_scenario_id)
    if scenario is None:
        raise RuntimeError(f"找不到业务场景：{resolved_scenario_id}")
    if scenario.name != SCENARIO_NAME:
        raise RuntimeError(
            f"业务场景 {scenario.id} 不是目标场景“{SCENARIO_NAME}”"
        )
    if agent.scenario_id != scenario.id:
        raise RuntimeError(
            f"Agent {agent.id} 绑定场景 {agent.scenario_id}，与所选场景 {scenario.id} 不一致"
        )
    if not agent.tenant_id or agent.tenant_id != scenario.tenant_id:
        raise RuntimeError("Agent 与业务场景的租户绑定不一致")
    if str(scenario.namespace or "") not in LEGACY_SCENARIO_NAMESPACES:
        raise RuntimeError(
            f"业务场景命名空间 {scenario.namespace!r} 不是可安全迁移的旧版值"
        )
    if not isinstance(agent.data_source_ids, list):
        raise RuntimeError("Agent 的数据源绑定列表无效")
    db.info["tenant_id"] = scenario.tenant_id
    return agent, scenario


def _validate_source_binding(
    source: DataSource,
    *,
    scenario: BusinessScenario,
    agent: Agent,
    expected_type: str,
    label: str,
) -> None:
    if source.scenario_id != scenario.id:
        raise RuntimeError(f"{label} {source.id} 不属于所选场景")
    if source.tenant_id != scenario.tenant_id:
        raise RuntimeError(f"{label} {source.id} 与场景租户不一致")
    if source.type != expected_type:
        raise RuntimeError(f"{label} {source.id} 类型必须为 {expected_type}")
    if source.id not in {str(value) for value in (agent.data_source_ids or [])}:
        raise RuntimeError(f"{label} {source.id} 未绑定到所选 Agent")


def _sqlite_source(
    db,
    scenario: BusinessScenario,
    agent: Agent,
    *,
    source_id: str | None = None,
) -> DataSource:
    if source_id:
        source = db.get(DataSource, source_id)
        if source is None:
            raise RuntimeError(f"找不到结构化数据源：{source_id}")
    else:
        source = _one_or_none(
            db.scalars(select(DataSource).where(
                DataSource.scenario_id == scenario.id,
                DataSource.type == "sqlite",
            )).all(),
            label="代理记账 SQLite 数据源",
        )
        if source is None:
            raise RuntimeError("代理记账场景没有 SQLite 数据源")
    _validate_source_binding(
        source,
        scenario=scenario,
        agent=agent,
        expected_type="sqlite",
        label="结构化数据源",
    )
    _validate_sqlite_source_contract(source)
    return source


def _validate_sqlite_source_contract(
    source: DataSource,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    path = Path(str((source.config or {}).get("path") or ""))
    if not path.is_file():
        raise RuntimeError(f"结构化数据源文件不存在：{path}")
    manager = (
        closing(
            sqlite3.connect(
                f"file:{path.resolve().as_posix()}?mode=ro",
                uri=True,
            )
        )
        if connection is None
        else nullcontext(connection)
    )
    with manager as source_connection:
        source_objects = {
            str(row[0]): {"type": str(row[1]), "sql": str(row[2] or "")}
            for row in source_connection.execute(
                "SELECT name, type, sql FROM sqlite_master "
                "WHERE type IN ('table','view')"
            )
        }
        tables = set(source_objects)
        columns_by_table = {
            table: {
                str(row[0])
                for row in source_connection.execute(
                    "SELECT name FROM pragma_table_info(?)", (table,)
                )
            }
            for table in tables
        }
    project_view = source_objects.get(PROJECT_VIEW_NAME)
    if project_view is not None and (
        project_view["type"] != "view"
        or _normalized_sql(project_view["sql"]) != _expected_project_view_sql()
    ):
        raise RuntimeError(
            f"结构化数据源中的 {PROJECT_VIEW_NAME} 已被非恢复包契约对象占用"
        )
    required = (
        {str(spec["table"]) for spec in AUDIT_OBJECT_SPECS}
        - {PROJECT_VIEW_NAME}
    ) | {"audit_projects", "customers"}
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError(
            f"结构化数据源缺少年度审计表：{'、'.join(missing)}"
        )
    required_columns: dict[str, set[str]] = {
        "customers": {"customer_id", "company_name"},
        "audit_projects": {
            column
            for spec in AUDIT_OBJECT_SPECS
            if spec["table"] == "audit_project_view"
            for _name, _api_name, _type, column, _key, _title in spec["properties"]
            if column != "company_name"
        },
    }
    for spec in AUDIT_OBJECT_SPECS:
        if spec["table"] == PROJECT_VIEW_NAME:
            continue
        required_columns[str(spec["table"])] = {
            column for _name, _api_name, _type, column, _key, _title in spec["properties"]
        }
    if PROJECT_VIEW_NAME in tables:
        required_columns[PROJECT_VIEW_NAME] = {
            column
            for spec in AUDIT_OBJECT_SPECS
            if spec["table"] == PROJECT_VIEW_NAME
            for _name, _api_name, _type, column, _key, _title in spec["properties"]
        }
    column_gaps = {
        table: sorted(columns - columns_by_table.get(table, set()))
        for table, columns in required_columns.items()
        if columns - columns_by_table.get(table, set())
    }
    if column_gaps:
        details = "；".join(
            f"{table}: {','.join(columns)}"
            for table, columns in sorted(column_gaps.items())
        )
        raise RuntimeError(f"结构化数据源缺少年度审计字段：{details}")


def _ensure_project_view(
    source: DataSource,
    *,
    connection: sqlite3.Connection | None = None,
) -> bool:
    path = Path(str((source.config or {}).get("path") or "")).resolve()
    manager = (
        closing(sqlite3.connect(path))
        if connection is None
        else nullcontext(connection)
    )
    with manager as source_connection:
        if not source_connection.in_transaction:
            source_connection.execute("BEGIN IMMEDIATE")
        exists = source_connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
            (PROJECT_VIEW_NAME,),
        ).fetchone()
        if exists:
            return False
        source_connection.execute(
            f"CREATE VIEW {PROJECT_VIEW_NAME} AS {PROJECT_VIEW_SQL}"
        )
        if connection is None:
            source_connection.commit()
    return True


def _remove_project_view(
    source: DataSource,
    *,
    connection: sqlite3.Connection | None = None,
) -> None:
    path = Path(str((source.config or {}).get("path") or "")).resolve()
    manager = (
        closing(sqlite3.connect(path))
        if connection is None
        else nullcontext(connection)
    )
    with manager as source_connection:
        if not source_connection.in_transaction:
            source_connection.execute("BEGIN IMMEDIATE")
        row = source_connection.execute(
            "SELECT type, sql FROM sqlite_master WHERE name=?",
            (PROJECT_VIEW_NAME,),
        ).fetchone()
        if row is not None:
            if (
                str(row[0]) != "view"
                or _normalized_sql(row[1]) != _expected_project_view_sql()
            ):
                raise RuntimeError("审计项目视图在补偿前已变化，拒绝自动删除")
            source_connection.execute(f"DROP VIEW {PROJECT_VIEW_NAME}")
        source_connection.commit()


def _property_contract_matches(prop: OntologyProperty, spec: PropertySpec) -> bool:
    name, api_name, data_type, _column, is_key, is_title = spec
    return (
        prop.name == name
        and prop.api_name == api_name
        and prop.data_type == data_type
        and prop.is_key is is_key
        and prop.is_title is is_title
        and prop.is_required is is_key
        and prop.is_enum is False
        and prop.enum_values == []
        and prop.default_value in (None, "")
        and prop.constraints == {}
        and prop.is_sensitive is False
    )


def _legacy_entity_matches(entity: OntologyEntity, spec: dict[str, Any]) -> bool:
    if not (
        entity.name == spec["name"]
        and entity.api_name == spec["api_name"]
        and entity.namespace == "bookkeeping_audit"
        and entity.description == spec["description"]
        and entity.state_property == spec["state_property"]
        and (entity.lifecycle_status or "active") == "active"
        and entity.is_abstract is False
    ):
        return False
    properties = list(entity.properties)
    try:
        return all(
            (
                prop := _identity_match(
                    properties,
                    name=property_spec[0],
                    api_name=property_spec[1],
                    label=f"对象“{entity.name}”的属性",
                )
            ) is not None
            and _property_contract_matches(prop, property_spec)
            for property_spec in spec["properties"]
        )
    except RuntimeError:
        return False


def _mapping_has_marker(mapping: DataMapping) -> bool:
    states = mapping.environment_status or {}
    return (
        isinstance(states, dict)
        and states.get(MAPPING_MARKER_KEY) == RECOVERY_PACK_ID
    )


def _legacy_mapping_matches(
    mapping: DataMapping,
    *,
    scenario: BusinessScenario,
    entity: OntologyEntity,
    source: DataSource,
    table_name: str,
    column_map: dict[str, str],
) -> bool:
    states = mapping.environment_status or {}
    dev = states.get("dev") if isinstance(states, dict) else None
    return (
        mapping.scenario_id == scenario.id
        and mapping.entity_id == entity.id
        and mapping.data_source_id == source.id
        and mapping.data_source_binding_key == ""
        and mapping.data_source_binding_ref == {}
        and mapping.table_name == table_name
        and mapping.column_map == column_map
        and mapping.transform_rules == {}
        and mapping.status == "ready"
        and mapping.last_error == ""
        and isinstance(dev, dict)
        and dev.get("status") == "ready"
        and str(dev.get("last_error") or "") == ""
    )


def _customer_mapping_contract_matches(
    mapping: DataMapping,
    *,
    scenario: BusinessScenario,
    customer: OntologyEntity,
    source: DataSource,
) -> bool:
    customer_columns = dict(mapping.column_map or {})
    customer_property_names = {prop.name for prop in customer.properties}
    return (
        mapping.scenario_id == scenario.id
        and mapping.entity_id == customer.id
        and mapping.data_source_id == source.id
        and mapping.data_source_binding_key == ""
        and mapping.data_source_binding_ref == {}
        and mapping.table_name == "customers"
        and mapping.transform_rules == {}
        and mapping.status in {"unknown", "ok", "ready"}
        and set(customer_columns.values()) >= {"customer_id", "company_name"}
        and set(customer_columns) <= customer_property_names
        and any(
            prop.is_key and customer_columns.get(prop.name) == "customer_id"
            for prop in customer.properties
        )
    )


def _preflight_audit_mapping_ownership(
    db,
    scenario: BusinessScenario,
    source: DataSource,
    existing_entities: list[OntologyEntity],
) -> tuple[dict[str, DataMapping | None], OntologyEntity, DataMapping]:
    """Claim mappings by scenario/entity across every source before side effects."""

    target_entities: dict[str, OntologyEntity | None] = {}
    for spec in AUDIT_OBJECT_SPECS:
        target_entities[spec["name"]] = _identity_match(
            existing_entities,
            name=spec["name"],
            api_name=spec["api_name"],
            label="年度审计对象",
        )
    customer = _identity_match(
        existing_entities,
        name="客户",
        api_name="customer",
        label="基础对象“客户”",
    )
    if customer is None:
        raise RuntimeError("代理记账场景缺少“客户”对象")

    entity_ids = {
        str(entity.id)
        for entity in [*target_entities.values(), customer]
        if entity is not None
    }
    mappings_by_entity: dict[str, list[DataMapping]] = {}
    if entity_ids:
        for mapping in db.scalars(
            select(DataMapping).where(
                DataMapping.scenario_id == scenario.id,
                DataMapping.entity_id.in_(entity_ids),
            )
        ):
            mappings_by_entity.setdefault(str(mapping.entity_id), []).append(mapping)

    claims: dict[str, DataMapping | None] = {}
    for spec in AUDIT_OBJECT_SPECS:
        entity = target_entities[spec["name"]]
        if entity is None:
            claims[spec["name"]] = None
            continue
        candidates = mappings_by_entity.get(str(entity.id), [])
        if len(candidates) > 1:
            raise RuntimeError(
                f"对象“{entity.name}”存在多个数据映射，拒绝自动认领"
            )
        mapping = candidates[0] if candidates else None
        if mapping is not None:
            if str(mapping.data_source_id) != str(source.id):
                raise RuntimeError(
                    f"对象“{entity.name}”已存在指向其他数据源的映射；"
                    "恢复包不会迁移或覆盖用户映射"
                )
            column_map = {
                property_spec[0]: property_spec[3]
                for property_spec in spec["properties"]
            }
            if not _mapping_has_marker(mapping) and not _legacy_mapping_matches(
                mapping,
                scenario=scenario,
                entity=entity,
                source=source,
                table_name=spec["table"],
                column_map=column_map,
            ):
                raise RuntimeError(
                    f"对象“{entity.name}”的数据映射未标记且不匹配旧版恢复契约"
                )
        claims[spec["name"]] = mapping

    customer_candidates = mappings_by_entity.get(str(customer.id), [])
    if len(customer_candidates) != 1:
        raise RuntimeError("基础对象“客户”的数据映射必须全源唯一")
    customer_mapping = customer_candidates[0]
    if str(customer_mapping.data_source_id) != str(source.id):
        raise RuntimeError("“客户”对象的数据映射未明确绑定所选代理记账业务库")
    if not _customer_mapping_contract_matches(
        customer_mapping,
        scenario=scenario,
        customer=customer,
        source=source,
    ):
        raise RuntimeError("基础“客户”对象的数据映射不满足只读年度审计契约")
    return claims, customer, customer_mapping


def _upsert_audit_objects(
    db,
    scenario: BusinessScenario,
    source: DataSource,
) -> tuple[dict[str, OntologyEntity], dict[str, DataMapping]]:
    existing_entities = list(db.scalars(
        select(OntologyEntity).where(OntologyEntity.scenario_id == scenario.id)
    ))
    mapping_claims, customer, customer_mapping = (
        _preflight_audit_mapping_ownership(
            db,
            scenario,
            source,
            existing_entities,
        )
    )
    entities: dict[str, OntologyEntity] = {}
    mappings: dict[str, DataMapping] = {}
    for spec in AUDIT_OBJECT_SPECS:
        entity = _identity_match(
            existing_entities,
            name=spec["name"],
            api_name=spec["api_name"],
            label="年度审计对象",
        )
        if entity is None:
            entity = OntologyEntity(
                scenario_id=scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
            )
            db.add(entity)
            db.flush()
            existing_entities.append(entity)
        elif not _has_marker(entity) and not _legacy_entity_matches(entity, spec):
            raise RuntimeError(
                f"年度审计对象“{spec['name']}”已被未标记资源占用；恢复包不会覆盖"
            )
        entity.api_name = spec["api_name"]
        entity.namespace = "bookkeeping_audit"
        entity.description = _marked_description(spec["description"])
        entity.state_property = spec["state_property"]
        entity.lifecycle_status = "active"
        entity.is_abstract = False
        props = list(db.scalars(
            select(OntologyProperty).where(OntologyProperty.entity_id == entity.id)
        ))
        column_map: dict[str, str] = {}
        for property_spec in spec["properties"]:
            name, api_name, data_type, column, is_key, is_title = property_spec
            prop = _identity_match(
                props,
                name=name,
                api_name=api_name,
                label=f"对象“{entity.name}”的属性",
            )
            if prop is None:
                prop = OntologyProperty(
                    entity_id=entity.id,
                    name=name,
                    api_name=api_name,
                )
                db.add(prop)
                props.append(prop)
            elif not _has_marker(prop) and not _property_contract_matches(
                prop, property_spec
            ):
                raise RuntimeError(
                    f"对象“{entity.name}”的属性“{name}”已被未标记资源占用"
                )
            prop.api_name = api_name
            prop.data_type = data_type
            prop.is_key = is_key
            prop.is_title = is_title
            prop.is_required = is_key
            prop.description = _marked_description(prop.description or "")
            column_map[name] = column
        mapping = mapping_claims.get(spec["name"])
        if mapping is None:
            mapping = DataMapping(
                scenario_id=scenario.id,
                entity_id=entity.id,
                data_source_id=source.id,
            )
            db.add(mapping)
            mapping_was_new = True
            legacy_mapping = False
        else:
            mapping_was_new = False
            legacy_mapping = not _mapping_has_marker(mapping)
            if legacy_mapping and not _legacy_mapping_matches(
                mapping,
                scenario=scenario,
                entity=entity,
                source=source,
                table_name=spec["table"],
                column_map=column_map,
            ):
                raise RuntimeError(
                    f"对象“{entity.name}”的数据映射未标记且不匹配旧版恢复契约"
                )
        mapping.table_name = spec["table"]
        mapping.column_map = column_map
        mapping.transform_rules = {}
        if mapping_was_new or legacy_mapping:
            mapping.status = "ready"
            mapping.last_error = ""
        states = dict(mapping.environment_status or {})
        dev = dict(states.get("dev") or {})
        if mapping_was_new or legacy_mapping or not dev:
            dev.update({"status": "ready", "last_error": ""})
        states["dev"] = dev
        states[MAPPING_MARKER_KEY] = RECOVERY_PACK_ID
        mapping.environment_status = {
            **states,
        }
        entities[entity.name] = entity
        mappings[entity.name] = mapping
    db.flush()

    # Customer is owned by the base bookkeeping model.  Validate and reuse it,
    # but never rewrite its identity or display metadata from this recovery pack.
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
        relation = _identity_match(
            relations,
            name=spec["name"],
            api_name=spec["api_name"],
            label="年度审计关系",
        )
        if relation is None:
            relation = OntologyRelation(
                scenario_id=scenario.id,
                name=spec["name"],
                api_name=spec["api_name"],
                source_entity_id=source_entity.id,
                target_entity_id=target_entity.id,
            )
            db.add(relation)
            relations.append(relation)
            legacy_relation = False
        else:
            legacy_relation = not _has_marker(relation)
            expected_description = f"{spec['source']}到{spec['target']}的双向可导航链接。"
            if legacy_relation and not (
                relation.name == spec["name"]
                and relation.api_name == spec["api_name"]
                and relation.namespace == "bookkeeping_audit"
                and relation.source_entity_id == source_entity.id
                and relation.target_entity_id == target_entity.id
                and relation.source_display_name == spec["forward"]
                and relation.source_api_name == spec["forward_api"]
                and relation.target_display_name == spec["reverse"]
                and relation.target_api_name == spec["reverse_api"]
                and relation.storage_kind == "foreign_key"
                and relation.relation_type == "1:N"
                and relation.constraints == {}
                and relation.description == expected_description
            ):
                raise RuntimeError(
                    f"年度审计关系“{spec['name']}”已被未标记资源占用"
                )
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
        relation.description = _marked_description(
            f"{spec['source']}到{spec['target']}的双向可导航链接。"
        )
        db.flush()
        binding = _one_or_none(
            db.scalars(
            select(RelationDataMapping).where(
                RelationDataMapping.relation_id == relation.id
            )
            ).all(),
            label=f"关系“{relation.name}”的物理绑定",
        )
        if binding is None:
            if legacy_relation:
                raise RuntimeError(
                    f"未标记的旧版关系“{relation.name}”缺少精确物理绑定，拒绝领养"
                )
            binding = RelationDataMapping(
                scenario_id=scenario.id,
                relation_id=relation.id,
                source_mapping_id=mappings[spec["source"]].id,
                target_mapping_id=mappings[spec["target"]].id,
                mode="target_fk",
                data_source_id=source.id,
            )
            db.add(binding)
        elif legacy_relation and not (
            binding.scenario_id == scenario.id
            and binding.source_mapping_id == mappings[spec["source"]].id
            and binding.target_mapping_id == mappings[spec["target"]].id
            and binding.mode == "target_fk"
            and binding.data_source_id == source.id
            and binding.data_source_binding_key == ""
            and binding.data_source_binding_ref == {}
            and binding.table_name == mappings[spec["target"]].table_name
            and binding.foreign_key_column == spec["foreign_key"]
            and binding.source_key_column == ""
            and binding.target_key_column == ""
            and binding.status == "ready"
            and binding.last_error == ""
        ):
            raise RuntimeError(
                f"未标记的旧版关系“{relation.name}”物理绑定不匹配恢复契约"
            )
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


def _template_bucket(
    db,
    scenario: BusinessScenario,
    agent: Agent,
    *,
    file_bucket_id: str | None = None,
    create_if_missing: bool = True,
) -> DataSource | None:
    buckets = db.execute(
        select(DataSource).where(
            DataSource.scenario_id == scenario.id,
            DataSource.type == "file_bucket",
        )
    ).scalars().all()
    if file_bucket_id:
        bucket = db.get(DataSource, file_bucket_id)
        if bucket is None:
            raise RuntimeError(f"找不到文件桶：{file_bucket_id}")
        _validate_source_binding(
            bucket,
            scenario=scenario,
            agent=agent,
            expected_type="file_bucket",
            label="文件桶",
        )
    elif buckets:
        bucket = _one_or_none(buckets, label="代理记账文件桶")
        assert bucket is not None
        _validate_source_binding(
            bucket,
            scenario=scenario,
            agent=agent,
            expected_type="file_bucket",
            label="文件桶",
        )
    elif create_if_missing:
        bucket = DataSource(
            tenant_id=scenario.tenant_id or agent.tenant_id,
            scenario_id=scenario.id,
            name="年度审计文档与附件",
            type="file_bucket",
            config={MAPPING_MARKER_KEY: RECOVERY_PACK_ID},
            status="ok",
        )
        db.add(bucket)
        db.flush()
        source_ids = list(agent.data_source_ids or [])
        source_ids.append(bucket.id)
        agent.data_source_ids = source_ids
    else:
        return None
    return bucket


def _cleanup_created_template_files(paths: Iterable[Path]) -> None:
    storage_root = datasource_service.BUCKETS_DIR.resolve()
    for stored_path in reversed(list(paths)):
        datasource_service.delete_bucket_file_path(str(stored_path))
        current = stored_path.parent
        for _level in range(3):
            try:
                resolved = current.resolve(strict=True)
                resolved.relative_to(storage_root)
                if resolved == storage_root:
                    break
                resolved.rmdir()
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                break
            current = resolved.parent


def _compensate_created_outputs(
    source: DataSource,
    *,
    project_view_created: bool,
    created_paths: Iterable[Path],
    connection: sqlite3.Connection | None = None,
) -> list[str]:
    """Remove this attempt's files before releasing the source writer lock."""

    path = Path(str((source.config or {}).get("path") or "")).resolve()
    manager = (
        closing(sqlite3.connect(path))
        if connection is None
        else nullcontext(connection)
    )
    cleanup_errors: list[str] = []
    with manager as source_connection:
        if not source_connection.in_transaction:
            source_connection.execute("BEGIN IMMEDIATE")
        try:
            _cleanup_created_template_files(created_paths)
        except Exception as cleanup_exc:  # pragma: no cover
            cleanup_errors.append(f"模板文件补偿失败：{cleanup_exc}")
        try:
            if project_view_created:
                # This commit is deliberately last: it releases the mutex only
                # after stable template paths can no longer be adopted.
                _remove_project_view(source, connection=source_connection)
            else:
                source_connection.rollback()
        except Exception as cleanup_exc:  # pragma: no cover
            if source_connection.in_transaction:
                source_connection.rollback()
            cleanup_errors.append(f"审计项目视图补偿失败：{cleanup_exc}")
    return cleanup_errors


def _save_template(
    db,
    bucket: DataSource,
    path: Path,
    *,
    owned_file_ids: Iterable[str] = (),
    created_paths: list[Path] | None = None,
) -> BucketFile:
    if not path.is_file():
        raise RuntimeError(f"缺少审计附件模板：{path}")
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    stable_file_id = hashlib.sha256(
        f"{RECOVERY_PACK_ID}:{bucket.id}:{digest}".encode("utf-8")
    ).hexdigest()[:32]
    stable_file = db.get(BucketFile, stable_file_id)
    if stable_file is not None:
        stable_path = Path(stable_file.stored_path)
        if not (
            stable_file.data_source_id == bucket.id
            and stable_file.filename == path.name
            and stable_file.content_sha256 == digest
            and stable_path.is_file()
            and hashlib.sha256(stable_path.read_bytes()).hexdigest() == digest
        ):
            raise RuntimeError(f"恢复包模板文件 ID 已被其他附件占用：{stable_file_id}")
        stable_file.status = "parsed"
        stable_file.error = ""
        return stable_file

    allowed_ids = {str(value) for value in owned_file_ids}
    candidates = db.execute(
        select(BucketFile).where(
            BucketFile.data_source_id == bucket.id,
            BucketFile.filename.in_([path.name, f"{path.stem}-{digest[:8]}{path.suffix}"]),
        )
    ).scalars().all()
    exact: list[BucketFile] = []
    for candidate in candidates:
        candidate_path = Path(candidate.stored_path)
        if (
            candidate.id in allowed_ids
            and candidate.content_sha256 == digest
            and candidate_path.is_file()
            and hashlib.sha256(candidate_path.read_bytes()).hexdigest() == digest
        ):
            exact.append(candidate)
    unowned = [candidate for candidate in candidates if candidate.id not in allowed_ids]
    if unowned:
        identifiers = "、".join(candidate.id for candidate in unowned)
        raise RuntimeError(
            f"文件桶中同名模板“{path.name}”已被未标记附件占用（{identifiers}）"
        )
    if len(exact) > 1:
        raise RuntimeError(f"文件桶中存在多个相同审计模板：{path.name}")
    if exact:
        exact[0].content_sha256 = digest
        exact[0].status = "parsed"
        exact[0].error = ""
        return exact[0]
    expected_path = (
        datasource_service.BUCKETS_DIR
        / bucket.id
        / ".generated"
        / stable_file_id
        / path.name
    )
    existed_before = expected_path.is_file()
    saved = datasource_service.save_bucket_file(
        bucket,
        path.name,
        content,
        mime=template_artifact_service.expected_mime(path.name) or "application/octet-stream",
        stable_file_id=stable_file_id,
    )
    saved.status = "parsed"
    saved.error = ""
    db.add(saved)
    db.flush()
    if created_paths is not None and not existed_before:
        created_paths.append(Path(saved.stored_path))
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


def _catalog_key(spec: dict[str, str]) -> str:
    return f"bookkeeping_{Path(spec['asset']).stem}"


def _catalog_identity_match(
    catalogs: Iterable[ArtifactTemplate],
    *,
    spec: dict[str, str],
) -> ArtifactTemplate | None:
    matches = {
        item.id: item
        for item in catalogs
        if item.key == _catalog_key(spec) or item.name == spec["template_name"]
    }
    if len(matches) > 1:
        raise RuntimeError(
            f"模板目录身份冲突：key={_catalog_key(spec)!r} 与 "
            f"name={spec['template_name']!r} 指向不同记录"
        )
    return next(iter(matches.values()), None)


def _catalog_version(db, catalog: ArtifactTemplate) -> ArtifactTemplateVersion | None:
    return (
        db.get(ArtifactTemplateVersion, catalog.current_version_id)
        if catalog.current_version_id
        else None
    )


def _legacy_catalog_matches(
    db,
    catalog: ArtifactTemplate,
    *,
    scenario: BusinessScenario,
    bucket: DataSource,
    spec: dict[str, str],
) -> bool:
    asset = ASSET_DIR / spec["asset"]
    if not asset.is_file():
        return False
    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
    version = _catalog_version(db, catalog)
    template_file = db.get(BucketFile, version.bucket_file_id) if version else None
    if not version or not template_file:
        return False
    stored_path = Path(template_file.stored_path)
    try:
        _content, metadata, placeholders = template_catalog_service.inspect_bucket_file(
            template_file, bucket
        )
    except Exception:
        return False
    return (
        catalog.tenant_id == scenario.tenant_id
        and catalog.scenario_id == scenario.id
        and catalog.key == _catalog_key(spec)
        and catalog.name == spec["template_name"]
        and catalog.purpose == spec["name"]
        and catalog.description == spec["description"]
        and catalog.status == "active"
        and catalog.created_by_user_id is None
        and version.template_id == catalog.id
        and version.filename == template_file.filename
        and version.artifact_format == metadata["format"]
        and version.mime == metadata["mime"]
        and version.size == metadata["size"]
        and version.content_sha256 == digest
        and version.placeholder_paths == placeholders
        and version.template_metadata == {
            "suffix": metadata["suffix"],
            "placeholder_count": len(placeholders),
        }
        and version.version_note in {
            "从既有模板 Action 迁移",
            "AP001 年度审计模板初始版本",
            "AP001 年度审计模板更新",
        }
        and version.created_by_user_id is None
        and template_file.data_source_id == bucket.id
        and template_file.filename in {
            asset.name,
            f"{asset.stem}-{digest[:8]}{asset.suffix}",
        }
        and template_file.content_sha256 == digest
        and template_file.size == metadata["size"]
        and template_file.mime == metadata["mime"]
        and template_file.origin_template_file_id is None
        and template_file.origin_template_sha256 == ""
        and template_file.origin_template_id is None
        and template_file.origin_template_version_id is None
        and template_file.generated_by_action_log_id is None
        and template_file.status == "parsed"
        and template_file.error == ""
        and stored_path.is_file()
        and hashlib.sha256(stored_path.read_bytes()).hexdigest() == digest
    )


def _template_action_contract(
    catalog: ArtifactTemplate,
    version: ArtifactTemplateVersion,
    *,
    bucket: DataSource,
    spec: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    variable_paths = set(version.placeholder_paths or [])
    variable_paths.update(
        template_artifact_service.referenced_variable_paths(spec["output"])
    )
    input_schema = template_artifact_service.merge_template_input_schema(
        {}, variable_paths
    )
    input_schema = _describe_template_schema(input_schema)
    executor_config = {
        **template_catalog_service.pinned_action_config(
            catalog,
            version,
            target_data_source_id=bucket.id,
            output_filename=spec["output"],
        ),
        "template_variable_paths": sorted(variable_paths),
    }
    return input_schema, executor_config


def _legacy_template_action_matches(
    action: OntologyAction,
    *,
    project: OntologyEntity,
    catalog: ArtifactTemplate,
    version: ArtifactTemplateVersion,
    bucket: DataSource,
    spec: dict[str, str],
) -> bool:
    input_schema, executor_config = _template_action_contract(
        catalog, version, bucket=bucket, spec=spec
    )
    return (
        action.entity_id == project.id
        and action.name == spec["name"]
        and action.description == spec["description"]
        and action.input_schema == input_schema
        and action.executor_type == "template"
        and action.executor_config == executor_config
        and action.precondition == ""
        and action.postcondition == ""
        and action.enabled is True
        and action.requires_confirmation is True
        and action.idempotency_required is True
        and action.permission_scope == "scenario"
        and action.access_scope == "tenant"
    )


def _preflight_template_ownership(
    db,
    scenario: BusinessScenario,
    project: OntologyEntity,
    bucket: DataSource,
) -> dict[str, set[str]]:
    catalogs = db.scalars(select(ArtifactTemplate).where(
        ArtifactTemplate.tenant_id == scenario.tenant_id,
    )).all()
    actions = db.scalars(select(OntologyAction).where(
        OntologyAction.scenario_id == scenario.id,
    )).all()
    owned_file_ids: dict[str, set[str]] = {}
    for spec in TEMPLATE_ACTION_SPECS:
        asset = ASSET_DIR / spec["asset"]
        if not asset.is_file():
            raise RuntimeError(f"缺少审计附件模板：{asset}")
        catalog = _catalog_identity_match(catalogs, spec=spec)
        if catalog is not None:
            if _has_marker(catalog):
                if (
                    catalog.tenant_id != scenario.tenant_id
                    or catalog.scenario_id != scenario.id
                    or catalog.key != _catalog_key(spec)
                ):
                    raise RuntimeError(
                        f"已标记模板目录“{_catalog_key(spec)}”与所选场景绑定不一致"
                    )
            elif not _legacy_catalog_matches(
                db,
                catalog,
                scenario=scenario,
                bucket=bucket,
                spec=spec,
            ):
                raise RuntimeError(
                    f"模板目录标识“{_catalog_key(spec)}”已被未标记资源占用"
                )
            version = _catalog_version(db, catalog)
            template_file = db.get(BucketFile, version.bucket_file_id) if version else None
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            if template_file is not None:
                stored_path = Path(template_file.stored_path)
                if (
                    template_file.data_source_id == bucket.id
                    and template_file.content_sha256 == digest
                    and stored_path.is_file()
                    and hashlib.sha256(stored_path.read_bytes()).hexdigest() == digest
                ):
                    owned_file_ids.setdefault(_catalog_key(spec), set()).add(
                        template_file.id
                    )
        action = _named_match(actions, name=spec["name"], label="模板操作")
        if action is None or _has_marker(action):
            continue
        version = _catalog_version(db, catalog) if catalog else None
        if (
            catalog is None
            or version is None
            or not _legacy_template_action_matches(
                action,
                project=project,
                catalog=catalog,
                version=version,
                bucket=bucket,
                spec=spec,
            )
        ):
            raise RuntimeError(
                f"模板操作“{spec['name']}”已被未标记资源占用"
            )
    return owned_file_ids


def _upsert_template_actions(
    db,
    scenario: BusinessScenario,
    project: OntologyEntity,
    bucket: DataSource,
    *,
    created_paths: list[Path] | None = None,
) -> list[OntologyAction]:
    owned_file_ids = _preflight_template_ownership(
        db, scenario, project, bucket
    )
    actions: list[OntologyAction] = []
    for spec in TEMPLATE_ACTION_SPECS:
        catalog_key = _catalog_key(spec)
        template_file = _save_template(
            db,
            bucket,
            ASSET_DIR / spec["asset"],
            owned_file_ids=owned_file_ids.get(catalog_key, set()),
            created_paths=created_paths,
        )
        catalog = _one_or_none(
            db.scalars(select(ArtifactTemplate).where(
                ArtifactTemplate.tenant_id == scenario.tenant_id,
                ArtifactTemplate.key == catalog_key,
            )).all(),
            label=f"模板目录标识“{catalog_key}”",
        )
        if catalog is None:
            catalog = template_catalog_service.create_from_bucket_file(
                db,
                tenant_id=str(scenario.tenant_id),
                template_file=template_file,
                template_source=bucket,
                scenario_id=scenario.id,
                name=spec["template_name"],
                purpose=spec["name"],
                description=_marked_description(spec["description"]),
                key=catalog_key,
                version_note="AP001 年度审计模板初始版本",
            )
            catalog_version = db.get(ArtifactTemplateVersion, catalog.current_version_id)
        else:
            catalog.name = spec["template_name"]
            catalog.purpose = spec["name"]
            catalog.description = _marked_description(spec["description"])
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
        input_schema, executor_config = _template_action_contract(
            catalog,
            catalog_version,
            bucket=bucket,
            spec=spec,
        )
        action = _one_or_none(
            db.scalars(
            select(OntologyAction).where(
                OntologyAction.scenario_id == scenario.id,
                OntologyAction.name == spec["name"],
            )
            ).all(),
            label=f"模板操作“{spec['name']}”",
        )
        if action is None:
            action = OntologyAction(
                scenario_id=scenario.id,
                entity_id=project.id,
                name=spec["name"],
            )
            db.add(action)
            db.flush()
        action.entity_id = project.id
        action.description = _marked_description(spec["description"])
        action.input_schema = input_schema
        action.executor_type = "template"
        action.executor_config = executor_config
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
    if scope["actions"]["mode"] == "all":
        return
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


def _verify_platform_commit_after_error(
    db,
    *,
    agent_id: str,
    scenario_id: str,
    source_id: str,
    bucket_id: str,
    attempt_id: str,
) -> str:
    """Classify an uncertain commit using this attempt and the full core contract."""

    try:
        bind = db.get_bind()
        verification_bind = getattr(bind, "engine", bind)
        with OrmSession(
            bind=verification_bind,
            autoflush=False,
            expire_on_commit=False,
        ) as verification_db:
            all_scenario_mappings = list(verification_db.scalars(
                select(DataMapping).where(
                    DataMapping.scenario_id == scenario_id
                )
            ))
            attempt_marked = [
                mapping
                for mapping in all_scenario_mappings
                if (
                    states := dict(mapping.environment_status or {})
                ).get(MAPPING_MARKER_KEY) == RECOVERY_PACK_ID
                and states.get(MAPPING_ATTEMPT_KEY) == attempt_id
            ]
            agent = verification_db.get(Agent, agent_id)
            scenario = verification_db.get(BusinessScenario, scenario_id)
            source = verification_db.get(DataSource, source_id)
            bucket = verification_db.get(DataSource, bucket_id)
            if any(value is None for value in (agent, scenario, source, bucket)):
                return (
                    PLATFORM_COMMIT_UNKNOWN
                    if attempt_marked
                    else PLATFORM_COMMIT_NOT_COMMITTED
                )
            assert agent is not None and scenario is not None
            assert source is not None and bucket is not None
            tenant_id = str(scenario.tenant_id or "")
            if not (
                tenant_id
                and str(agent.tenant_id or "") == tenant_id
                and str(source.tenant_id or "") == tenant_id
                and str(bucket.tenant_id or "") == tenant_id
                and str(agent.scenario_id or "") == scenario_id
                and str(source.scenario_id or "") == scenario_id
                and str(bucket.scenario_id or "") == scenario_id
                and source.type == "sqlite"
                and bucket.type == "file_bucket"
            ):
                return PLATFORM_COMMIT_UNKNOWN

            all_entities = list(verification_db.scalars(
                select(OntologyEntity).where(
                    OntologyEntity.scenario_id == scenario_id
                )
            ))
            entities: dict[str, OntologyEntity] = {}
            complete = True
            for spec in AUDIT_OBJECT_SPECS:
                matches = [
                    entity
                    for entity in all_entities
                    if entity.name == spec["name"]
                    or entity.api_name == spec["api_name"]
                ]
                if len(matches) != 1:
                    complete = False
                    break
                entity = matches[0]
                properties = list(entity.properties)
                if not (
                    entity.name == spec["name"]
                    and entity.api_name == spec["api_name"]
                    and entity.namespace == "bookkeeping_audit"
                    and _has_marker(entity)
                    and all(
                        (
                            prop := _identity_match(
                                properties,
                                name=property_spec[0],
                                api_name=property_spec[1],
                                label=f"对象“{entity.name}”的属性",
                            )
                        ) is not None
                        and _property_contract_matches(prop, property_spec)
                        and _has_marker(prop)
                        for property_spec in spec["properties"]
                    )
                ):
                    complete = False
                    break
                entities[spec["name"]] = entity

            target_ids = {str(entity.id) for entity in entities.values()}
            target_mappings = [
                mapping
                for mapping in all_scenario_mappings
                if str(mapping.entity_id) in target_ids
            ]
            mappings: dict[str, DataMapping] = {}
            if complete:
                for spec in AUDIT_OBJECT_SPECS:
                    entity = entities[spec["name"]]
                    candidates = [
                        mapping
                        for mapping in target_mappings
                        if str(mapping.entity_id) == str(entity.id)
                    ]
                    expected_columns = {
                        property_spec[0]: property_spec[3]
                        for property_spec in spec["properties"]
                    }
                    if len(candidates) != 1:
                        complete = False
                        break
                    mapping = candidates[0]
                    states = dict(mapping.environment_status or {})
                    if not (
                        str(mapping.data_source_id) == source_id
                        and mapping.table_name == spec["table"]
                        and dict(mapping.column_map or {}) == expected_columns
                        and dict(mapping.transform_rules or {}) == {}
                        and mapping.status == "ready"
                        and str(mapping.last_error or "") == ""
                        and states.get(MAPPING_MARKER_KEY) == RECOVERY_PACK_ID
                        and states.get(MAPPING_ATTEMPT_KEY) == attempt_id
                        and states.get("dev")
                        == {"status": "ready", "last_error": ""}
                    ):
                        complete = False
                        break
                    mappings[spec["name"]] = mapping

            customer = None
            if complete:
                customer_matches = [
                    entity
                    for entity in all_entities
                    if entity.name == "客户" or entity.api_name == "customer"
                ]
                if len(customer_matches) != 1:
                    complete = False
                else:
                    customer = customer_matches[0]
                    customer_mappings = list(verification_db.scalars(
                        select(DataMapping).where(
                            DataMapping.scenario_id == scenario_id,
                            DataMapping.entity_id == customer.id,
                        )
                    ))
                    if (
                        len(customer_mappings) != 1
                        or not _customer_mapping_contract_matches(
                            customer_mappings[0],
                            scenario=scenario,
                            customer=customer,
                            source=source,
                        )
                    ):
                        complete = False
                    else:
                        mappings["客户"] = customer_mappings[0]

            if complete:
                relations = list(verification_db.scalars(
                    select(OntologyRelation).where(
                        OntologyRelation.scenario_id == scenario_id
                    )
                ))
                for spec in AUDIT_RELATION_SPECS:
                    matches = [
                        relation
                        for relation in relations
                        if relation.name == spec["name"]
                        or relation.api_name == spec["api_name"]
                    ]
                    if len(matches) != 1 or not _has_marker(matches[0]):
                        complete = False
                        break
                    relation = matches[0]
                    bindings = list(verification_db.scalars(
                        select(RelationDataMapping).where(
                            RelationDataMapping.relation_id == relation.id
                        )
                    ))
                    if not (
                        len(bindings) == 1
                        and relation.source_entity_id
                        == (customer.id if spec["source"] == "客户" else entities[spec["source"]].id)
                        and relation.target_entity_id == entities[spec["target"]].id
                        and bindings[0].scenario_id == scenario_id
                        and bindings[0].source_mapping_id == mappings[spec["source"]].id
                        and bindings[0].target_mapping_id == mappings[spec["target"]].id
                        and bindings[0].data_source_id == source_id
                        and bindings[0].table_name == mappings[spec["target"]].table_name
                        and bindings[0].foreign_key_column == spec["foreign_key"]
                        and bindings[0].status == "ready"
                    ):
                        complete = False
                        break

            if complete:
                catalogs = list(verification_db.scalars(
                    select(ArtifactTemplate).where(
                        ArtifactTemplate.tenant_id == tenant_id
                    )
                ))
                actions = list(verification_db.scalars(
                    select(OntologyAction).where(
                        OntologyAction.scenario_id == scenario_id
                    )
                ))
                for spec in TEMPLATE_ACTION_SPECS:
                    catalog_matches = [
                        item
                        for item in catalogs
                        if item.key == _catalog_key(spec)
                        or item.name == spec["template_name"]
                    ]
                    action_matches = [
                        item for item in actions if item.name == spec["name"]
                    ]
                    if len(catalog_matches) != 1 or len(action_matches) != 1:
                        complete = False
                        break
                    catalog = catalog_matches[0]
                    action = action_matches[0]
                    version = verification_db.get(
                        ArtifactTemplateVersion,
                        catalog.current_version_id,
                    )
                    template_file = (
                        verification_db.get(BucketFile, version.bucket_file_id)
                        if version is not None
                        else None
                    )
                    asset = ASSET_DIR / spec["asset"]
                    digest = hashlib.sha256(asset.read_bytes()).hexdigest()
                    stored_path = (
                        Path(template_file.stored_path)
                        if template_file is not None
                        else None
                    )
                    if not (
                        catalog.scenario_id == scenario_id
                        and _has_marker(catalog)
                        and version is not None
                        and version.template_id == catalog.id
                        and version.content_sha256 == digest
                        and template_file is not None
                        and template_file.data_source_id == bucket_id
                        and template_file.content_sha256 == digest
                        and stored_path is not None
                        and stored_path.is_file()
                        and hashlib.sha256(stored_path.read_bytes()).hexdigest()
                        == digest
                        and _has_marker(action)
                        and action.entity_id == entities["审计项目"].id
                        and action.executor_type == "template"
                        and action.enabled is True
                        and action.requires_confirmation is True
                        and action.executor_config.get("target_data_source_id")
                        == bucket_id
                    ):
                        complete = False
                        break

            if complete:
                complete = (
                    (agent.system_prompt or "").count(PROMPT_MARKER) == 1
                    and source_id in set(agent.data_source_ids or [])
                    and bucket_id in set(agent.data_source_ids or [])
                    and scenario.namespace == "bookkeeping_audit"
                    and "年度审计域覆盖" in (scenario.description or "")
                )
            if complete:
                return PLATFORM_COMMIT_CONFIRMED
            if not attempt_marked:
                return PLATFORM_COMMIT_NOT_COMMITTED
            return PLATFORM_COMMIT_UNKNOWN
    except Exception:
        return PLATFORM_COMMIT_UNKNOWN


def _acquire_upgrade_mutex(
    db,
    agent: Agent,
    scenario: BusinessScenario,
    source: DataSource,
    bucket: DataSource | None,
) -> str:
    """Hold the verified platform exclusion contract through final resolution."""

    tenant_id = str(scenario.tenant_id or "")
    if (
        not tenant_id
        or str(agent.tenant_id or "") != tenant_id
        or str(source.tenant_id or "") != tenant_id
        or (bucket is not None and str(bucket.tenant_id or "") != tenant_id)
    ):
        raise RuntimeError("代理记账升级锁定范围缺少一致的租户身份")
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
            raise RuntimeError("代理记账升级场景锁获取失败")
        return dialect_name
    if dialect_name != "postgresql":
        raise RuntimeError(
            f"代理记账恢复脚本不支持平台数据库方言“{dialect_name or 'unknown'}”；"
            "仅 SQLite 与 PostgreSQL 具备已验证的事务互斥契约"
        )

    db.execute(text(
        "LOCK TABLE data_mappings IN SHARE ROW EXCLUSIVE MODE"
    ))
    locks = [
        db.scalar(
            select(Agent.id)
            .where(
                Agent.id == agent.id,
                Agent.tenant_id == tenant_id,
                Agent.scenario_id == scenario.id,
            )
            .with_for_update()
        ),
        db.scalar(
            select(BusinessScenario.id)
            .where(
                BusinessScenario.id == scenario.id,
                BusinessScenario.tenant_id == tenant_id,
            )
            .with_for_update()
        ),
        db.scalar(
            select(DataSource.id)
            .where(
                DataSource.id == source.id,
                DataSource.scenario_id == scenario.id,
                DataSource.tenant_id == tenant_id,
            )
            .with_for_update()
        ),
    ]
    if bucket is not None:
        locks.append(
            db.scalar(
                select(DataSource.id)
                .where(
                    DataSource.id == bucket.id,
                    DataSource.scenario_id == scenario.id,
                    DataSource.tenant_id == tenant_id,
                    DataSource.type == "file_bucket",
                )
                .with_for_update()
            )
        )
    if any(value is None for value in locks):
        raise RuntimeError("代理记账升级 Agent、场景、数据源或文件桶锁获取失败")
    return dialect_name


def _lock_created_bucket(
    db,
    *,
    platform_dialect: str,
    scenario: BusinessScenario,
    bucket: DataSource,
) -> None:
    if platform_dialect != "postgresql":
        return
    locked = db.scalar(
        select(DataSource.id)
        .where(
            DataSource.id == bucket.id,
            DataSource.scenario_id == scenario.id,
            DataSource.tenant_id == scenario.tenant_id,
            DataSource.type == "file_bucket",
        )
        .with_for_update()
    )
    if locked is None:
        raise RuntimeError("代理记账升级文件桶锁获取失败")


def upgrade(
    db,
    *,
    agent_id: str | None = None,
    scenario_id: str | None = None,
    source_id: str | None = None,
    file_bucket_id: str | None = None,
) -> dict[str, Any]:
    attempt_id = uuid.uuid4().hex
    source: DataSource | None = None
    bucket: DataSource | None = None
    created_paths: list[Path] = []
    project_view_created = False
    source_committed = False
    platform_committed = False
    external_resolution_attempted = False
    platform_dialect = ""
    try:
        agent, scenario = _find_agent_and_scenario(
            db,
            agent_id=agent_id,
            scenario_id=scenario_id,
        )
        source = _sqlite_source(
            db,
            scenario,
            agent,
            source_id=source_id,
        )
        bucket = _template_bucket(
            db,
            scenario,
            agent,
            file_bucket_id=file_bucket_id,
            create_if_missing=False,
        )
        resolved_agent_id = str(agent.id)
        resolved_scenario_id = str(scenario.id)
        resolved_source_id = str(source.id)
        resolved_bucket_id = str(bucket.id) if bucket is not None else ""
        platform_dialect = _acquire_upgrade_mutex(
            db,
            agent,
            scenario,
            source,
            bucket,
        )
        db.expire_all()
        agent, scenario = _find_agent_and_scenario(
            db,
            agent_id=resolved_agent_id,
            scenario_id=resolved_scenario_id,
        )
        source = _sqlite_source(
            db,
            scenario,
            agent,
            source_id=resolved_source_id,
        )
        bucket = _template_bucket(
            db,
            scenario,
            agent,
            file_bucket_id=resolved_bucket_id or file_bucket_id,
            create_if_missing=True,
        )
        if bucket is None:  # pragma: no cover - defensive type narrowing
            raise RuntimeError("代理记账恢复包未能解析文件桶")
        _lock_created_bucket(
            db,
            platform_dialect=platform_dialect,
            scenario=scenario,
            bucket=bucket,
        )
        resolved_bucket_id = str(bucket.id)
        source_path = Path(
            str((source.config or {}).get("path") or "")
        ).expanduser().resolve()
        with closing(sqlite3.connect(source_path)) as source_connection:
            source_connection.execute("BEGIN IMMEDIATE")
            try:
                _validate_sqlite_source_contract(
                    source,
                    connection=source_connection,
                )
                entities, mappings = _upsert_audit_objects(
                    db,
                    scenario,
                    source,
                )
                _upsert_audit_relations(
                    db,
                    scenario,
                    source,
                    entities,
                    mappings,
                )
                actions = _upsert_template_actions(
                    db,
                    scenario,
                    entities["审计项目"],
                    bucket,
                    created_paths=created_paths,
                )
                _extend_explicit_action_scope(agent, actions)
                _update_agent_contract(agent)
                scenario.namespace = "bookkeeping_audit"
                if "年度审计域覆盖" not in (scenario.description or ""):
                    scenario.description = (
                        (scenario.description or "").rstrip()
                        + "\n年度审计域覆盖审计项目、底稿、函证、调整、报告、经审计财务报表、报表附注和三级复核。"
                    ).strip()
                project_view_created = _ensure_project_view(
                    source,
                    connection=source_connection,
                )
                # The base customer mapping is user-owned and must never carry
                # a recovery-attempt marker or be rewritten by this pack.
                for name, mapping in mappings.items():
                    if name == "客户":
                        continue
                    mapping.environment_status = {
                        **(mapping.environment_status or {}),
                        MAPPING_ATTEMPT_KEY: attempt_id,
                    }
                db.flush()
                result_payload = {
                    "attempt_id": attempt_id,
                    "agent_id": resolved_agent_id,
                    "scenario_id": resolved_scenario_id,
                    "source_id": resolved_source_id,
                    "bucket_id": resolved_bucket_id,
                    "mapped_objects": sorted(
                        name for name in mappings if name != "客户"
                    ),
                    "template_actions": [action.name for action in actions],
                }
                source_connection.commit()
                source_committed = True
                # Preserve the source ownership boundary until the independent
                # platform outcome is known or compensation is complete.
                source_connection.execute("BEGIN IMMEDIATE")
                try:
                    db.commit()
                    platform_committed = True
                except Exception as exc:
                    external_resolution_attempted = True
                    rollback_succeeded = True
                    try:
                        db.rollback()
                    except Exception as rollback_exc:  # pragma: no cover
                        rollback_succeeded = False
                        if hasattr(exc, "add_note"):
                            exc.add_note(f"原平台会话回滚失败：{rollback_exc}")
                    verification = _verify_platform_commit_after_error(
                        db,
                        agent_id=resolved_agent_id,
                        scenario_id=resolved_scenario_id,
                        source_id=resolved_source_id,
                        bucket_id=resolved_bucket_id,
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
                        cleanup_errors = _compensate_created_outputs(
                            source,
                            project_view_created=project_view_created,
                            created_paths=created_paths,
                            connection=source_connection,
                        )
                        if cleanup_errors and hasattr(exc, "add_note"):
                            exc.add_note("；".join(cleanup_errors))
                        raise
                    else:
                        source_connection.rollback()
                        if hasattr(exc, "add_note"):
                            exc.add_note(
                                f"{platform_dialect or 'unknown'} 平台提交结果无法确认；"
                                "已保留本次新增视图和模板文件，可在确认平台状态后幂等重试。"
                            )
                        raise
                else:
                    source_connection.rollback()
            except Exception:
                if source_connection.in_transaction:
                    source_connection.rollback()
                raise
    except Exception as exc:
        cleanup_errors: list[str] = []
        if not source_committed:
            try:
                _cleanup_created_template_files(created_paths)
            except Exception as cleanup_exc:  # pragma: no cover
                cleanup_errors.append(f"模板文件补偿失败：{cleanup_exc}")
        elif (
            not platform_committed
            and not external_resolution_attempted
            and source is not None
        ):
            cleanup_errors.extend(_compensate_created_outputs(
                source,
                project_view_created=project_view_created,
                created_paths=created_paths,
            ))
        db.rollback()
        if cleanup_errors and hasattr(exc, "add_note"):
            exc.add_note("；".join(cleanup_errors))
        raise
    return result_payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="安全安装代理记账年度审计恢复包"
    )
    parser.add_argument("--agent-id", help="目标 Agent ID；多候选时必须提供")
    parser.add_argument("--scenario-id", help="目标业务场景 ID")
    parser.add_argument("--source-id", help="目标结构化 SQLite 数据源 ID")
    parser.add_argument("--file-bucket-id", help="目标模板/产出物文件桶 ID")
    args = parser.parse_args()
    from app.config import get_settings

    settings = get_settings()
    if not settings.uses_sqlite_database or settings.minio_configured:
        parser.error(
            "此脚本仅供迁移前的隔离 SQLite fixture 使用；MySQL/MinIO 环境禁止运行，"
            "请使用版本化 MySQL 数据迁移和 MinIO lifecycle 工具"
        )
    init_db()
    db = SessionLocal()
    try:
        result = upgrade(
            db,
            agent_id=args.agent_id,
            scenario_id=args.scenario_id,
            source_id=args.source_id,
            file_bucket_id=args.file_bucket_id,
        )
        print("代理记账年度审计交付升级完成")
        print(f"Agent: {result['agent_id']}")
        print(f"场景: {result['scenario_id']}")
        print(f"语义映射: {', '.join(result['mapped_objects'])}")
        print(f"模板动作: {', '.join(result['template_actions'])}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
