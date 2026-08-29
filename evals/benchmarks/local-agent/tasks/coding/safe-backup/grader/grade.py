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
    import subprocess
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); source = root / 'name;$(touch PWNED) with spaces.txt'; destination = root / 'backups'; destination.mkdir(); source.write_text('payload', encoding='utf-8')
        completed = subprocess.run(['bash','backup.sh',str(source),str(destination)], text=True, capture_output=True)
        assert completed.returncode == 0, completed.stderr
        target = destination / (source.name + '.bak'); assert target.read_text() == 'payload'
        assert not (root / 'PWNED').exists() and sorted(p.name for p in destination.iterdir()) == [target.name]
        again = subprocess.run(['bash','backup.sh',str(source),str(destination)])
        assert again.returncode != 0 and target.read_text() == 'payload'
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
