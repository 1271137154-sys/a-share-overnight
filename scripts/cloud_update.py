"""Run and publish a cloud-hosted market-day update for GitHub Actions."""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_source import AkshareDataSource


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("intraday", "close"), required=True)
    args = parser.parse_args()
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    if not source.is_trading_day(date.today()):
        print("Not an A-share trading day; skipped.")
        return 0

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    if args.mode == "intraday" and not (14 <= now.hour < 15):
        print("Outside the 14:00-15:00 China-time intraday window; skipped.")
        return 0
    if args.mode == "close" and now.hour < 15:
        print("Before China-market close; skipped.")
        return 0

    if args.mode == "intraday":
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_intraday_screen.py"), "--force"], cwd=ROOT)
        return result.returncode

    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_formal_screen.py")], cwd=ROOT)
    if result.returncode:
        return result.returncode
    # Enrich newly selected stocks, then score once more using that public
    # industry profile for exact industry-board matching.
    subprocess.run([sys.executable, str(ROOT / "scripts" / "enrich_company_profiles.py")], cwd=ROOT, check=False)
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_formal_screen.py"), "--stored-snapshot"], cwd=ROOT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
