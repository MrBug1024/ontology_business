# -*- coding: utf-8 -*-
"""Dump conversation 3b11881ce2df4bddb10c1f92c694d0d3 in full."""
import json
import sqlite3
import sys

DB = r"f:\test\backend\data\platform.db"
CID = "3b11881ce2df4bddb10c1f92c694d0d3"

_builtin_print = print
OUT = open(r"f:\test\backend\scripts\chat_3b11_full.txt", "w", encoding="utf-8")
def print(*args, **kwargs):
    kwargs.pop("file", None)
    _builtin_print(*args, file=OUT, **kwargs)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT * FROM conversations WHERE id=?", (CID,))
conv = cur.fetchone()
if conv is None:
    print("conversation not found")
    raise SystemExit(1)

print("=" * 80)
print("CONVERSATION:", dict(conv))
print("=" * 80)

cur.execute(
    "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC, id ASC",
    (CID,),
)
msgs = cur.fetchall()
print(f"total messages: {len(msgs)}")
for i, m in enumerate(msgs):
    d = dict(m)
    print("\n" + "#" * 80)
    print(f"[{i}] role={d.get('role')}  created={d.get('created_at')}")
    content = d.get("content") or ""
    print("-" * 80)
    print(content)
    # extra fields
    for k in ("tool_calls", "tool_call_id", "name", "metadata", "attachments"):
        if k in d and d[k]:
            print(f"  --{k}--")
            try:
                print(json.dumps(json.loads(d[k]), ensure_ascii=False, indent=2))
            except Exception:
                print(d[k])
conn.close()
OUT.close()
