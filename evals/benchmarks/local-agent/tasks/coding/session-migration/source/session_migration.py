# ruff: noqa
# fmt: off
def migrate_session(data):
    data["schema_version"] = 3
    return data
