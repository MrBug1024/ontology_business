# -*- coding: utf-8 -*-
"""更新 Agent 提示词：回答规范补充附件产出要求。"""
import urllib.request, json

AGENT = "7eb71f4a9a49417693d79b95c67879d2"
r = json.load(urllib.request.urlopen(f"http://127.0.0.1:8001/api/agents/{AGENT}"))
sp = r["system_prompt"]

old = """## 回答规范
1. 先查询数据，再给出分析和建议
2. 引用具体数据（金额、日期、科目）
3. 涉及税务计算时，列出计算过程
4. 发现风险时，明确标注风险等级和整改建议
5. 回答要专业、简洁、有条理"""

new = """## 回答规范
1. 先查询数据，再给出分析和建议
2. 引用具体数据（金额、日期、科目）
3. 涉及税务计算时，列出计算过程
4. 发现风险时，明确标注风险等级和整改建议
5. 回答要专业、简洁、有条理
6. **产出物附件**：当生成正式业务产出物（审计报告、经审计财务报表、报表附注、管理建议书、月度报告、函证、分析报告等）时，必须调用 save_deliverable 工具保存为附件，并在回答末尾以 Markdown 链接附上，格式：[📎 文件名.md](/api/data-sources/files/<file_id>/download)。用户可点击预览或下载。一次对话可生成多个附件（如审计报告+报表+附注分别保存）。"""

assert old in sp, "old 回答规范 not found"
sp = sp.replace(old, new)

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
