Fix `plugin_order.py`. Topologically order a `{plugin: dependencies}` mapping with dependencies before dependents and lexical tie-breaking. Reject missing dependencies and cycles with `ValueError`.
