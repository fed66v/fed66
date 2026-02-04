import sqlite3

conn = sqlite3.connect("data.db")
c = conn.cursor()

c.execute("""
CREATE TABLE IF NOT EXISTS users (
    name TEXT PRIMARY KEY,
    user_id TEXT NOT NULL
)
""")

data = [
    ("ahmed", "111111111111111111"),
    ("محمد", "222222222222222222"),
    ("sara", "333333333333333333"),
    ("علي", "444444444444444444")
]

c.executemany("INSERT OR REPLACE INTO users VALUES (?, ?)", data)

conn.commit()
conn.close()

print("✅ Database created successfully")
