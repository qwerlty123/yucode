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
    mod = load("retry_policy.py")
    assert mod.retry_delays(1, 2, 8) == []
    assert mod.retry_delays(5, 2, 8) == [2, 4, 8, 8]
    assert mod.retry_delays(4, 2, 8, 5) == [5, 5, 8]
    for args in ((0, 1, 2, None), (2, 3, 2, None), (2, 1, 2, -1)):
        try: mod.retry_delays(*args)
        except ValueError: pass
        else: raise AssertionError(args)
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
