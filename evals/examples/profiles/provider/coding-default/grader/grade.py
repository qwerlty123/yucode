from pathlib import Path

passed = Path("coding-default.txt").read_text(encoding="utf-8") == "coding profile works\n"
passed = passed and Path("README.md").read_text(encoding="utf-8") == "provider coding fixture\n"
raise SystemExit(0 if passed else 1)
