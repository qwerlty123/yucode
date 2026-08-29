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
    import copy
    mod = load("session_migration.py")
    old = {"schema_version": 1, "messages": [{"role":"user"}], "metadata": {"cwd":"/repo"}}
    before = copy.deepcopy(old); migrated = mod.migrate_session(old)
    assert old == before
    assert migrated == {"schema_version":3,"turns":[{"role":"user"}],"metadata":{"workspace":"/repo"}}
    assert mod.migrate_session(migrated) == migrated
    try: mod.migrate_session({"schema_version": 9})
    except ValueError: pass
    else: raise AssertionError("unknown version accepted")
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
