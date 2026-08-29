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
    mod = load("tool_pairing.py")
    calls = [{"id": "b", "name": "Read"}, {"id": "a", "name": "Search"}]
    messages = [{"role": "assistant", "tool_calls": calls}, {"role": "tool", "tool_call_id": "a", "content": "A"}, {"role": "tool", "tool_call_id": "b", "content": "B"}]
    assert [pair[0]["id"] for pair in mod.pair_tool_messages(messages)] == ["a", "b"]
    bad = [[{"role": "tool", "tool_call_id": "x"}], [{"role": "assistant", "tool_calls": calls}], messages + [{"role": "tool", "tool_call_id": "a"}]]
    for value in bad:
        try: mod.pair_tool_messages(value)
        except ValueError: pass
        else: raise AssertionError(value)
except BaseException as exc:
    result = {'passed': False, 'error': f'{type(exc).__name__}: {exc}'}
else:
    result = {'passed': True}
output = pathlib.Path(os.environ.get('YUCODE_EVAL_OUTPUT', '.'))
output.mkdir(parents=True, exist_ok=True)
(output / 'grade.json').write_text(json.dumps(result, sort_keys=True) + '\n', encoding='utf-8')
print(json.dumps(result, sort_keys=True))
raise SystemExit(0 if result['passed'] else 1)
