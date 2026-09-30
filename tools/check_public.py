#!/usr/bin/env python3
"""Check the public source tree, never the user's live Codex state."""
from pathlib import Path
import re
import sys
ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", "build", ".venv"}
patterns = [re.compile(r"/Users/[A-Za-z0-9_.-]+/"),
            re.compile(r"(?:sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]{24,}"),
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")]
errors = []
count = 0
for path in ROOT.rglob("*"):
    if not path.is_file() or any(part in SKIP for part in path.relative_to(ROOT).parts):
        continue
    if path.is_symlink():
        errors.append(f"symlink: {path.relative_to(ROOT)}"); continue
    if path.suffix in {".sqlite3", ".sqlite", ".db", ".log"} or path.name.startswith(".env"):
        errors.append(f"private runtime file: {path.relative_to(ROOT)}"); continue
    try:
        data = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        continue
    count += 1
    for pattern in patterns:
        if pattern.search(data):
            errors.append(f"sensitive pattern {pattern.pattern!r}: {path.relative_to(ROOT)}")
if errors:
    print("\n".join(errors)); sys.exit(1)
print(f"Public source checks passed: {count} text files (heuristic scan, plus manual asset review).")
