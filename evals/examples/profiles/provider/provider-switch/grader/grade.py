from pathlib import Path

raise SystemExit(0 if Path("provider-switch.txt").read_text(encoding="utf-8") == "switched\n" else 1)
