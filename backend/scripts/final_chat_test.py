# -*- coding: utf-8 -*-
"""最终端到端审计测试（本体注入后）"""
import json
import urllib.request

AGENT_ID = "83bcf35455834a4284c1b5eaaafe07b4"
URL = f"http://127.0.0.1:8001/api/agents/{AGENT_ID}/chat"

question = (
    "请审计规则 27（护理天数超过住院天数）。"
    "先定位规则，再根据规则内容决定需要哪些表的哪些字段，"
    "查询并统计违规记录，给出违规明细和汇总。"
)

req = urllib.request.Request(
    URL,
    data=json.dumps({"message": question, "stream": True}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

tools = []
answer_parts = []
with urllib.request.urlopen(req, timeout=600) as resp:
    for raw in resp:
        line = raw.decode().strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        t = ev.get("type")
        data = ev.get("data")
        if t == "tool_call":
            tools.append((data or {}).get("name"))
        elif t == "token":
            answer_parts.append(data if isinstance(data, str) else "")
        elif t == "done":
            if isinstance(data, str) and data:
                answer_parts = [data]

answer = "".join(answer_parts)
print("=== 调用的工具链 ===")
for t in tools:
    print("  -", t)
print(f"\n=== 回答（{len(answer)} 字符）===")
print(answer[:2500])
