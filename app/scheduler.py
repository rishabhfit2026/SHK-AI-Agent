"""Background jobs (IST). Each job opens its own DB session and logs its result."""

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.db import session_scope
from app.services import brief, notify, pipeline, receivables, reorder

log = logging.getLogger(__name__)


def _job(name, fn):
    def run():
        try:
            with session_scope() as db:
                log.info("job %s: %s", name, fn(db))
        except Exception:
            log.exception("job %s failed", name)
    run.__name__ = name
    return run


JOBS = {
    "daily_brief": (lambda db: brief.generate_brief(db).brief_date, dict(hour=8, minute=0)),
    "reorder_scan": (reorder.run_reorder_scan, dict(hour=9, minute=0)),
    "payment_reminders": (receivables.run_payment_reminders, dict(hour=10, minute=0)),
    "pipeline_followups": (pipeline.run_pipeline_followups, dict(hour=9, minute=15)),
    "flush_outbox": (notify.flush_outbox, dict(minute="*")),
}


def start() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone="Asia/Kolkata")
    for name, (fn, cron) in JOBS.items():
        sched.add_job(_job(name, fn), "cron", id=name, max_instances=1, coalesce=True, **cron)
    sched.start()
    return sched


def run_now(name: str):
    fn, _ = JOBS[name]
    with session_scope() as db:
        return fn(db)
