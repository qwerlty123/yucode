# ruff: noqa
# fmt: off
class TTLCache:
    def __init__(self): self.values = {}
    def set(self, key, value, ttl, now): self.values[key] = value
    def get(self, key, now, default=None): return self.values.get(key, default)
    def delete(self, key): return False
