"""FastAPI app: feeds, media, covers, submit API and a small management UI."""
from __future__ import annotations

import hmac
import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from . import db
from .config import BASE_URL, MEDIA_DIR, SourceDef, get_token, load_config
from .covers import cover_path
from .db import Episode
from .feedgen import build_feed
from .ingest import (
    build_digest,
    poll_breaking,
    poll_rss_source,
    process_episode,
    submit_url,
)
from .tasks import spawn

log = logging.getLogger("podcastfeeds")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="podcastfeeds", docs_url=None, redoc_url=None, openapi_url=None)


def _check(token: str) -> None:
    if not hmac.compare_digest(token, get_token()):
        raise HTTPException(status_code=404)


def _base(request: Request) -> str:
    if BASE_URL:
        return BASE_URL
    return str(request.base_url).rstrip("/")


def _source_for(config, ep: Episode) -> SourceDef:
    """The source that owns this episode, falling back to the inbox source."""
    return next(
        (src for src in config.sources if src.slug == ep.source_slug),
        next(src for src in config.sources if src.type == "inbox"),
    )


def _requeue(episode_id: int, digest_error: str | None = None) -> None:
    """Reset an episode to 'pending' and kick off processing with the current
    pipeline. Raises 404 if missing, 400 if it's a digest and digest_error given."""
    config = load_config()
    with db.session() as s:
        ep = s.get(Episode, episode_id)
        if not ep:
            raise HTTPException(status_code=404)
        if digest_error and ep.guid.startswith("digest:"):
            raise HTTPException(status_code=400, detail=digest_error)
        source = _source_for(config, ep)
        ep.status = "pending"
        ep.error = ""
        s.add(ep)
        s.commit()
    spawn(process_episode(episode_id, source))


