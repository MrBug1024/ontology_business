# -*- coding: utf-8 -*-
"""完善年度审计工作流：加入经审计报表/附注查询与生成、三级复核出具步骤。"""
import urllib.request, json

BASE = "http://127.0.0.1:3009/api/scenarios"
WF = "4876b72d84574c0892f8271b09be135d"
LLM = "f9b99cbad0134980ac17c9f7afa26817"

# 操作 ID
A_PROJECT = "54a45ad5a8284553ad1509ea8e990096"   # 查询审计项目
A_PAPER = "5a839a219102423e9caad85253312f67"     # 查询审计底稿
A_CONF = "8b03624919e94dc79ffd660d46767360"      # 查询函证情况
A_ADJ = "604982aa71a44331a37a72048691ff20"       # 查询审计调整
A_STMT = "2b9267d747264bd1a322789333150975"      # 查询经审计财务报表
A_NOTE = "33a46dad506b4a50acfcb7d21ddcd81f"      # 查询报表附注
A_REVIEW = "e37a0eece98042158c53b4e7bea4a40f"    # 查询复核记录
R_RISK = "936ff9c4a20b40abb6ec1c6a94a07366"      # 高风险判断

def node(id, type, name, x, y, data):
    return {"id": id, "type": type, "name": name,
            "position": {"x": x, "y": y}, "data": {"name": name, **data}}

nodes = [
    node("start", "start", "开始", 0, 200, {}),
    node("n1", "action", "查询审计项目", 200, 200, {"action_id": A_PROJECT, "params": {}}),
    node("n2", "action", "查询审计底稿", 400, 200, {"action_id": A_PAPER, "params": {"project_id": "{{params.project_id}}"}}),
    node("n3", "action", "查询函证情况", 600, 200, {"action_id": A_CONF, "params": {"project_id": "{{params.project_id}}"}}),
    node("n4", "action", "查询审计调整", 800, 200, {"action_id": A_ADJ, "params": {"project_id": "{{params.project_id}}"}}),
    node("n5", "rule", "高风险判断", 1000, 200, {"rule_id": R_RISK, "record": {"风险等级": "{{params.risk_level}}"}}),
    node("n6", "llm", "生成风险关注审计报告", 1200, 100, {
        "llm_config_id": LLM,
        "system": "你是注册会计师，根据审计数据生成审计报告，本项目为高风险，需扩大实质性程序并重点关注复核。",
        "prompt": "请根据以下数据生成风险关注型审计报告：\n审计项目：{{n1.result}}\n审计底稿：{{n2.result}}\n函证情况：{{n3.result}}\n审计调整：{{n4.result}}\n高风险判断：{{n5.result}}\n\n报告应包含：1)审计范围与风险评估 2)实质性程序执行 3)函证与差异分析 4)审计调整及其影响 5)审计意见 6)重大风险提示",
    }),
    node("n7", "llm", "生成标准审计报告", 1200, 300, {
        "llm_config_id": LLM,
        "system": "你是注册会计师，根据审计数据生成标准无保留意见审计报告。",
        "prompt": "请根据以下数据生成标准无保留意见审计报告：\n审计项目：{{n1.result}}\n审计底稿：{{n2.result}}\n函证情况：{{n3.result}}\n审计调整：{{n4.result}}\n高风险判断：{{n5.result}}\n\n报告应包含：一、审计意见 二、形成审计意见的基础 三、管理层对财务报表的责任 四、注册会计师对财务报表审计的责任",
    }),
    node("n8", "action", "查询经审计财务报表", 1400, 200, {"action_id": A_STMT, "params": {"project_id": "{{params.project_id}}"}}),
    node("n9", "action", "查询报表附注", 1600, 200, {"action_id": A_NOTE, "params": {"project_id": "{{params.project_id}}"}}),
    node("n10", "llm", "生成报表附注与披露", 1800, 200, {
        "llm_config_id": LLM,
        "system": "你是注册会计师，负责编制财务报表附注，确保披露完整、符合企业会计准则。",
        "prompt": "请根据以下数据编制/完善财务报表附注：\n经审计财务报表：{{n8.result}}\n已有附注条目：{{n9.result}}\n审计调整：{{n4.result}}\n\n附注应包含：一、公司基本情况 二、会计报表编制基础 三、主要会计政策与会计估计 四、税项 五、财务报表项目注释（货币资金、应收账款、固定资产、应付账款、未分配利润等，含期末数/期初数）",
    }),
    node("n11", "action", "查询复核记录", 2000, 200, {"action_id": A_REVIEW, "params": {"project_id": "{{params.project_id}}"}}),
    node("n12", "llm", "三级复核与出具报告", 2200, 200, {
        "llm_config_id": LLM,
        "system": "你是会计师事务所主任会计师，负责审计项目三级复核并决定是否出具报告。",
        "prompt": "请对以下审计项目执行三级复核（一级项目经理→二级部门经理→三级主任会计师）并给出出具结论：\n审计报告：{{n6.result}} {{n7.result}}\n经审计财务报表：{{n8.result}}\n报表附注：{{n10.result}}\n复核记录：{{n11.result}}\n\n请输出：1)各级复核关注点与结论 2)报表勾稽校验（资产=负债+权益）3)审计意见类型是否恰当 4)是否具备出具条件及归档要求",
    }),
    node("end", "end", "结束", 2400, 200, {"summary": "年度审计流程完成：报告+报表+附注+三级复核"}),
]

edges = [
    {"id": "e1", "source": "start", "target": "n1", "label": ""},
    {"id": "e2", "source": "n1", "target": "n2", "label": ""},
    {"id": "e3", "source": "n2", "target": "n3", "label": ""},
    {"id": "e4", "source": "n3", "target": "n4", "label": ""},
    {"id": "e5", "source": "n4", "target": "n5", "label": ""},
    {"id": "e6", "source": "n5", "target": "n6", "label": "true"},
    {"id": "e7", "source": "n5", "target": "n7", "label": "false"},
    {"id": "e8", "source": "n6", "target": "n8", "label": ""},
    {"id": "e9", "source": "n7", "target": "n8", "label": ""},
    {"id": "e10", "source": "n8", "target": "n9", "label": ""},
    {"id": "e11", "source": "n9", "target": "n10", "label": ""},
    {"id": "e12", "source": "n10", "target": "n11", "label": ""},
    {"id": "e13", "source": "n11", "target": "n12", "label": ""},
    {"id": "e14", "source": "n12", "target": "end", "label": ""},
]

payload = {
    "name": "年度审计流程",
    "description": "年度财务报表审计全流程：查询审计项目→底稿→函证→审计调整→高风险判断→生成审计报告→查询经审计报表/附注→生成报表附注与披露→三级复核→出具报告归档",
    "trigger_type": "manual",
    "trigger_config": {},
    "steps": [],
    "nodes": nodes,
    "edges": edges,
    "enabled": True,
}

req = urllib.request.Request(
    f"{BASE}/workflows/{WF}",
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="PUT",
)
r = json.load(urllib.request.urlopen(req))
print("updated workflow", r["id"], "nodes:", len(r["nodes"]), "edges:", len(r["edges"]))
