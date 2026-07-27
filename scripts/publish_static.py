"""Build the free GitHub Pages dashboard from the same screening engine used locally."""
from __future__ import annotations
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.database import Database
from app.data_source import AkshareDataSource
from app.strategies import load_strategies
from app.tasks import run_daily_screen

SITE_DATA = ROOT / "site" / "data"

def jsonable(value):
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value

def build_payload(db: Database, trade_date: str, strategies: list[dict]) -> dict:
    names = {item["id"]: item["name"] for item in strategies}
    records = db.load_selections(trade_date)
    for item in records:
        item["strategy_names"] = [names[key] for key in item["strategy_ids"]]
    return {"trade_date": trade_date, "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "records": jsonable(records), "strategy_counts": db.strategy_counts(trade_date), "rejections": jsonable(db.load_rejections(trade_date, limit=500))}

def main():
    db = Database(settings.database_path)
    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    strategies = load_strategies(ROOT / "config" / "strategies.json")
    result = run_daily_screen(db, source, strategies)
    if result["status"] != "success":
        print(f"Static dashboard not updated: {result['message']}")
        return result
    dates = db.dates()
    if not dates:
        raise RuntimeError(f"No saved screening data: {result['message']}")
    trade_date = dates[0]
    payload = build_payload(db, trade_date, strategies)
    SITE_DATA.mkdir(parents=True, exist_ok=True)
    (SITE_DATA / f"{trade_date}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    existing = []
    manifest_path = SITE_DATA / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text(encoding="utf-8")).get("dates", [])
    dates = [trade_date] + [item for item in existing if item != trade_date]
    manifest_path.write_text(json.dumps({"dates": dates[:90], "latest": trade_date}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Static dashboard built for {trade_date}: {len(payload['records'])} candidates")
    return result

if __name__ == "__main__":
    main()
