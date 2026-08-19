# -*- coding: utf-8 -*-
"""为代理记账业务场景新增审计产出物查询操作（幂等：先查后建）。"""
import urllib.request, json

BASE = "http://127.0.0.1:3009/api/scenarios"
SID = "56e2006148e8499e8599f5c7c8145e60"
DS = "68fcb44b941a40d48c7aba1efb14e7f6"

def post(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req))

d = json.load(urllib.request.urlopen(f"{BASE}/{SID}"))
entities = {e["name"]: e["id"] for e in d["entities"]}
existing_actions = {a["name"] for a in d["actions"]}

ACTIONS = [
    {
        "entity_id": entities["审计报告"],
        "name": "查询审计报告",
        "description": "查询审计项目出具的审计报告与管理建议书（含意见类型、文号、状态）",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "审计项目ID"}},
            "required": ["project_id"],
        },
        "executor_type": "sql",
        "executor_config": {
            "data_source_id": DS,
            "sql": "SELECT report_id, report_no, report_type, opinion_type, report_date, preparer, reviewer, review_date, status, content_summary FROM audit_reports WHERE project_id = '{project_id}' ORDER BY report_date",
        },
        "precondition": "",
        "postcondition": "",
        "enabled": True,
    },
    {
        "entity_id": entities["经审计财务报表"],
        "name": "查询经审计财务报表",
        "description": "查询审计项目经审定的四表合计数（资产/负债/权益/收入/净利润）",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "审计项目ID"}},
            "required": ["project_id"],
        },
        "executor_type": "sql",
        "executor_config": {
            "data_source_id": DS,
            "sql": "SELECT statement_id, statement_type, period, total_assets, total_liabilities, total_equity, total_revenue, net_profit, status FROM audited_statements WHERE project_id = '{project_id}' ORDER BY statement_type",
        },
        "precondition": "",
        "postcondition": "",
        "enabled": True,
    },
    {
        "entity_id": entities["报表附注"],
        "name": "查询报表附注",
        "description": "查询审计项目财务报表附注条目（公司基本情况、会计政策、科目明细披露）",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "审计项目ID"}},
            "required": ["project_id"],
        },
        "executor_type": "sql",
        "executor_config": {
            "data_source_id": DS,
            "sql": "SELECT note_id, note_no, note_title, note_content, status FROM statement_notes WHERE project_id = '{project_id}' ORDER BY note_no",
        },
        "precondition": "",
        "postcondition": "",
        "enabled": True,
    },
    {
        "entity_id": entities["复核记录"],
        "name": "查询复核记录",
        "description": "查询审计项目三级复核记录（项目经理→部门经理→主任会计师）",
        "input_schema": {
            "type": "object",
            "properties": {"project_id": {"type": "string", "description": "审计项目ID"}},
            "required": ["project_id"],
        },
        "executor_type": "sql",
        "executor_config": {
            "data_source_id": DS,
            "sql": "SELECT review_id, review_level, reviewer, review_date, review_result, issues_found, status FROM review_records WHERE project_id = '{project_id}' ORDER BY review_date",
        },
        "precondition": "",
        "postcondition": "",
        "enabled": True,
    },
]

for a in ACTIONS:
    if a["name"] in existing_actions:
        print("skip", a["name"])
        continue
    r = post(f"/{SID}/actions", a)
    print("created", a["name"], r["id"])
