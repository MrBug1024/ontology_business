"""Dump the first conversation of the yibao audit agent to a text file."""
import sqlite3

AGENT_ID = "83bcf35455834a4284c1b5eaaafe07b4"
DB = r"f:\test\backend\data\platform.db"

con = sqlite3.connect(DB)
cur = con.cursor()
rows = cur.execute(
    "select id, title, created_at from conversations where agent_id=? order by created_at",
    (AGENT_ID,),
).fetchall()
print("conversations:")
for r in rows:
    print(r)

if not rows:
    raise SystemExit(1)

conv_id = rows[0][0]
msgs = cur.execute(
    "select role, content, tool_calls, tool_results, created_at from messages where conversation_id=? order by created_at",
    (conv_id,),
).fetchall()

out = []
for i, (role, content, tool_calls, tool_results, ts) in enumerate(msgs):
    out.append(f"\n{'='*80}\n[#{i}] role={role} time={ts}\n{'='*80}")
    if content:
        out.append(f"--- content ---\n{content}")
    if tool_calls:
        out.append(f"--- tool_calls ---\n{tool_calls}")
    if tool_results:
        out.append(f"--- tool_results ---\n{tool_results}")

text = "\n".join(out)
with open(r"f:\test\backend\scripts\first_chat_dump.txt", "w", encoding="utf-8") as f:
    f.write(text)
print(f"\nwritten {len(text)} chars, {len(msgs)} messages")
