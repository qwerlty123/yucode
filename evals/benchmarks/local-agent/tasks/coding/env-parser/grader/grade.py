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
    mod = load("env_parser.py")
    text = chr(10).join(['# comment', ' export PORT = 8080 ', 'NAME="hello world"', 'EMPTY=', 'TOKEN=a=b=c', "SINGLE='x y'", ''])
    assert mod.parse_env(text) == {"PORT": "8080", "NAME": "hello world", "EMPTY": "", "TOKEN": "a=b=c", "SINGLE": "x y"}
    for bad in ("NO_EQUALS", "1BAD=x", "A B=x"):
        try: mod.parse_env(bad)
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
