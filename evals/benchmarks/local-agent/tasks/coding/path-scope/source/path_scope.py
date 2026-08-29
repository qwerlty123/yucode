# ruff: noqa
# fmt: off
from pathlib import Path
def resolve_scoped(root, candidate):
    return Path(root) / candidate
