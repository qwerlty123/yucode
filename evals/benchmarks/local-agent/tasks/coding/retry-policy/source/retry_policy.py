# ruff: noqa
# fmt: off
def retry_delays(attempts, base, cap, retry_after=None):
    return [base] * attempts
