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
    mod = load("cache.py")
    cache = mod.TTLCache(); cache.set("a", 1, 5, 10)
    assert cache.get("a", 14) == 1 and cache.get("a", 15, "miss") == "miss"
    assert cache.get("a", 11, "gone") == "gone"
    cache.set("a", 2, 1, 20); cache.set("a", 3, 3, 20)
    assert cache.get("a", 22) == 3 and cache.delete("a") is True and cache.delete("a") is False
    try: cache.set("x", 1, -1, 0)
    except ValueError: pass
    else: raise AssertionError("negative ttl accepted")
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
