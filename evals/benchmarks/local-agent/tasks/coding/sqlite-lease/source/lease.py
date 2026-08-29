# ruff: noqa
# fmt: off
def claim(conn, worker, now, ttl):
    row = conn.execute("select id from jobs limit 1").fetchone()
    return row[0] if row else None
