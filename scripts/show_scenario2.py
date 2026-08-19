import urllib.request, json

BASE = "http://127.0.0.1:8001/api"
SID = "56e2006148e8499e8599f5c7c8145e60"
d = json.load(urllib.request.urlopen(f"{BASE}/scenarios/{SID}"))

out = []
out.append("=== 实体 (%d) ===" % len(d["entities"]))
for e in d["entities"]:
    out.append(f"  {e['id']}  {e['name']}  props={len(e.get('properties', []))}")
out.append("=== 关系 (%d) ===" % len(d["relations"]))
for r in d["relations"]:
    out.append(f"  {r['id']}  {r['name']}")
out.append("=== 映射 (%d) ===" % len(d["mappings"]))
for m in d["mappings"]:
    out.append(f"  {m['id']}  {m.get('entity_name', m.get('entity_id'))} -> {m.get('table_name')}")
out.append("=== 操作 (%d) ===" % len(d["actions"]))
for a in d["actions"]:
    out.append(f"  {a['id']}  {a['name']}  [{a.get('entity_name','')}]")
out.append("=== 规则 (%d) ===" % len(d["rules"]))
for r in d["rules"]:
    out.append(f"  {r['id']}  {r['name']}  [{r.get('entity_name','')}] {r.get('severity')}")
out.append("=== 工作流 (%d) ===" % len(d["workflows"]))
for w in d["workflows"]:
    out.append(f"  {w['id']}  {w['name']}  nodes={len(w.get('nodes', []))}")
out.append("=== 实例统计 ===")
out.append(f"  instances={len(d.get('instances', []))}  relation_instances={len(d.get('relation_instances', []))}")

open(r"e:\work\test\scripts\scenario_state.txt", "w", encoding="utf-8").write("\n".join(out))
print("WROTE", len(out), "lines")
