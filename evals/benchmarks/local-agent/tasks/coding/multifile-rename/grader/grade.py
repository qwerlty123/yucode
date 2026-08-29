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
    mod = load("rename_symbol.py")
    files = {"a.py": "value = 1\nprint(value)  # value stays in comment\n", "b.py": "label = 'value'\ndef f(value): return value\n"}
    got = mod.rename_symbol(files, "value", "item")
    assert got["a.py"] == "item = 1\nprint(item)  # value stays in comment\n"
    assert got["b.py"] == "label = 'value'\ndef f(item): return item\n"
    assert files["a.py"].startswith("value")
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
