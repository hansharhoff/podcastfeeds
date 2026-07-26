"""TickTick intake: URLs added to a designated TickTick list become inbox
episodes, and the tasks are marked complete.

Requires data/ticktick.json written by scripts/ticktick_auth.py:
  {"access_token": "...", "list": "Podcast"}
No token file -> the poller is a silent no-op.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from .config import DATA_DIR
from .ingest import submit_url


def _parse_dt(value: str) -> datetime | None:
    """Parse a TickTick timestamp ('2026-07-13T14:00:00.000+0000') or an ISO
    watermark ('...+00:00') to an aware datetime."""
    if not value:
        return None
    v = value.replace("Z", "+00:00")
    # Insert a colon in a +HHMM / -HHMM offset so fromisoformat accepts it.
    m = re.search(r"([+-]\d{2})(\d{2})$", v)
    if m:
        v = v[: m.start()] + m.group(1) + ":" + m.group(2)
    try:
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None

log = logging.getLogger("podcastfeeds")

TOKENS_FILE = DATA_DIR / "ticktick.json"
API = "https://api.ticktick.com/open/v1"
URL_RE = re.compile(r"https?://\S+")


def detect_kind(url: str) -> str:
    """Queue-item kind heuristic (spec §1): no URL means a book reference;
    .pdf paths and arxiv links are papers; everything else is an article."""
    if not url:
        return "book"
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.path.lower().endswith(".pdf") or host == "arxiv.org":
        return "pdf"
    return "article"


def pdf_url(url: str) -> str:
    """The direct-download URL for a kind=pdf item: arxiv abstract pages
    map to their PDF; anything else is assumed to already be the PDF."""
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") == "arxiv.org" and "/abs/" in parsed.path:
        return url.replace("/abs/", "/pdf/")
    return url


def _load() -> dict | None:
    if not TOKENS_FILE.exists():
        return None
    try:
        return json.loads(TOKENS_FILE.read_text())
    except Exception:
        log.warning("ticktick.json unreadable")
        return None


async def poll_ticktick() -> int:
    """Returns number of URLs submitted."""
    conf = _load()
    if not conf or not conf.get("access_token"):
        return 0
    # Accept a single "list" or several "lists".
    wanted = conf.get("lists") or [conf.get("list") or "Podcast"]
    wanted_lc = {str(n).lower() for n in wanted}
    headers = {"Authorization": f"Bearer {conf['access_token']}"}
    submitted = 0
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(f"{API}/project")
            if resp.status_code == 401:
                log.warning("ticktick: access token expired/invalid — rerun scripts/ticktick_auth.py")
                return 0
            resp.raise_for_status()
            projects = [p for p in resp.json() if p.get("name", "").lower() in wanted_lc]
            if not projects:
                log.warning("ticktick: none of the lists %r found", wanted)
                return 0
            # Watermark: never process the pre-existing backlog. First poll seeds
            # the watermark to "now"; only tasks created afterwards are queued.
            from . import db
            with db.session() as s:
                wm_iso = db.kv_get(s, "ticktick_watermark")
            if not wm_iso:
                with db.session() as s:
                    db.kv_set(s, "ticktick_watermark",
                              datetime.now(UTC).isoformat())
                log.info("ticktick: seeded watermark; backlog left untouched")
                return 0
            watermark = _parse_dt(wm_iso)
            newest = watermark
            for project in projects:
                data = (await client.get(f"{API}/project/{project['id']}/data")).json()
                for task in data.get("tasks", []) or []:
                    if task.get("status"):  # already completed
                        continue
                    created = _parse_dt(task.get("createdTime") or "")
                    if created is None or created <= watermark:  # backlog / already-seen
                        continue
                    if created > newest:
                        newest = created
                    text = f"{task.get('title', '')} {task.get('content', '')} {task.get('desc', '')}"
                    match = URL_RE.search(text)
                    if not match:
                        continue
                    url = match.group(0).rstrip(").,]")
                    await submit_url(url)
                    await client.post(
                        f"{API}/project/{project['id']}/task/{task['id']}/complete"
                    )
                    submitted += 1
                    log.info("ticktick: queued %s (from %s)", url, project.get("name"))
            if newest > watermark:
                with db.session() as s:
                    db.kv_set(s, "ticktick_watermark", newest.isoformat())
    except Exception:
        log.exception("ticktick poll failed")
    return submitted
