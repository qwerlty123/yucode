from pathlib import Path

raise SystemExit(0 if Path("strict-tools.txt").read_text(encoding="utf-8") == "strict schema accepted\n" else 1)
