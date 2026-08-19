import sqlite3

con = sqlite3.connect(r"E:\work\test\backend\data\demo_bookkeeping.db")
cur = con.cursor()
for (name,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({name})")]
    n = cur.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    print(name, n, cols)
