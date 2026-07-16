"""Entrypoint: python -m app.main"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import uvicorn

from . import db
from .config import DATA_DIR, MEDIA_DIR, PORT, get_token
from .scheduler import start_scheduler
from .web import app

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("podcastfeeds")


@asynccontextmanager
async def lifespan(_app):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    db.engine()
    _recover_stuck_episodes()
    await _resume_pending()
    scheduler = start_scheduler()
    # Don't log the secret token; confirm it loaded without printing it.
    log.info("management UI on :%d/<token>/ (token loaded, %d chars)",
             PORT, len(get_token()))
    yield
    scheduler.shutdown(wait=False)


def _recover_stuck_episodes() -> None:
    """A restart mid-TTS leaves episodes in 'processing' forever — requeue them."""
    from sqlmodel import select

    from .db import Episode

    with db.session() as s:
        stuck = s.exec(select(Episode).where(Episode.status == "processing")).all()
        for ep in stuck:
            ep.status = "pending"
            s.add(ep)
        if stuck:
            s.commit()
            log.info("requeued %d episodes stuck in processing", len(stuck))


async def _resume_pending() -> None:
    """Process any pending episodes left over from a restart. rss/digest
    sources get re-polled by the scheduler, but inbox items are only kicked
    off at submit time — so an interrupted inbox submit would orphan otherwise."""
    from sqlmodel import select

    from .config import load_config
    from .db import Episode
    from .ingest import process_episode
    from .tasks import spawn

    config = load_config()
    by_slug = {s.slug: s for s in config.sources}
    with db.session() as s:
        pending = s.exec(select(Episode).where(Episode.status == "pending")).all()
        ids = [(ep.id, ep.source_slug) for ep in pending]
    for ep_id, slug in ids:
        source = by_slug.get(slug) or next(
            (s for s in config.sources if s.type == "inbox"), None
        )
        if source:
            spawn(process_episode(ep_id, source))
    if ids:
        log.info("resumed %d pending episodes on startup", len(ids))


app.router.lifespan_context = lifespan


def run() -> None:
    # access_log off: uvicorn's access line logs the full request path, which
    # includes the secret URL token. Our own log_fetches middleware records
    # fetches with the token redacted instead.
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    run()
