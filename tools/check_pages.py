"""Syntax-check the JavaScript inside the served HTML pages.

The pages are plain files with no build step, which is the right trade for two
small apps but means nothing catches a broken string literal until a browser
refuses to run the script and shows a blank screen. This does catch it.

    python tools/check_pages.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "backend" / "src" / "scoreboard" / "web"
SCRIPT = re.compile(r"<script>([\s\S]*?)</script>")


def main() -> int:
    node = shutil.which("node")
    if not node:
        print("node is not installed; skipping.")
        return 0

    failures = 0
    for page in sorted(WEB.glob("*.html")):
        blocks = SCRIPT.findall(page.read_text(encoding="utf-8"))
        if not blocks:
            print(f"  {page.name}: no inline script")
            continue
        for index, block in enumerate(blocks):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(block)
                temp = handle.name
            result = subprocess.run(
                [node, "--check", temp], capture_output=True, text=True
            )
            Path(temp).unlink(missing_ok=True)
            if result.returncode == 0:
                print(f"  {page.name} [{index}]: ok")
            else:
                failures += 1
                print(f"  {page.name} [{index}]: FAILED")
                print("    " + result.stderr.strip().splitlines()[0])
                for line in result.stderr.strip().splitlines()[1:6]:
                    print("    " + line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
