Fix `utf8_budget.py`. `truncate_utf8(text, max_bytes)` must return the longest Unicode prefix whose UTF-8 encoding fits the byte budget. Never split a code point; reject negative budgets.
