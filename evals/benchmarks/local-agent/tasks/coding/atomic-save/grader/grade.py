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
    mod = load("atomic_save.py")
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "state.bin"; target.write_bytes(b"old")
        calls = []
        def replace(source, destination):
            calls.append((pathlib.Path(source), pathlib.Path(destination), pathlib.Path(source).read_bytes()))
            __import__("os").replace(source, destination)
        mod.atomic_save(target, b"new", replace=replace)
        assert target.read_bytes() == b"new" and calls[0][0].parent == target.parent and calls[0][2] == b"new"
        def fail(source, destination): raise OSError("boom")
        try: mod.atomic_save(target, b"bad", replace=fail)
        except OSError: pass
        else: raise AssertionError("replace failure swallowed")
        assert target.read_bytes() == b"new" and sorted(p.name for p in target.parent.iterdir()) == ["state.bin"]
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
