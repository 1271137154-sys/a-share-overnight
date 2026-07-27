"""Idempotent daily screening task shared by the scheduler and administrator button."""
import logging
from datetime import date
from concurrent.futures import ThreadPoolExecutor, as_completed
import pandas as pd
from .database import Database
from .data_source import MarketDataSource
from .strategies import analyze_universe

logger = logging.getLogger(__name__)
TASK_NAME = "daily_screen"

def run_daily_screen(db: Database, source: MarketDataSource, strategies: list[dict], trade_date: date | None = None) -> dict:
    day = trade_date or date.today()
    date_key = day.isoformat()
    if not source.is_trading_day(day):
        db.finish_task(TASK_NAME, date_key, "skipped", "非A股交易日，未执行筛选") if db.begin_task(TASK_NAME, date_key) else None
        return {"status": "skipped", "trade_date": date_key, "records": 0, "message": "非A股交易日"}
    if not db.begin_task(TASK_NAME, date_key):
        existing = db.latest_task(TASK_NAME)
        return {"status": "already_completed", "trade_date": date_key, "records": existing.get("records_count") if existing else 0, "message": "今日任务已成功完成，未重复执行"}
    try:
        frame = source.fetch_spot()
        db.save_snapshot(date_key, frame, source.source_name)
        # Every configured strategy requires a 2%–7.5% daily gain and at least
        # RMB 300m turnover.  Filter cheaply before requesting historical bars
        # from a public source, rather than fetching arbitrary market rows.
        research_pool = frame[
            frame["pct_change"].between(2, 7.5)
            & (frame["amount"] >= 300_000_000)
        ].sort_values("amount", ascending=False).head(200)
        histories = {}
        # Public historical endpoints are network-bound.  A small worker pool
        # keeps the 14:40 task timely without changing any screening rule.
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(source.fetch_history, code): code for code in research_pool["code"]}
            for future in as_completed(futures):
                code = futures[future]
                try:
                    histories[code] = future.result()
                except Exception as exc:
                    logger.warning("History task for %s failed: %s", code, exc)
                    histories[code] = pd.DataFrame()
        usable_codes = [code for code, history in histories.items() if not history.empty]
        records, rejected = analyze_universe(
            research_pool[research_pool["code"].isin(usable_codes)], histories, strategies
        )
        db.save_selections(date_key, records)
        db.save_strategy_matches(date_key, records)
        db.save_rejections(date_key, rejected)
        db.finish_task(TASK_NAME, date_key, "success", "筛选结果已保存", len(records))
        logger.info("Daily screen completed for %s: %s records", date_key, len(records))
        return {"status": "success", "trade_date": date_key, "records": len(records), "message": "筛选结果已保存"}
    except Exception as exc:
        logger.exception("Daily screen failed for %s", date_key)
        db.finish_task(TASK_NAME, date_key, "failed", str(exc), 0)
        raise
