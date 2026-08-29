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
    mod = load("path_scope.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "root"; outside = pathlib.Path(tmp) / "outside"
        root.mkdir(); outside.mkdir(); (root / "nested").mkdir()
        assert mod.resolve_scoped(root, "nested/file.txt") == (root / "nested/file.txt").resolve()
        for bad in ("../outside/x", str(outside / "x")):
            try: mod.resolve_scoped(root, bad)
            except ValueError: pass
            else: raise AssertionError(bad)
        (root / "link").symlink_to(outside, target_is_directory=True)
        try: mod.resolve_scoped(root, "link/x")
        except ValueError: pass
        else: raise AssertionError("symlink escape")
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
