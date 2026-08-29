Fix `config.py`. `merge_config(defaults, file_values, env_values, cli_values)` must apply layers in that order, ignore `None` values, keep unknown keys, and never mutate an input mapping.
