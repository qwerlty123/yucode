# ruff: noqa
# fmt: off
import importlib.util
import json
import os
import pathlib
import sys
import tempfile

def load(path):
    spec = importlib.util.spec_from_file_location('candidate', path)
    if spec is None or spec.loader is None:
        raise ImportError(f'cannot load {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

try:
    import sqlite3
    mod = load("lease.py")
    conn = sqlite3.connect(":memory:")
    conn.execute("create table jobs(id integer primary key, status text, lease_owner text, lease_until integer)")
    conn.executemany("insert into jobs values(?,?,?,?)", [(2,"pending",None,0),(1,"running","old",5),(3,"running","busy",50)])
    conn.commit()
    assert mod.claim(conn, "w", 10, 7) == 1
    assert conn.execute("select status,lease_owner,lease_until from jobs where id=1").fetchone() == ("running","w",17)
    assert mod.claim(conn, "w", 10, 7) == 2 and mod.claim(conn, "w", 10, 7) is None
    try: mod.claim(conn, "w", 0, 0)
    except ValueError: pass
    else: raise AssertionError("zero ttl accepted")
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
