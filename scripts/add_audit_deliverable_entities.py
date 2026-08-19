# -*- coding: utf-8 -*-
"""为代理记账业务场景新增审计产出物实体（幂等：先查后建）。"""
import urllib.request, json

BASE = "http://127.0.0.1:3009/api/scenarios"
SID = "56e2006148e8499e8599f5c7c8145e60"

def post(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.load(urllib.request.urlopen(req))

def prop(name, dtype="string", key=False, required=False, desc=""):
    return {"name": name, "data_type": dtype, "description": desc,
            "is_key": key, "is_required": required, "is_enum": False,
            "enum_values": [], "default_value": ""}

ENTITIES = [
    {
        "name": "审计报告",
        "description": "年度审计出具的正式报告，含审计报告正文与管理建议书",
        "icon": "file-text",
        "color": "#dc2626",
        "is_abstract": False,
        "properties": [
            prop("报告ID", key=True, required=True),
            prop("项目ID", required=True, desc="关联审计项目"),
            prop("报告文号", desc="如 京创会审字[2024]第3999号"),
            prop("报告类型", desc="审计报告/管理建议书"),
            prop("审计意见类型", desc="标准无保留意见/保留意见/否定意见/无法表示意见"),
            prop("报告日期"),
            prop("编制人"),
            prop("复核人"),
            prop("复核日期"),
            prop("状态", desc="草稿/已出具/已归档"),
            prop("内容摘要", desc="审计意见段与管理建议书要点"),
        ],
    },
    {
        "name": "经审计财务报表",
        "description": "经审计的四表合计数（资产负债表/利润表/现金流量表/所有者权益变动表）",
        "icon": "table",
        "color": "#059669",
        "is_abstract": False,
        "properties": [
            prop("报表ID", key=True, required=True),
            prop("项目ID", required=True, desc="关联审计项目"),
            prop("报表类型", desc="资产负债表/利润表/现金流量表/所有者权益变动表"),
            prop("会计期间", desc="如 2023-12-31 或 2023年度"),
            prop("资产总计", "float"),
            prop("负债总计", "float"),
            prop("权益总计", "float"),
            prop("营业收入", "float"),
            prop("净利润", "float"),
            prop("状态", desc="未审定/已审定"),
        ],
    },
    {
        "name": "报表附注",
        "description": "财务报表附注条目（公司基本情况、会计政策、科目明细披露）",
        "icon": "list",
        "color": "#d97706",
        "is_abstract": False,
        "properties": [
            prop("附注ID", key=True, required=True),
            prop("项目ID", required=True, desc="关联审计项目"),
            prop("附注编号", desc="如 五(一)"),
            prop("附注标题", desc="如 货币资金/应收账款/固定资产"),
            prop("附注内容", desc="期末数、期初数及披露说明"),
            prop("状态", desc="未审定/已审定"),
        ],
    },
    {
        "name": "复核记录",
        "description": "审计项目三级复核记录（项目经理→部门经理→主任会计师）",
        "icon": "check-circle",
        "color": "#7c3aed",
        "is_abstract": False,
        "properties": [
            prop("复核ID", key=True, required=True),
            prop("项目ID", required=True, desc="关联审计项目"),
            prop("复核级别", desc="一级复核/二级复核/三级复核"),
            prop("复核人"),
            prop("复核日期"),
            prop("复核结果", desc="通过/有条件通过/不通过"),
            prop("发现问题", desc="复核中发现的问题及处理"),
            prop("状态", desc="待复核/已完成"),
        ],
    },
]

# 已存在的实体名
d = json.load(urllib.request.urlopen(f"{BASE}/{SID}"))
existing = {e["name"] for e in d["entities"]}

for ent in ENTITIES:
    if ent["name"] in existing:
        print("skip", ent["name"])
        continue
    r = post(f"/{SID}/entities", ent)
    print("created", ent["name"], r["id"])
