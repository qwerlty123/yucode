# ruff: noqa
# fmt: off
def encode_cursor(offset, filters):
    return str(offset)

def decode_cursor(value):
    return {"offset": int(value), "filters": {}}
