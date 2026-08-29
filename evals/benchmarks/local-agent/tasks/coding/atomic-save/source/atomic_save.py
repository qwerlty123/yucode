# ruff: noqa
# fmt: off
def atomic_save(path, data, replace=None):
    open(path, "wb").write(data)
