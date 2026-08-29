# ruff: noqa
# fmt: off
def rename_symbol(files, old, new):
    return {path: text.replace(old, new) for path, text in files.items()}
