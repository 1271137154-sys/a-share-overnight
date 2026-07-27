"""Idempotent daily screening task shared by the scheduler and administrator button."""
import logging
from datetime import date
from .database import Database
from .data_source import MarketDataSource
from .strategies import run_strategies

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
        histories = {code: source.fetch_history(code) for code in frame["code"].head(200)}
        records = run_strategies(frame[frame["code"].isin(histories)], histories, strategies)
        db.save_selections(date_key, records)
        db.finish_task(TASK_NAME, date_key, "success", "筛选结果已保存", len(records))
        logger.info("Daily screen completed for %s: %s records", date_key, len(records))
        return {"status": "success", "trade_date": date_key, "records": len(records), "message": "筛选结果已保存"}
    except Exception as exc:
        logger.exception("Daily screen failed for %s", date_key)
        db.finish_task(TASK_NAME, date_key, "failed", str(exc), 0)
        raise
