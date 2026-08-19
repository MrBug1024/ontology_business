# -*- coding: utf-8 -*-
import urllib.request, json

d = json.load(urllib.request.urlopen('http://127.0.0.1:8001/api/scenarios/56e2006148e8499e8599f5c7c8145e60'))
print('=== 实体 ===')
for e in d['entities']:
    props = [p['name'] for p in e.get('properties', [])]
    print(f"{e['name']} ({e['id'][:8]}) props={props}")
print()
print('=== 关系 ===')
for r in d['relations']:
    print(f"{r['name']}: {r.get('source_entity_name','?')} -> {r.get('target_entity_name','?')}")
print()
print('=== 财务报表实例 ===')
for inst in d.get('instances', []):
    if inst.get('entity_name') == '财务报表':
        print(inst.get('data', {}))
print()
print('=== 映射 ===')
for m in d.get('mappings', []):
    print(f"{m.get('entity_name')} -> {m.get('table_name')}")
