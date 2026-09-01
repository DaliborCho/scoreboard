"""Rename the working codename to the real product name.

`scoreboard` is a placeholder chosen so the name could be decided later. It
appears in the Python package, the Docker service names, the database name and
the token prefixes. Run this once when the product name is settled:

    python tools/rename.py acme

Pass --dry-run first to see what would change.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

OLD = "scoreboard"
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "postgres-data", "certs"}
TEXT_SUFFIXES = {
    ".py", ".toml", ".yml", ".yaml", ".md", ".txt", ".cfg", ".ini",
    ".json", ".ts", ".tsx", ".js", ".html", ".env", ".example",
}


def is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in {"Dockerfile", ".env.example", ".gitignore"}


def walk(root: Path):
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_name", help="lowercase identifier, e.g. 'podium'")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    new = args.new_name.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", new):
        print("Name must be a lowercase Python identifier: letters, digits, underscore.")
        return 1
    if new == OLD:
        print("That is already the current name.")
        return 1

    root = Path(__file__).resolve().parents[1]
    edited, renamed = 0, 0

    for path in walk(root):
        if path.is_file() and is_text(path):
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError):
                continue
            if OLD not in content and OLD.upper() not in content:
                continue
            updated = content.replace(OLD, new).replace(OLD.upper(), new.upper())
            updated = updated.replace(OLD.capitalize(), new.capitalize())
            edited += 1
            print(f"  edit    {path.relative_to(root)}")
            if not args.dry_run:
                path.write_text(updated, encoding="utf-8")

    for path in sorted(walk(root), key=lambda p: len(p.parts), reverse=True):
        if OLD in path.name:
            target = path.with_name(path.name.replace(OLD, new))
            renamed += 1
            print(f"  rename  {path.relative_to(root)} -> {target.name}")
            if not args.dry_run:
                shutil.move(str(path), str(target))

    verb = "would change" if args.dry_run else "changed"
    print(f"\n{verb}: {edited} files edited, {renamed} paths renamed")
    if not args.dry_run:
        print("\nNext: docker compose down -v && docker compose up -d --build")
        print("The database name changes, so the old volume must be dropped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
