"""APScheduler 调度器。"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import settings
from app.models.db import session_scope
from app.services.audit import run_audit

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    sched = BackgroundScheduler(timezone="Asia/Shanghai")
    sched.add_job(
        _run_audit_safe,
        CronTrigger(
            hour=settings.wiki_audit_cron_hour,
            minute=settings.wiki_audit_cron_minute,
        ),
        id="audit_daily",
        replace_existing=True,
    )
    sched.start()
    _scheduler = sched
    log.info(
        "审校 cron 已注册：每天 %02d:%02d",
        settings.wiki_audit_cron_hour, settings.wiki_audit_cron_minute,
    )
    return sched


def _run_audit_safe() -> None:
    try:
        with session_scope() as s:
            run_audit(s)
    except Exception:  # noqa: BLE001
        log.exception("定时审校失败")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
