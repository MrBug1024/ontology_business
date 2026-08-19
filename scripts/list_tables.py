import sqlite3
con = sqlite3.connect(r"E:\work\test\backend\data\demo_bookkeeping.db")
cur = con.cursor()
lines = []
for (n,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    cnt = cur.execute(f"SELECT COUNT(*) FROM {n}").fetchone()[0]
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({n})")]
    lines.append(f"{n} {cnt} {cols}")
open(r"e:\work\test\scripts\db_tables.txt", "w", encoding="utf-8").write("\n".join(lines))
print("done", len(lines))
