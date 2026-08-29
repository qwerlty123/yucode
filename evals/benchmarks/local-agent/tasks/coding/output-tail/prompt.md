Fix `output_tail.py`. Given an iterable of byte chunks, return exactly the last `limit` bytes without joining unbounded history. Zero returns empty and negative limits raise `ValueError`.
