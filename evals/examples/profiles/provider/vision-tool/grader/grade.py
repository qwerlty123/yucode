from pathlib import Path

raise SystemExit(0 if Path("vision-tool.txt").read_text(encoding="utf-8") == "red\n" else 1)
