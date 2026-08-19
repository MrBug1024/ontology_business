# -*- coding: utf-8 -*-
"""端到端测试：模拟前端 Agent 对话，让 Agent 对 AP001 执行年度审计。
消费 SSE 流，打印工具调用与最终回答，验证能否真正产出审计结果/报告/报表。"""
import urllib.request, json

AGENT = "7eb71f4a9a49417693d79b95c67879d2"
BASE = "http://127.0.0.1:8001/api"

def sse_chat(message, conversation_id=None):
    body = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    req = urllib.request.Request(
        f"{BASE}/agents/{AGENT}/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    resp = urllib.request.urlopen(req, timeout=300)
    conv_id = None
    tools = []
    content = ""
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:]
        if payload == "[DONE]":
            break
        try:
            ev = json.loads(payload)
        except Exception:
            continue
        t = ev.get("type")
        d = ev.get("data")
        if t == "token":
            content += d
        elif t == "tool_call":
            tools.append(("CALL", d.get("name"), d.get("args")))
        elif t == "tool_result":
            tools.append(("RESULT", d.get("name"), str(d.get("result"))[:120]))
        elif t == "error":
            tools.append(("ERROR", None, d))
    # 从消息列表取 conv id
    return content, tools

content, tools = sse_chat("请对审计项目 AP001（北京****有限公司2023年度）执行完整的年度审计流程，给出审计结论、审计报告、经审计财务报表和报表附注。")

print("=" * 70)
print("工具调用序列：")
for kind, name, arg in tools:
    if kind == "CALL":
        print(f"  [CALL] {name}  args={json.dumps(arg, ensure_ascii=False)[:150]}")
    elif kind == "RESULT":
        print(f"    -> {name}: {arg}")
    else:
        print(f"  [ERROR] {arg}")
print("=" * 70)
print("最终回答（前 2500 字）：")
print(content[:2500])
print("=" * 70)
print("回答总长度:", len(content))
