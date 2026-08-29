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
    mod = load("cursor.py")
    value = mod.encode_cursor(12, {"tag": "中文", "state": ["a", "b"]})
    assert "=" not in value
    assert value == mod.encode_cursor(12, {"state": ["a", "b"], "tag": "中文"})
    assert mod.decode_cursor(value) == {"offset": 12, "filters": {"tag": "中文", "state": ["a", "b"]}}
    for bad in ("%%%", mod.encode_cursor(0, {})[:-1]):
        try: mod.decode_cursor(bad)
        except ValueError: pass
        else: raise AssertionError(bad)
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
