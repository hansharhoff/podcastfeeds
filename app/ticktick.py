"""TickTick intake: tasks on watched TickTick lists are upserted into the
admin approval queue (TickTickItem) for Hans to review and generate/dismiss
by hand — the poller itself never generates episodes or writes to TickTick.

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
from sqlmodel import select

from .config import DATA_DIR, SourceDef, load_config
from .db import Episode, TickTickItem
from .ingest import submit_url
from .tasks import spawn


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
    """Upsert open tasks from the watched lists into the admin approval queue.
    Read-only: never completes tasks, never generates episodes (spec:
    docs/superpowers/specs/2026-07-25-ticktick-phase2-design.md). Returns the
    number of newly queued items."""
    conf = _load()
    if not conf or not conf.get("access_token"):
        return 0
    wanted = conf.get("lists") or [conf.get("list") or "Podcast"]
    wanted_lc = {str(n).lower() for n in wanted}
    headers = {"Authorization": f"Bearer {conf['access_token']}"}
    new_items = 0
    open_ids: set[str] = set()
    all_lists_ok = True
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
            from . import db
            with db.session() as s:
                for project in projects:
                    resp = await client.get(f"{API}/project/{project['id']}/data")
                    if resp.status_code != 200:
                        all_lists_ok = False
                        continue
                    for task in resp.json().get("tasks") or []:
                        if task.get("status"):  # completed in TickTick
                            continue
                        open_ids.add(task["id"])
                        new_items += _upsert(s, project.get("name", ""), task)
                # Only a poll that saw every watched list may conclude a queued
                # item's task is gone — an API blip must not wipe the queue.
                if all_lists_ok:
                    _auto_dismiss(s, open_ids)
    except Exception:
        log.exception("ticktick poll failed")
    return new_items


def _upsert(s, project_name: str, task: dict) -> int:
    """Queue an unseen open task; task_id dedup makes re-polls no-ops.
    Returns 1 if a new item was queued."""
    from sqlmodel import select

    from .db import TickTickItem

    task_id = task["id"]
    if s.exec(select(TickTickItem).where(TickTickItem.task_id == task_id)).first():
        return 0
    title = (task.get("title") or "").strip()
    notes = " ".join(p for p in (task.get("content"), task.get("desc")) if p).strip()
    match = URL_RE.search(f"{title} {notes}")
    url = match.group(0).rstrip(").,]") if match else ""
    item = TickTickItem(
        task_id=task_id, project=project_name, title=title or url or "Untitled",
        notes=notes, url=url, kind=detect_kind(url),
        task_created=_parse_dt(task.get("createdTime") or ""),
    )
    s.add(item)
    s.commit()
    log.info("ticktick: queued [%s] %s (from %s)", item.kind, item.title[:60], project_name)
    return 1


def _auto_dismiss(s, open_ids: set[str]) -> None:
    """Hans completed/deleted a still-queued task in TickTick — mirror that
    here so the queue can be cleaned from either side."""
    from sqlmodel import select

    from .db import TickTickItem

    for item in s.exec(select(TickTickItem).where(TickTickItem.status == "queued")).all():
        if item.task_id not in open_ids:
            item.status = "dismissed"
            s.add(item)
            log.info("ticktick: auto-dismissed %s (task gone from TickTick)", item.title[:60])
    s.commit()


async def generate_item(item_id: int, mode: str = "summary") -> int:
    """Hans clicked Generate: create the episode for a queued item, routed by
    kind (spec §2). The click IS the approval — nothing calls this
    automatically. Returns the episode id."""
    from . import db as _db

    with _db.session() as s:
        item = s.get(TickTickItem, item_id)
        if not item or item.status == "dismissed":
            raise ValueError(f"no queued ticktick item {item_id}")
        kind, url, title, notes, task_id = (
            item.kind, item.url, item.title, item.notes, item.task_id)
    config = load_config()
    inbox = next(src for src in config.sources if src.type == "inbox")
    try:
        if kind == "article":
            ep_id = await submit_url(url, title=title)
        elif kind == "pdf":
            ep_id = _create_episode(inbox.slug, url, title, pdf_url(url))
            source = SourceDef(**{**inbox.__dict__, "allow_pdf": True, "voice": "",
                                  "narrate_mode": "full" if mode == "full" else "summary"})
            from .ingest import process_episode
            spawn(process_episode(ep_id, source))
        else:  # book — brief is generated first, then narrated as source_text
            ep_id = _create_episode(inbox.slug, f"ticktick:{task_id}", title, "")
            spawn(_render_book_brief(ep_id, item_id, inbox, title, notes))
    except Exception as exc:
        # Failures keep the item queued with the error shown inline in the
        # queue row (spec §2/§5) — nothing vanishes silently, and the caller
        # (Task 7's route) still sees the failure via the re-raise.
        log.exception("ticktick: generate_item failed for item %s [%s]", item_id, kind)
        with _db.session() as s:
            item = s.get(TickTickItem, item_id)
            item.status = "queued"
            item.last_error = str(exc)[:300]
            s.add(item)
            s.commit()
        raise
    with _db.session() as s:
        item = s.get(TickTickItem, item_id)
        item.status = "generated"
        item.episode_id = ep_id
        item.last_error = ""
        s.add(item)
        s.commit()
    log.info("ticktick: generated item %s [%s] -> episode %s", item_id, kind, ep_id)
    return ep_id


def _create_episode(slug: str, guid: str, title: str, link: str) -> int:
    """Insert-or-reset an episode row (mirrors submit_url's dedup so a
    re-Generate after an error retries instead of duplicating)."""
    from . import db as _db

    with _db.session() as s:
        existing = s.exec(select(Episode).where(
            Episode.source_slug == slug, Episode.guid == guid)).first()
        if existing:
            existing.status = "pending"
            existing.error = ""
            s.add(existing)
            s.commit()
            return existing.id
        ep = Episode(source_slug=slug, guid=guid, title=title or "Untitled", link=link)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        return ep.id


async def _render_book_brief(ep_id: int, item_id: int, inbox, title: str,
                             notes: str) -> None:
    """LLM book brief (spec §4): generate the spoken text, park it in
    source_text, and let the normal pipeline narrate it (an episode with no
    link falls back to source_text as its body). On failure the queue item
    goes back to 'queued' with the error inline — nothing vanishes silently."""
    from . import db as _db
    from .ingest import process_episode
    from .summarize import llm

    prompt = (
        "You are preparing a short podcast brief about a book someone added to "
        "their reading list. Write 400-600 words of flowing spoken prose (no "
        "headings, no lists, no markdown) covering: what the book is and who "
        "wrote it, its core argument or story, its reception and context, and "
        "why it might be worth reading. Open with a sentence making clear this "
        "is a brief ABOUT the book, not the book itself. If you are not "
        "confident you know this book, say so honestly and keep it short.\n\n"
        f"Book reference: {title}" + (f"\nNotes: {notes}" if notes else "")
    )
    try:
        brief = await llm(prompt)
        with _db.session() as s:
            ep = s.get(Episode, ep_id)
            ep.source_text = brief
            s.add(ep)
            s.commit()
        await process_episode(ep_id, inbox)
    except Exception as exc:
        log.exception("book brief failed for queue item %s", item_id)
        with _db.session() as s:
            ep = s.get(Episode, ep_id)
            ep.status = "error"
            ep.error = f"book brief failed: {exc}"[:300]
            s.add(ep)
            item = s.get(TickTickItem, item_id)
            item.status = "queued"
            item.last_error = str(exc)[:300]
            item.episode_id = None
            s.add(item)
            s.commit()
