# ruff: noqa
# fmt: off
async def run_with_cleanup(work, cleanup):
    return await work()
