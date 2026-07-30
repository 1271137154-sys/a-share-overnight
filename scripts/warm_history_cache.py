"""Populate the reusable cloud history cache without publishing a screen."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_source import AkshareDataSource
from scripts.run_formal_screen import fetch_history


def main() -> int:
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    spot = source.fetch_spot()
    universe = spot[
        spot["code"].astype(str).str.zfill(6).str.startswith(("60", "00"))
        & ~spot["name"].astype(str).str.contains("ST", na=False)
    ].copy()
    codes = universe["code"].astype(str).str.zfill(6).tolist()
    complete = failed = 0
    # Intentionally limited: four concurrent requests balances speed with
    # public-data-source rate limits.
    with ThreadPoolExecutor(max_workers=4) as executor:
        jobs = {executor.submit(fetch_history, source, code): code for code in codes}
        for index, job in enumerate(as_completed(jobs), start=1):
            _, origin = job.result()
            if str(origin).startswith("failed"):
                failed += 1
            else:
                complete += 1
            if index % 100 == 0 or index == len(codes):
                print(f"history cache: {index}/{len(codes)} complete={complete} failed={failed}", flush=True)
    print(f"history cache finished: complete={complete} total={len(codes)} failed={failed}")
    return 0 if complete / len(codes) >= 0.95 else 1


if __name__ == "__main__":
    raise SystemExit(main())
