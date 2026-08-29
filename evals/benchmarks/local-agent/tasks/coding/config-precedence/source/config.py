# ruff: noqa
# fmt: off
def merge_config(defaults, file_values, env_values, cli_values):
    result = dict(cli_values)
    result.update(defaults)
    return result
