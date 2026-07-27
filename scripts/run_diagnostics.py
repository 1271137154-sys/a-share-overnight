"""Build a full-universe audit report for the three wide-screen strategies."""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.data_source import AkshareDataSource
from app.diagnostics import build_report
from app.config import settings


def main():
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    spot = source.fetch_spot()
    trade_date = date.today().isoformat()
    mainboard = spot[spot["code"].astype(str).str.zfill(6).str.startswith(("60", "00"))].copy()
    histories = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        jobs = {executor.submit(source.fetch_history, code, 35): str(code).zfill(6) for code in mainboard["code"]}
        for future in as_completed(jobs):
            code = jobs[future]
            try:
                histories[code] = future.result()
            except Exception:
                histories[code] = None
    report = build_report(spot, histories, trade_date)
    path = ROOT / "site" / "data" / f"diagnostics-{trade_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, default=str, indent=2)
    path.write_text(text, encoding="utf-8")
    (ROOT / "site" / "data" / "diagnostics-latest.json").write_text(text, encoding="utf-8")
    print(f"diagnostics written: {path}; mainboard={report['baseline']['mainboard']}")

if __name__ == "__main__":
    main()
