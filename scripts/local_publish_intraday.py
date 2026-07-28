"""Publish a provisional 14:30--14:40 phone view to GitHub Pages."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git") or str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "git" / "cmd" / "git.exe")


def run(*args: str, check: bool = True):
    return subprocess.run([GIT, *args], cwd=ROOT, text=True, capture_output=True, check=check)


def main() -> int:
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_intraday_screen.py")], cwd=ROOT)
    if result.returncode:
        return result.returncode
    run("add", "site/data/intraday-latest.json", "site/data")
    if run("diff", "--cached", "--quiet", check=False).returncode == 0:
        return 0
    run("commit", "-m", "chore: publish provisional intraday screen")
    run("push", "origin", "main")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
