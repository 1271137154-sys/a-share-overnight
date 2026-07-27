import logging
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from .config import settings
from .tasks import run_daily_screen

logger = logging.getLogger(__name__)

def create_scheduler(db, source, strategies) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=ZoneInfo("Asia/Shanghai"))
    scheduler.add_job(
        lambda: run_daily_screen(db, source, strategies),
        CronTrigger(day_of_week="mon-fri", hour=settings.scheduler_hour, minute=settings.scheduler_minute, timezone=ZoneInfo("Asia/Shanghai")),
        id="daily_screen", replace_existing=True, coalesce=True, max_instances=1,
    )
    return scheduler

def start_scheduler(db, source, strategies):
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled by ENABLE_SCHEDULER")
        return None
    scheduler = create_scheduler(db, source, strategies)
    scheduler.start()
    logger.info("Scheduler started: weekdays %02d:%02d Asia/Shanghai", settings.scheduler_hour, settings.scheduler_minute)
    return scheduler
