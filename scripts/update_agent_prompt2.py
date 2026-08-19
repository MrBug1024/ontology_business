# -*- coding: utf-8 -*-
"""更新 Agent 系统提示词：年审模块补充审计产出物（报告/报表/附注/复核）。"""
import urllib.request, json

AGENT = "7eb71f4a9a49417693d79b95c67879d2"
r = json.load(urllib.request.urlopen(f"http://127.0.0.1:8001/api/agents/{AGENT}"))
sp = r["system_prompt"]

old_block = """- **AI 自动化**：AI 助理工程师负责抽凭抽样（≥70%）、底稿索引、测算表；AI 注册会计师负责风险评估矩阵、审计报告初稿、复核关注点识别、政策版本比对
- **可用操作**：查询审计项目、查询审计底稿、查询函证情况、查询审计调整
- **可用规则**：函证未回函预警、函证差异预警、高风险审计项目预警、审计调整未接受预警
- **可用工作流**：年度审计流程（查询项目→底稿→函证→调整→高风险判断→生成审计报告）"""

new_block = """- **审计产出物**（年审最终交付）：
  - 审计报告：标准无保留/保留/否定/无法表示意见；结构=审计意见+形成审计意见的基础+管理层责任+注册会计师责任（四段式）
  - 经审计财务报表：资产负债表/利润表/现金流量表/所有者权益变动表四表合计数；必须满足 资产总计=负债总计+权益总计
  - 财务报表附注：公司基本情况+编制基础+主要会计政策（金融工具/应收款项账龄/固定资产折旧）+税项+报表项目注释（期末数/期初数）+关联方+日后事项
  - 管理建议书：值得关注的内部控制缺陷+管理层回复+其他建议（经营/税务/管理）
  - 三级复核：一级项目经理→二级部门经理→三级主任会计师，全部通过方可出具
- **标杆案例**：北京****有限公司2023年审（京创会审字[2024]第3999号，标准无保留意见）——资产总计795.66万、负债1053.02万、权益-257.36万（资不抵债但持续经营）、营收4803.60万、净利润194.91万；关键调整=应收账款期末负值重分类171.86万至预收账款。详见文档桶《年审案例参考-北京有限公司2023》
- **AI 自动化**：AI 助理工程师负责抽凭抽样（≥70%）、底稿索引、测算表；AI 注册会计师负责风险评估矩阵、审计报告初稿、报表附注编制、复核关注点识别、政策版本比对
- **可用操作**：查询审计项目、查询审计底稿、查询函证情况、查询审计调整、查询审计报告、查询经审计财务报表、查询报表附注、查询复核记录
- **可用规则**：函证未回函预警、函证差异预警、高风险审计项目预警、审计调整未接受预警、报表勾稽校验-资产负债平衡、审计报告未出具预警、三级复核未完成预警
- **可用工作流**：年度审计流程（查询项目→底稿→函证→调整→高风险判断→生成审计报告→查询报表/附注→生成报表附注与披露→三级复核→出具报告归档）"""

assert old_block in sp, "old block not found"
sp = sp.replace(old_block, new_block)

body = json.dumps({
    "name": r["name"],
    "description": r.get("description", ""),
    "scenario_id": r.get("scenario_id"),
    "llm_config_id": r.get("llm_config_id"),
    "system_prompt": sp,
    "skill_ids": r.get("skill_ids", []),
    "mcp_ids": r.get("mcp_ids", []),
    "data_source_ids": r.get("data_source_ids", []),
    "temperature": r.get("temperature", 0.2),
    "max_tokens": r.get("max_tokens", 4096),
}).encode()
req = urllib.request.Request(
    f"http://127.0.0.1:8001/api/agents/{AGENT}",
    data=body,
    headers={"Content-Type": "application/json"},
    method="PUT",
)
res = json.load(urllib.request.urlopen(req))
print("updated, prompt length:", len(res.get("system_prompt", "")))
