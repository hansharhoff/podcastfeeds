"""Paid-access health: in-app probe of every `paid: true` Substack source.

Durable replacement for external session-bound monitoring (the July 2026
expiry went unnoticed for days): on a schedule, fetch each paid source's
newest only_paid post through the real fetch_post path and record whether the
full body came back. The admin page renders the latest snapshot as a banner;
failures also log a WARNING.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from .config import load_config
from .db import utcnow
from .substack import _api_json, fetch_post

log = logging.getLogger("podcastfeeds")

# Latest snapshot, rendered by the admin page. Refreshed at boot and on a
# schedule; empty until the first check completes.
LAST: dict = {"checked_at": "", "results": []}


def newest_paid_slug(archive_items: list | None) -> str | None:
    """First (newest) archive item that is not free — the natural probe target.
    Self-maintaining: no hardcoded slugs to rot as posts age."""
    for item in archive_items or []:
        if (item.get("audience") or "everyone") != "everyone" and item.get("slug"):
            return item["slug"]
    return None


async def check_paid_access() -> dict:
    """Probe each paid substack source's newest paid post; update LAST."""
    results: list[dict] = []
    for source in load_config().sources:
        if not source.paid or source.type != "rss":
            continue
        feed_host = urlparse(source.url).netloc
        if not feed_host.endswith(".substack.com"):
            continue
        sub = feed_host[: -len(".substack.com")]
        archive = await _api_json(
            f"https://{sub}.substack.com/api/v1/archive?sort=new&limit=20")
        slug = newest_paid_slug(archive if isinstance(archive, list) else None)
        if not slug:
            log.warning("paid-access check: no paid post found in %s archive", sub)
            continue
        post = await fetch_post(sub, slug)
        ok = bool(post and post["accessible"])
        results.append({
            "slug": source.slug, "name": source.name, "post": slug, "ok": ok,
            "delivered": post["delivered_words"] if post else 0,
            "wordcount": post["wordcount"] if post else 0,
        })
        if not ok:
            log.warning(
                "PAID ACCESS BROKEN for %s (%s/%s): delivered %d/%d words — "
                "substack.sid in config/secrets.yaml likely expired",
                source.name, sub, slug,
                post["delivered_words"] if post else 0,
                post["wordcount"] if post else 0)
    LAST["checked_at"] = utcnow().strftime("%Y-%m-%d %H:%M UTC")
    LAST["results"] = results
    return LAST
