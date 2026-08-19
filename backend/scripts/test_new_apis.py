# -*- coding: utf-8 -*-
"""验证本体扩展 API：Actions / Rules / Events / Workflows / 执行日志"""
import json
import urllib.request

BASE = "http://127.0.0.1:8001/api"


def req(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def main():
    # 1. 场景列表（应含新计数字段）
    st, scenarios = req("GET", "/scenarios")
    print(f"[1] GET /scenarios -> {st}, {len(scenarios)} 个场景")
    s0 = scenarios[0]
    print(f"    字段检查: action_count={s0.get('action_count')}, rule_count={s0.get('rule_count')}, "
          f"event_count={s0.get('event_count')}, workflow_count={s0.get('workflow_count')}")
    sid = s0["id"]

    # 2. 场景详情（应含 actions/rules/events/workflows 列表）
    st, detail = req("GET", f"/scenarios/{sid}")
    print(f"[2] GET /scenarios/{sid[:8]} -> {st}, "
          f"actions={len(detail.get('actions', []))}, rules={len(detail.get('rules', []))}, "
          f"events={len(detail.get('events', []))}, workflows={len(detail.get('workflows', []))}")

    # 3. 取一个实体用于创建 Action
    ent = detail["entities"][0]
    print(f"    使用实体: {ent['name']} ({ent['id'][:8]})")

    # 4. 创建 Action（script 执行器，便于直接执行验证）
    st, action = req("POST", f"/scenarios/{sid}/actions", {
        "entity_id": ent["id"],
        "name": "测试操作",
        "description": "API 验证用",
        "executor_type": "script",
        "executor_config": {"script": "result = {'ok': True, 'echo': params}"},
        "input_schema": {"msg": {"type": "string"}},
        "enabled": True,
    })
    print(f"[4] POST actions -> {st}, id={action.get('id', '?')[:8]}")
    aid = action["id"]

    # 5. 执行 Action
    st, res = req("POST", f"/scenarios/actions/{aid}/execute", {"params": {"msg": "hello"}})
    print(f"[5] POST actions/{aid[:8]}/execute -> {st}, status={res.get('status')}, result={res.get('result')}")

    # 6. 创建 Rule 并评估
    st, rule = req("POST", f"/scenarios/{sid}/rules", {
        "name": "测试规则",
        "description": "数量 > 2 时命中",
        "entity_id": ent["id"],
        "condition": {"field": "数量", "op": ">", "value": 2},
        "action_on_match": "标记违规",
        "severity": "warning",
        "enabled": True,
    })
    print(f"[6] POST rules -> {st}, id={rule.get('id', '?')[:8]}")
    rid = rule["id"]

    st, ev1 = req("POST", f"/scenarios/rules/{rid}/evaluate", {"record": {"数量": 5}})
    st2, ev2 = req("POST", f"/scenarios/rules/{rid}/evaluate", {"record": {"数量": 1}})
    print(f"    评估 数量=5 -> matched={ev1.get('matched')}; 数量=1 -> matched={ev2.get('matched')}")

    # 7. 创建 Event
    st, event = req("POST", f"/scenarios/{sid}/events", {
        "name": "测试事件",
        "description": "API 验证用",
        "trigger_source": "手动",
        "payload_schema": {"id": {"type": "string"}},
        "enabled": True,
    })
    print(f"[7] POST events -> {st}, id={event.get('id', '?')[:8]}")
    eid = event["id"]

    # 8. 创建 Workflow（action + rule + event 三步）并执行
    st, wf = req("POST", f"/scenarios/{sid}/workflows", {
        "name": "测试工作流",
        "description": "API 验证用",
        "trigger_type": "manual",
        "steps": [
            {"type": "action", "action_id": aid, "params": {"msg": "wf"}},
            {"type": "rule", "rule_id": rid, "record": {"数量": 9}},
            {"type": "event", "event_id": eid, "payload": {"id": "x1"}},
        ],
        "enabled": True,
    })
    print(f"[8] POST workflows -> {st}, id={wf.get('id', '?')[:8]}")
    wid = wf["id"]

    st, wres = req("POST", f"/scenarios/workflows/{wid}/execute", {"params": {}})
    steps = wres.get("steps", [])
    print(f"    执行工作流 -> {st}, status={wres.get('status')}, 步骤数={len(steps)}")
    for s in steps:
        print(f"      step {s.get('step')} [{s.get('type')}] -> {s.get('status')}")

    # 9. 执行日志
    st, logs = req("GET", f"/scenarios/{sid}/execution-logs")
    print(f"[9] GET execution-logs -> {st}, {len(logs)} 条")
    for lg in logs[:5]:
        print(f"    {lg['target_type']}/{lg['target_name']} -> {lg['status']} ({lg.get('duration_ms')}ms)")

    # 10. 清理测试数据
    for path in [f"/scenarios/actions/{aid}", f"/scenarios/rules/{rid}", f"/scenarios/events/{eid}", f"/scenarios/workflows/{wid}"]:
        st, _ = req("DELETE", path)
        print(f"[10] DELETE {path.split('/')[1]}/{path.split('/')[2][:8]} -> {st}")

    print("\n全部验证完成 ✓")


if __name__ == "__main__":
    main()
