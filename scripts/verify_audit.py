import urllib.request, json

BASE = "http://127.0.0.1:8001/api"
SID = "56e2006148e8499e8599f5c7c8145e60"

def get(path):
    return json.load(urllib.request.urlopen(BASE + path))

def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return json.load(urllib.request.urlopen(req))

d = get(f"/scenarios/{SID}")

# 1. 找到年度审计流程工作流 ID
wf = [w for w in d["workflows"] if w["name"] == "年度审计流程"][0]
print("年度审计流程 ID:", wf["id"])

# 2. 执行 4 个审计操作
acts = {a["name"]: a["id"] for a in d["actions"]}
print("\n=== 执行审计操作 ===")
for name, aid in acts.items():
    if name.startswith("查询审计") or name.startswith("查询函证"):
        params = {}
        if name in ("查询审计底稿", "查询函证情况", "查询审计调整"):
            params = {"project_id": "AP002"}
        try:
            r = post(f"/scenarios/actions/{aid}/execute", {"params": params})
            rows = r.get("result", {}).get("rows", [])
            print(f"[OK] {name} params={params} -> {len(rows)} 行")
            for row in rows[:3]:
                print("   ", row)
        except Exception as e:
            print(f"[ERR] {name}: {e}")

# 3. 评估 4 个审计规则
rules = {r["name"]: r["id"] for r in d["rules"]}
print("\n=== 评估审计规则 ===")
cases = [
    ("函证未回函预警", {"回函状态": "未回函"}),
    ("函证差异预警", {"差异金额": 5000}),
    ("高风险审计项目预警", {"风险等级": "高"}),
    ("审计调整未接受预警", {"客户是否接受": "否"}),
]
for name, rec in cases:
    rid = rules.get(name)
    if not rid:
        print(f"[MISS] 规则 {name} 未找到")
        continue
    try:
        r = post(f"/scenarios/rules/{rid}/evaluate", {"record": rec})
        print(f"[{'HIT' if r.get('matched') else 'no '}] {name} record={rec} -> matched={r.get('matched')} severity={r.get('severity')}")
    except Exception as e:
        print(f"[ERR] {name}: {e}")

# 4. 执行年度审计流程（高风险分支）
print("\n=== 执行年度审计流程 (AP002, 高风险) ===")
try:
    r = post(f"/scenarios/workflows/{wf['id']}/execute", {"params": {"project_id": "AP002", "risk_level": "高"}})
    print("status:", r.get("status"), "duration_ms:", r.get("duration_ms"))
    for s in r.get("steps", []):
        print(f"   [{s.get('status')}] {s.get('name')} {s.get('error') or ''}")
except Exception as e:
    print("[ERR]", e)
