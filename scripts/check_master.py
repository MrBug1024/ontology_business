import sqlite3
con = sqlite3.connect(r"E:\work\test\backend\data\demo_bookkeeping.db")
cur = con.cursor()
print("=== sqlite_master tables ===")
for row in cur.execute("SELECT type, name FROM sqlite_master ORDER BY type, name"):
    print(row)
print("=== integrity ===")
print(cur.execute("PRAGMA integrity_check").fetchone())
