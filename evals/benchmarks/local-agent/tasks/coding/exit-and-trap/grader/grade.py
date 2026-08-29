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
        marker = pathlib.Path(tmp) / 'marker'
        failed = subprocess.run(['bash','run_with_trap.sh',str(marker),'sh','-c','exit 7'])
        temp_path = pathlib.Path(marker.read_text().strip())
        assert failed.returncode == 7 and not temp_path.exists()
        ok = subprocess.run(['bash','run_with_trap.sh',str(marker),'sh','-c','exit 0'])
        assert ok.returncode == 0 and not pathlib.Path(marker.read_text().strip()).exists()
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
