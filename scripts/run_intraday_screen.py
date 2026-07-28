"""Build a clearly labelled intraday (not-after-close) screening result.

The formal screen remains the authoritative 15:10 after-close result.  This
script only reads the already-complete local history cache, combines it with a
fresh full-market quote, and publishes a provisional snapshot for phone review
between 14:30 and 14:40.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import settings
from app.data_source import AkshareDataSource
from app.diagnostics import build_report
from scripts.run_formal_screen import CACHE, SITE, composite, jsonable, read_cache, score_candidate


NAMES = {
    "strong_close_momentum": "策略1：强势收盘隔夜动量（宽筛）",
    "recent_limit_up_trend": "策略2：近期涨停后的温和趋势（宽筛）",
    "limit_up_reacceleration": "策略3：涨停后整理再转强（宽筛）",
}


def main(market_date: str | None = None, force: bool = False) -> None:
    market_date = market_date or date.today().isoformat()
    now = datetime.now().astimezone()
    if not force and (now.date().isoformat() != market_date or not (14 <= now.hour < 15)):
        raise RuntimeError("盘中临时筛选只允许在交易日14:00至15:00运行。")

    source = AkshareDataSource(settings.request_retries, settings.request_timeout_seconds)
    spot = source.fetch_spot()
    fresh = spot[
        spot["code"].astype(str).str.zfill(6).str.startswith(("60", "00"))
        & ~spot["name"].astype(str).str.contains("ST", na=False)
    ].copy()
    fresh["code"] = fresh["code"].astype(str).str.zfill(6)
    if fresh.empty:
        raise RuntimeError("盘中实时行情获取失败，临时筛选未生成。")

    histories: dict[str, pd.DataFrame] = {}
    insufficient: list[str] = []
    for code in fresh["code"]:
        history = read_cache(code)
        if history is None or len(history) < 60:
            insufficient.append(code)
            continue
        snapshot = fresh.loc[fresh["code"] == code].iloc[0]
        histories[code] = composite(history, snapshot, market_date)

    coverage = len(histories) / len(fresh) if len(fresh) else 0
    if coverage < .95:
        raise RuntimeError(f"历史缓存覆盖率 {coverage:.2%} 低于95%，盘中临时筛选未生成。")

    complete_spot = fresh[fresh["code"].isin(histories)].copy()
    report = build_report(complete_spot, histories, market_date, source_updated_at=now.isoformat(timespec="seconds"))
    selected = []
    for code, item in report["stocks"].items():
        ids = [strategy_id for strategy_id, checks in item.get("strategies", {}).items() if all(check["status"] == "pass" for check in checks)]
        if not ids:
            continue
        review = score_candidate(item["metrics"], histories[code], market_date, ids)
        selected.append({
            "code": code, "name": item["name"], "strategy_ids": ids,
            "strategy_names": [NAMES[strategy_id] for strategy_id in ids],
            "metrics": item["metrics"], "score": review["score"],
            "priority_score": review["priority_score"], "score_category": review["category"],
            "score_detail": review,
            "failures": {strategy_id: [check["name"] for check in checks if check["status"] != "pass"] for strategy_id, checks in item["strategies"].items()},
        })
    selected.sort(key=lambda item: (-item["priority_score"], -len(item["strategy_ids"]), -float(item["metrics"].get("amount") or 0), item["code"]))

    raw_last = max((frame["date"].astype(str).str[:10].iloc[-2] for frame in histories.values()), default=None)
    strategy_counts = {strategy_id: sum(strategy_id in item["strategy_ids"] for item in selected) for strategy_id in NAMES}
    build_id = hashlib.sha256(f"intraday:{market_date}:{now.isoformat()}:{len(selected)}".encode()).hexdigest()[:12]
    payload = {
        "build_id": build_id, "data_version": "intraday-wide-v1", "market_date": market_date,
        "quote_time": now.isoformat(timespec="seconds"), "historical_last_date": raw_last,
        "used_snapshot_composite": True, "provisional": True, "formal": False,
        "disclaimer": "14:30—14:40盘中临时筛选；价格、成交量、量比和换手率尚未收盘确认，15:10正式结果会重新校验。",
        "coverage": coverage, "history_success": len(histories), "history_failed": len(insufficient),
        "excluded_history_insufficient": insufficient, "strategy_counts": strategy_counts,
        "union_count": len(selected), "multi_strategy_count": sum(len(item["strategy_ids"]) > 1 for item in selected),
        "records": jsonable(selected),
    }
    SITE.mkdir(parents=True, exist_ok=True)
    (SITE / f"intraday-{market_date}.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    (SITE / "intraday-latest.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps({"market_date": market_date, "quote_time": payload["quote_time"], "coverage": coverage, "counts": strategy_counts, "union": len(selected)}, ensure_ascii=False))


if __name__ == "__main__":
    main(force="--force" in sys.argv)