@app.middleware("http")
async def log_fetches(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if "/feeds/" in path or "/media/" in path:
        # Redact the secret token (first path segment) so it never hits logs.
        redacted = re.sub(r"^/[^/]+/", "/<token>/", path)
        log.info(
            "fetch %s %s ua=%r", response.status_code, redacted,
            request.headers.get("user-agent", "-"),
        )
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/{token}/", response_class=HTMLResponse)
async def index(request: Request, token: str):
    _check(token)
    config = load_config()
    with db.session() as s:
        episodes = s.exec(
            select(Episode).order_by(Episode.created_at.desc()).limit(400)  # type: ignore[union-attr]
        ).all()
    base = _base(request)
    feeds = [
        {"slug": "all", "name": "All feeds (combined)",
         "url": f"{base}/{token}/feeds/all.xml"},
    ] + [
        {"slug": s.slug, "name": s.name, "url": f"{base}/{token}/feeds/{s.slug}.xml"}
        for s in config.sources
    ]
    # Group the way the pipeline groups: by source, dupes/skips visible in place.
    names = {s.slug: s.name for s in config.sources}
    groups: dict[str, dict] = {}
    for ep in episodes:
        g = groups.setdefault(
            ep.source_slug,
            {"name": names.get(ep.source_slug, ep.source_slug),
             "episodes": [], "ready": 0},
        )
        g["episodes"].append(ep)
        if ep.status == "ready":
            g["ready"] += 1

    # "Needs your decision": ready episodes that look off — very short (possible
    # paywall stub / thin content). Surfaced at the top for a keep/redo call.
    decisions = [
        ep for ep in episodes
        if ep.status == "ready" and 0 < (ep.audio_seconds or 0) < 90
        and "accepted-as-is" not in ep.feedback
    ]

    from . import elevenlabs
    from .voices import get_roster

    el = {
        "configured": elevenlabs.configured(),
        "used": elevenlabs.used_this_month(),
        "budget": elevenlabs.CHAR_BUDGET,
        "remaining": (await elevenlabs.budget_remaining()) if elevenlabs.configured() else 0,
        "sources": [
            {"slug": s.slug, "name": s.name, "on": elevenlabs.el_enabled(s.slug)}
            for s in config.sources if s.type in ("rss", "breaking", "inbox")
        ],
    }
    return templates.TemplateResponse(request, "index.html", {
        "groups": groups, "decisions": decisions, "feeds": feeds,
        "token": token, "base": base, "roster": get_roster(),
        "fixed_voices": {s.slug: s.voice for s in config.sources if s.voice},
        "el": el,
    })


@app.post("/{token}/api/eltoggle/{slug}")
async def api_eltoggle(token: str, slug: str):
    """Enable/disable ElevenLabs for a source (admin toggle)."""
    _check(token)
    from . import elevenlabs
    elevenlabs.set_enabled(slug, not elevenlabs.el_enabled(slug))
    log.info("elevenlabs %s for %s", "ON" if elevenlabs.el_enabled(slug) else "OFF", slug)
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.post("/{token}/api/dismiss/{episode_id}")
async def api_dismiss(token: str, episode_id: int):
    """Accept a flagged episode as-is (remove it from the decisions list)."""
    _check(token)
    with db.session() as s:
        ep = s.get(Episode, episode_id)
        if not ep:
            raise HTTPException(status_code=404)
        ep.feedback = f"accepted-as-is\n{ep.feedback}".strip()
        s.add(ep)
        s.commit()
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.get("/{token}/episode/{episode_id}", response_class=HTMLResponse)
async def episode_detail(request: Request, token: str, episode_id: int):
    """Script + provenance + feedback for one episode."""
    _check(token)
    import json as _json

    with db.session() as s:
        ep = s.get(Episode, episode_id)
    if not ep:
        raise HTTPException(status_code=404)
    blocks = []
    if ep.script:
        try:
            blocks = _json.loads(ep.script).get("blocks", [])
        except Exception:
            blocks = [{"voice": ep.voice, "text": ep.script}]
    prov = {}
    if ep.provenance:
        try:
            prov = _json.loads(ep.provenance)
        except Exception:
            prov = {"raw": ep.provenance}
    return templates.TemplateResponse(request, "episode.html", {
        "ep": ep, "blocks": blocks, "prov": prov, "token": token,
    })


@app.post("/{token}/api/feedback/{episode_id}")
async def api_feedback(token: str, episode_id: int, request: Request):
    _check(token)
    form = await request.form()
    verdict = (form.get("verdict") or "").strip()[:20]
    note = (form.get("note") or "").strip()[:500]
    if not verdict and not note:
        raise HTTPException(status_code=400, detail="empty feedback")
    from .db import utcnow

    with db.session() as s:
        ep = s.get(Episode, episode_id)
        if not ep:
            raise HTTPException(status_code=404)
        title = ep.title  # read before commit expires the instance
        line = f"{utcnow().strftime('%Y-%m-%d %H:%M')} {verdict} {note}".strip()
        ep.feedback = f"{line}\n{ep.feedback}".strip()
        s.add(ep)
        s.commit()
    log.info("feedback on episode %s (%s): %s %s", episode_id, title[:40], verdict, note)
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.post("/{token}/api/unskip/{episode_id}")
async def api_unskip(token: str, episode_id: int):
    """Un-skip a skipped episode: queue it for generation with the current
    pipeline (recovers backlog / false-paywall / filtered items on demand)."""
    _check(token)
    _requeue(episode_id, "digests are built on schedule, not per-episode")
    log.info("unskip requested for episode %s", episode_id)
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.post("/{token}/api/redo/{episode_id}")
async def api_redo(token: str, episode_id: int):
    """Regenerate an episode with the current pipeline (explicit user request)."""
    _check(token)
    _requeue(episode_id, "digests can't be redone; the next scheduled build uses the latest pipeline")
    log.info("redo requested for episode %s", episode_id)
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.post("/{token}/api/submit")
async def api_submit(token: str, request: Request):
    """Share a URL. Accepts JSON {url, title?, language?} or form fields."""
    _check(token)
    content_type = request.headers.get("content-type", "")
    if "json" in content_type:
        data = await request.json()
    else:
        form = await request.form()
        data = dict(form)
    url = (data.get("url") or data.get("text") or "").strip()
    # Android share often puts the URL inside free text — pull out the first http(s) link.
    if url and not url.startswith("http"):
        for word in url.split():
            if word.startswith("http"):
                url = word
                break
    if not url.startswith("http"):
        raise HTTPException(status_code=400, detail="no URL found in submission")
    ep_id = await submit_url(
        url, title=(data.get("title") or "").strip(),
        language=(data.get("language") or "auto").strip(),
    )
    if "json" in content_type:
        return {"ok": True, "episode_id": ep_id}
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.post("/{token}/api/poll")
async def api_poll(token: str):
    """Manually trigger a poll of every source (runs in background)."""
    _check(token)
    config = load_config()

    async def run():
        for source in config.sources:
            try:
                if source.type == "rss":
                    await poll_rss_source(source)
                elif source.type == "breaking":
                    await poll_breaking(source)
                elif source.type == "digest":
                    await build_digest(source)
            except Exception:
                log.exception("manual poll failed for %s", source.slug)

    spawn(run())
    return {"ok": True, "message": "poll started"}


@app.post("/{token}/api/retry/{episode_id}")
async def api_retry(token: str, episode_id: int):
    _check(token)
    _requeue(episode_id)
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.get("/{token}/feeds/{slug}.xml")
async def feed_xml(request: Request, token: str, slug: str):
    _check(token)
    config = load_config()
    source = None
    if slug != "all":
        source = next((s for s in config.sources if s.slug == slug), None)
        if source is None:
            raise HTTPException(status_code=404)
    xml = build_feed(config, _base(request), token, source)
    return Response(content=xml, media_type="application/rss+xml")


@app.get("/{token}/media/{filename}")
async def media(token: str, filename: str):
    _check(token)
    path = (MEDIA_DIR / filename).resolve()
    if not path.is_file() or MEDIA_DIR.resolve() not in path.parents:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="audio/mpeg", filename=filename)


@app.get("/{token}/covers/{slug}.png")
async def cover(token: str, slug: str):
    _check(token)
    config = load_config()
    name = "All feeds" if slug == "all" else next(
        (s.name for s in config.sources if s.slug == slug), None
    )
    if name is None:
        raise HTTPException(status_code=404)
    return FileResponse(cover_path(slug, name), media_type="image/png")
