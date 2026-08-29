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
    mod = load("utf8_budget.py")
    assert mod.truncate_utf8("A你🙂B", 0) == ""
    assert mod.truncate_utf8("A你🙂B", 4) == "A你"
    assert mod.truncate_utf8("A你🙂B", 8) == "A你🙂"
    assert mod.truncate_utf8("é", 1) == ""
    try: mod.truncate_utf8("x", -1)
    except ValueError: pass
    else: raise AssertionError("negative budget accepted")
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
