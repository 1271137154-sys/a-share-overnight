"""Run the daily screen on this Windows computer and publish its static result.

Designed for Windows Task Scheduler.  It never stores GitHub credentials: the
existing Git credential manager handles the authenticated push.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
GIT_PATH = (
    shutil.which("git")
    or str(Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "native" / "git" / "cmd" / "git.exe")
)


def run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [GIT_PATH, *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_formal_screen.py")], cwd=ROOT)
    if completed.returncode:
        logging.error("Formal screen failed; no phone result will be published.")
        return completed.returncode

    run_git("add", "site/data")
    if not run_git("diff", "--cached", "--quiet", check=False).returncode:
        logging.info("Screening output has not changed; no publish needed.")
        return 0

    run_git("commit", "-m", "chore: update daily screening results")
    run_git("push", "origin", "main")
    logging.info("Daily screening result pushed to GitHub Pages.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or str(exc), file=sys.stderr)
        raise SystemExit(1)
