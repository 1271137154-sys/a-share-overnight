"""Cache public company profile fields for screened candidates.

Only the official CNINFO company-overview endpoint is used.  It intentionally
does not invent a catalyst, concept, or cross-industry narrative.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import akshare as ak

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "company_profiles.json"
OUTPUT = ROOT / "site" / "data" / "company-profiles.json"
FORMAL = ROOT / "site" / "data" / "formal-latest.json"


def fetch_profile(code: str) -> tuple[str, dict | None]:
    for attempt in range(3):
        try:
            frame = ak.stock_profile_cninfo(code)
            if frame.empty:
                return code, None
            row = frame.iloc[0]
            return code, {
                "industry": str(row.get("所属行业", "未披露")),
                "business": str(row.get("主营业务", "未披露")),
                "scope": str(row.get("经营范围", "未披露"))[:500],
                "source": "CNINFO company profile",
            }
        except Exception:
            time.sleep(attempt + 1)
    return code, None


def main() -> None:
    existing = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    payload = json.loads(FORMAL.read_text(encoding="utf-8")) if FORMAL.exists() else {"records": []}
    codes = [str(item["code"]).zfill(6) for item in payload.get("records", []) if str(item["code"]).zfill(6) not in existing]
    # This CNINFO adapter embeds a JavaScript engine which is not thread-safe
    # on Windows.  Sequential requests plus checkpointing are slower once,
    # but stable and resume cleanly after an interruption.
    for index, code in enumerate(codes, start=1):
        returned_code, profile = fetch_profile(code)
        if profile:
            existing[returned_code] = profile
        if index % 10 == 0:
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(existing, ensure_ascii=False, indent=2)
    CACHE.write_text(text, encoding="utf-8")
    OUTPUT.write_text(text, encoding="utf-8")
    print(json.dumps({"cached_profiles": len(existing), "new_requests": len(codes)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
