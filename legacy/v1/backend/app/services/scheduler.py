"""Background scheduler — SSL renewal + scheduled backups.

زمان‌بند پس‌زمینه: تمدید SSL و بک‌آپ‌های زمان‌بندی‌شده.
اگر APScheduler نصب نباشد، با هشدار غیرفعال می‌شود (پنل همچنان کار می‌کند).
"""
from __future__ import annotations

import logging

log = logging.getLogger("icsd.scheduler")
_scheduler = None


def start() -> None:
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("APScheduler نصب نیست — زمان‌بندی غیرفعال شد / APScheduler missing; scheduling disabled")
        return

    from app.services import ssl as ssl_service
    from app.services import backup as backup_service

    _scheduler = BackgroundScheduler(timezone="UTC")

    # Daily SSL renewal at 03:30 UTC (acme.sh only renews near-expiry certs)
    def _renew():
        try:
            if ssl_service.acme_installed():
                ssl_service.renew_all(apply=True)
        except Exception as e:  # noqa
            log.error("SSL renew error: %s", e)

    _scheduler.add_job(_renew, CronTrigger(hour=3, minute=30), id="ssl_renew", replace_existing=True)

    # Record metrics history every 5 minutes; prune old data daily
    from app.services import metrics as metrics_service

    def _record():
        try:
            metrics_service.record_history()
        except Exception as e:  # noqa
            log.error("metrics history error: %s", e)

    def _prune():
        try:
            metrics_service.prune_history(keep_days=30)
        except Exception as e:  # noqa
            log.error("metrics prune error: %s", e)

    from apscheduler.triggers.interval import IntervalTrigger
    _scheduler.add_job(_record, IntervalTrigger(minutes=5), id="metrics_record", replace_existing=True)
    _scheduler.add_job(_prune, CronTrigger(hour=4, minute=0), id="metrics_prune", replace_existing=True)

    # Alert checks (SSL expiry / disk full / site down) every hour
    from app.services import notify as notify_service

    def _alerts():
        try:
            notify_service.check_alerts()
        except Exception as e:  # noqa
            log.error("alert check error: %s", e)

    _scheduler.add_job(_alerts, IntervalTrigger(hours=1), id="alert_check", replace_existing=True)

    # Load each enabled backup job by its own cron expression
    reload_backup_jobs(_scheduler, CronTrigger, backup_service)

    _scheduler.start()
    log.info("Scheduler started / زمان‌بند فعال شد")


def reload_backup_jobs(scheduler=None, CronTrigger=None, backup_service=None) -> None:
    """(Re)register backup jobs from the database."""
    scheduler = scheduler or _scheduler
    if scheduler is None:
        return
    if CronTrigger is None:
        from apscheduler.triggers.cron import CronTrigger
    if backup_service is None:
        from app.services import backup as backup_service

    for job in backup_service.list_jobs():
        if not job.get("enabled"):
            continue
        job_id = f"backup_{job['id']}"
        try:
            trigger = CronTrigger.from_crontab(job["schedule_cron"], timezone="UTC")
        except ValueError:
            log.error("cron نامعتبر برای job %s", job["id"])
            continue

        def _make(j):
            def _run():
                try:
                    backup_service.run_job(j, apply=True)
                except Exception as e:  # noqa
                    log.error("backup job %s failed: %s", j.get("id"), e)
            return _run

        scheduler.add_job(_make(job), trigger, id=job_id, replace_existing=True)


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
