from pathlib import Path

raise SystemExit(0 if Path("indexed.txt").read_text(encoding="utf-8") == "index located\n" else 1)
