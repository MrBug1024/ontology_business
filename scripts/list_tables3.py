import sqlite3
con = sqlite3.connect(r"E:\work\test\backend\data\demo_bookkeeping.db")
out = []
for (n,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    cnt = con.execute("SELECT COUNT(*) FROM " + n).fetchone()[0]
    cols = [r[1] for r in con.execute("PRAGMA table_info(" + n + ")")]
    out.append(n + " | " + str(cnt) + " | " + ",".join(cols))
with open(r"e:\work\test\scripts\tbl_v3.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("WROTE", len(out), "tables")
