# -*- coding: utf-8 -*-
"""通过数据映射导入实例（规则 / 药品）"""
import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8001/api"

# 映射 ID（setup_mappings.py 创建）
MAPPING_RULE = "f959378e719c43bc93c75a5640287e7c"      # 规则 -> 规则表
MAPPING_DRUG = "11556e5244b74b258986f136484f0593"      # 药品 -> 项目明细表


def post(url, payload):
    req = urllib.request.Request(
        f"{BASE}{url}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return json.loads(urllib.request.urlopen(req, timeout=120).read().decode())


for name, mid, limit in [("规则", MAPPING_RULE, 50), ("药品", MAPPING_DRUG, 50)]:
    try:
        r = post(f"/scenarios/mappings/{mid}/import", {"limit": limit})
        print(f"{name}: {r}")
    except urllib.error.HTTPError as e:
        print(f"{name}: HTTP {e.code} {e.read().decode()}")
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: {exc}")
