"""Background jobs: polling, digests, retention cleanup."""
from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import load_config
from .ingest import build_digest, cleanup_old_episodes, poll_breaking, poll_rss_source

log = logging.getLogger("podcastfeeds")


def start_scheduler() -> AsyncIOScheduler:
    config = load_config()
    scheduler = AsyncIOScheduler(timezone="Europe/Copenhagen")

    for source in config.sources:
        if source.type == "rss":
            scheduler.add_job(
                poll_rss_source, IntervalTrigger(minutes=source.poll_minutes),
                args=[source], id=f"poll:{source.slug}", max_instances=1,
                coalesce=True, misfire_grace_time=600,
            )
        elif source.type == "breaking":
            scheduler.add_job(
                poll_breaking, IntervalTrigger(minutes=source.poll_minutes),
                args=[source], id=f"poll:{source.slug}", max_instances=1,
                coalesce=True, misfire_grace_time=600,
            )
        elif source.type == "digest":
            scheduler.add_job(
                build_digest, CronTrigger.from_crontab(source.schedule, timezone="Europe/Copenhagen"),
                args=[source], id=f"digest:{source.slug}", max_instances=1,
                coalesce=True, misfire_grace_time=3600,
            )

    scheduler.add_job(
        cleanup_old_episodes, CronTrigger.from_crontab("15 4 * * *", timezone="Europe/Copenhagen"),
        args=[config.retention_days], id="cleanup",
    )

    from .ticktick import poll_ticktick

    scheduler.add_job(
        poll_ticktick, IntervalTrigger(minutes=5), id="ticktick",
        max_instances=1, coalesce=True,
    )

    # First pass shortly after boot so a fresh install produces episodes immediately.
    for source in config.sources:
        if source.type in ("rss", "breaking"):
            poll = poll_breaking if source.type == "breaking" else poll_rss_source
            scheduler.add_job(poll, "date", args=[source],
                              id=f"boot:{source.slug}", misfire_grace_time=None)

    scheduler.start()
    log.info("scheduler started with %d sources", len(config.sources))
    return scheduler
