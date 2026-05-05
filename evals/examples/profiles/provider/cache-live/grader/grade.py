from pathlib import Path

raise SystemExit(0 if Path("README.md").read_text(encoding="utf-8") == "cache telemetry fixture\n" else 1)
