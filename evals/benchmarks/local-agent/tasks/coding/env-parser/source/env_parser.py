# ruff: noqa
# fmt: off
def parse_env(text):
    return dict(line.split("=") for line in text.splitlines())
