from pathlib import Path

raise SystemExit(0 if Path("README.md").read_text(encoding="utf-8") == "provider builtin fixture\n" else 1)
