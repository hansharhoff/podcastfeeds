"""Source polling and the episode processing pipeline."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import feedparser
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from . import db
from .config import MEDIA_DIR, PIPELINE_VERSION, SourceDef, load_config, pick_voice
from .db import Episode, utcnow
from .extract import (
    detect_language,
    extract_article,
    extract_og_image,
    extract_segments,
    fetch_html,
    fetch_image_jpeg,
    image_area,
    is_paywalled,
    mark_dialogue,
    segments_from_clean_html,
    strip_html,
)
from .substack import fetch_post, substack_ref
from .summarize import (
    article_summary,
    digest_script,
    is_cruft_line,
    scrub_light,
    spoken_date,
    vision_analyze,
)
from .tasks import spawn
from .tts import synthesize, synthesize_blocks
from .voices import assign_voice

log = logging.getLogger("podcastfeeds")

# One TTS job at a time keeps CPU + edge-tts usage polite.
_tts_lock = asyncio.Lock()


def _entry_guid(entry) -> str:
    return entry.get("id") or entry.get("link") or entry.get("title", "")


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def _is_duplicate_title(s, source_slug: str, title: str) -> bool:
    """Same story arriving via several feeds (blog + HN) under slightly
    different titles: substring match on normalized titles."""
    norm = _norm_title(title)
    if len(norm) < 6:
        return False
    recent = s.exec(
        select(Episode).where(Episode.source_slug == source_slug)
        .order_by(Episode.created_at.desc()).limit(30)  # type: ignore[union-attr]
    ).all()
    for other in recent:
        other_norm = _norm_title(other.title)
        if len(other_norm) >= 6 and (norm in other_norm or other_norm in norm):
            return True
    return False


def _entry_audio(entry) -> tuple[str, int]:
    for enc in entry.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("audio"):
            try:
                length = int(enc.get("length") or 0)
            except (TypeError, ValueError):
                length = 0
            return enc.get("href", ""), length
    return "", 0


def _substack_fetch_url(source: SourceDef, link: str) -> str:
    """Substack mirrors every post at {subdomain}.substack.com/p/{slug}. When a
    source's feed is a substack subdomain but its post links use a custom domain
    (slowboring.com etc.), fetch via the substack subdomain so the substack.com
    session cookie applies — otherwise paid posts come back paywalled."""
    feed_host = urlparse(source.url).netloc
    if not feed_host.endswith(".substack.com"):
        return link
    link_host = urlparse(link).netloc
    path = urlparse(link).path
    if link_host != feed_host and "/p/" in path:
        return f"https://{feed_host}{path}"
    return link


def _entry_text(entry) -> str:
    for content in entry.get("content", []) or []:
        if content.get("value"):
            return strip_html(content["value"])
    return strip_html(entry.get("summary", ""))


def _parse_feed_sync(url: str):
    # feedparser uses urllib with no timeout; guard so a hung feed host can't
    # tie up the worker thread (and stall that source's polling) indefinitely.
    import socket
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(30)
    try:
        return feedparser.parse(url)
    finally:
        socket.setdefaulttimeout(old)


async def _parse_feed(url: str):
    parsed = await asyncio.to_thread(_parse_feed_sync, url)
    # Transient upstream failures (hnrss 502s): one retry before giving up.
    if not parsed.entries and (parsed.bozo or getattr(parsed, "status", 200) >= 500):
        await asyncio.sleep(10)
        parsed = await asyncio.to_thread(_parse_feed_sync, url)
    return parsed


async def poll_rss_source(source: SourceDef) -> int:
    """Discover new entries in an rss source (possibly several feeds);
    returns number of new episodes."""
    from .summarize import matches_criteria

    feeds = await asyncio.gather(
        *(_parse_feed(u) for u in source.feed_urls()), return_exceptions=True
    )
    entries = []
    for parsed in feeds:
        if isinstance(parsed, Exception) or not getattr(parsed, "entries", None):
            log.warning("feed poll %s: a feed failed: %s", source.slug, parsed)
            continue
        # Take enough for both generation and the "keep latest N available" set.
        entries.extend(parsed.entries[: max(source.max_items_per_poll * 4, source.keep_available + 2)])
    entries.sort(
        key=lambda e: e.get("published_parsed") or e.get("updated_parsed") or (0,),
        reverse=True,
    )
    recent = list(entries)  # newest-first snapshot, before watermark filtering

    # Watermark: only generate posts published AFTER subscription — never
    # backfill the archive. First poll seeds the watermark from the newest
    # entry and generates just that one; later polls process only entries
    # newer than the watermark, advancing it as they go.
    def _entry_dt(entry):
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        return datetime(*t[:6], tzinfo=UTC) if t else None

    with db.session() as s:
        wm_iso = db.kv_get(s, f"feed_watermark:{source.slug}")
    watermark = datetime.fromisoformat(wm_iso) if wm_iso else None

    if watermark is None:
        newest_dt = next((_entry_dt(e) for e in entries if _entry_dt(e)), None)
        seed = newest_dt or utcnow()
        with db.session() as s:
            db.kv_set(s, f"feed_watermark:{source.slug}", seed.isoformat())
        # Treat the seed as the floor so the end-of-poll advance can't regress the
        # watermark below it (the seed episode may be an older filter-passing post
        # than the newest entry — otherwise everything between would backfill).
        watermark = seed
        # First subscribe: generate only the latest post that passes title_filter
        # (so the seed episode is a real ACX article, not an open thread; a real
        # HA release, not a random blog post) — never the whole archive.
        if source.title_filter:
            first = next(
                (e for e in entries if re.search(source.title_filter, e.get("title", ""))),
                None,
            )
            entries = [first] if first else []
        else:
            entries = entries[:1]
    else:
        # Keep entries newer than the watermark. Dateless entries are kept and
        # fall back to guid dedup (so a dateless feed isn't silenced forever).
        entries = [e for e in entries if _entry_dt(e) is None or _entry_dt(e) > watermark]
        # Process OLDEST-first: if a burst exceeds max_items_per_poll, the newest
        # unprocessed entries stay above the (per-entry advancing) watermark and
        # are picked up on the next poll — nothing is skipped.
        entries.sort(key=lambda e: _entry_dt(e) or watermark)

    new = 0
    max_dt = watermark
    for entry in entries:
        guid = _entry_guid(entry)
        if not guid:
            continue
        if source.title_filter and not re.search(
            source.title_filter, entry.get("title", "")
        ):
            continue
        with db.session() as s:
            exists = s.exec(
                select(Episode).where(
                    Episode.source_slug == source.slug, Episode.guid == guid
                )
            ).first()
        if exists:
            continue
        if new >= source.max_items_per_poll:
            break

        title = entry.get("title", "Untitled")
        summary = entry.get("summary", "")
        with db.session() as s:
            if _is_duplicate_title(s, source.slug, title):
                s.add(Episode(
                    source_slug=source.slug, guid=guid, title=title,
                    link=entry.get("link", ""), status="skipped",
                    description="duplicate of an existing episode (title match)",
                ))
                s.commit()
                continue
        if source.llm_filter:
            verdict = await matches_criteria(title, summary, source.llm_filter)
            if verdict is None:
                continue  # LLM down: leave unrecorded, re-check next poll
            if not verdict:
                with db.session() as s:  # record the rejection, don't re-classify
                    s.add(Episode(
                        source_slug=source.slug, guid=guid, title=title,
                        link=entry.get("link", ""), status="skipped",
                        description="filtered out by llm_filter",
                    ))
                    s.commit()
                continue

        audio_url, audio_bytes = ("", 0)
        if source.prefer_existing_audio:
            audio_url, audio_bytes = _entry_audio(entry)
        ep = Episode(
            source_slug=source.slug,
            guid=guid,
            title=title,
            description=strip_html(summary)[:1000],
            source_text=_entry_text(entry)[:60000],
            link=entry.get("link", ""),
            audio_url=audio_url,
            audio_bytes=audio_bytes,
        )
        if audio_url:  # passthrough: original audio, nothing to generate
            ep.status = "ready"
            ep.published_at = utcnow()
        try:
            with db.session() as s:
                s.add(ep)
                s.commit()
        except IntegrityError:
            # Lost a race with an overlapping poll — the row already exists.
            continue
        edt = _entry_dt(entry)
        if edt and (max_dt is None or edt > max_dt):
            max_dt = edt
        new += 1

    # Advance the watermark past everything generated this poll.
    if max_dt and (watermark is None or max_dt > watermark):
        with db.session() as s:
            db.kv_set(s, f"feed_watermark:{source.slug}", max_dt.isoformat())

    # Keep the latest N posts browsable/unskippable: record any not already an
    # episode as a 'skipped' row. (llm_filter sources excluded — their feeds are
    # firehoses where most entries aren't wanted.)
    if source.keep_available and not source.llm_filter:
        _record_available(source, recent, source.keep_available)

    await process_pending(source)
    return new


def _record_available(source: SourceDef, recent: list, keep: int) -> int:
    """Record the newest `keep` filter-passing feed entries as skipped rows (if
    not already episodes) so they show in the admin panel with an unskip button."""
    made = 0
    for entry in recent[:keep]:
        guid = _entry_guid(entry)
        if not guid:
            continue
        if source.title_filter and not re.search(source.title_filter, entry.get("title", "")):
            continue
        with db.session() as s:
            if s.exec(
                select(Episode).where(
                    Episode.source_slug == source.slug, Episode.guid == guid
                )
            ).first():
                continue
            try:
                s.add(Episode(
                    source_slug=source.slug, guid=guid,
                    title=entry.get("title", "Untitled"),
                    description="available — unskip to generate",
                    source_text=_entry_text(entry)[:60000],
                    link=entry.get("link", ""), status="skipped",
                ))
                s.commit()
                made += 1
            except IntegrityError:
                s.rollback()
    return made


_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)


def _collect_breaking(data) -> list[dict]:
    """Walk DR's __NEXT_DATA__ tree, returning breaking articles as
    {title, summary, urlPathId}. An article is breaking if ANY of its
    publications has breaking == true. Deduped by urlPathId."""
    found: list[dict] = []
    seen: set[str] = set()

    def walk(o) -> None:
        if isinstance(o, dict):
            pubs = o.get("publications")
            path = o.get("urlPathId")
            if o.get("title") and isinstance(pubs, list) and path:
                breaking = any(
                    p.get("breaking") for p in pubs if isinstance(p, dict)
                )
                if (breaking and isinstance(path, str)
                        and path.startswith("/nyheder") and path not in seen):
                    seen.add(path)
                    found.append({
                        "title": o["title"],
                        "summary": o.get("summary") or "",
                        "urlPathId": path,
                    })
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(data)
    return found


async def poll_breaking(source: SourceDef) -> int:
    """DR breaking news: create an episode only for articles DR itself flags
    as breaking (the `breaking` field in the front page's embedded JSON).
    Returns the number of new episodes."""
    try:
        html_text = await fetch_html(source.url)
    except Exception as exc:
        log.warning("breaking poll %s: fetch failed: %s", source.slug, exc)
        return 0

    m = _NEXT_DATA_RE.search(html_text)
    if not m:
        log.warning("breaking poll %s: __NEXT_DATA__ script tag not found", source.slug)
        return 0
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        log.warning("breaking poll %s: __NEXT_DATA__ JSON parse failed: %s",
                    source.slug, exc)
        return 0

    articles = _collect_breaking(data)
    if not articles:
        return 0

    new = 0
    for article in articles:
        if new >= source.max_items_per_poll:
            break
        guid = article["urlPathId"]
        link = "https://www.dr.dk" + guid
        with db.session() as s:
            exists = s.exec(
                select(Episode).where(
                    Episode.source_slug == source.slug, Episode.guid == guid
                )
            ).first()
        if exists:
            continue
        try:
            with db.session() as s:
                s.add(Episode(
                    source_slug=source.slug,
                    guid=guid,
                    title=article["title"],
                    description=article["summary"],
                    link=link,
                ))
                s.commit()
        except IntegrityError:
            # Lost a race with an overlapping poll — the row already exists.
            continue
        new += 1

    if new:
        log.info("breaking poll %s: %d new breaking article(s)", source.slug, new)
    await process_pending(source)
    return new


async def process_pending(source: SourceDef) -> None:
    with db.session() as s:
        pending = s.exec(
            select(Episode).where(
                Episode.source_slug == source.slug, Episode.status == "pending"
            ).order_by(Episode.created_at)
        ).all()
        ids = [e.id for e in pending]
    for ep_id in ids:
        await process_episode(ep_id, source)


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attr(url: str) -> str:
    """Escape a URL for use inside a double-quoted HTML attribute (show notes
    are rendered by podcast-app webviews; a hostile source URL must not break
    out of the attribute)."""
    return (url.replace("&", "&amp;").replace('"', "&quot;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _source_label(source: SourceDef, link: str) -> str:
    """Human label for titles/show-notes. For the inbox, the article's domain."""
    if source.type == "inbox" and link:
        return urlparse(link).netloc.removeprefix("www.") or source.name
    return source.name


def _shownotes_header(label: str, link: str) -> str:
    line = f"<p><strong>Source: {_html_escape(label)}</strong>"
    if link:
        line += f' — <a href="{_attr(link)}">original</a>'
    return line + "</p>"


def _interleaved_shownotes(label: str, segments: list[dict], link: str,
                           max_chars: int = 24000) -> str:
    """Full article as HTML with text, quotes and images in reading order, so
    the listener can scroll along. Image descriptions become captions."""
    parts = [_shownotes_header(label, link)]
    used = 0
    for seg in segments:
        if used >= max_chars:
            parts.append("<p>…</p>")
            break
        if seg["type"] in ("text", "dialogue") and is_cruft_line(seg["text"]):
            continue
        if seg["type"] == "text":
            parts.append(f"<p>{_html_escape(seg['text'])}</p>")
            used += len(seg["text"])
        elif seg["type"] == "dialogue":
            parts.append(
                f"<p><strong>{_html_escape(seg['speaker'])}:</strong> "
                f"{_html_escape(seg['text'])}</p>"
            )
            used += len(seg["text"])
        elif seg["type"] == "heading":
            parts.append(f"<h3>{_html_escape(seg['text'])}</h3>")
            used += len(seg["text"])
        elif seg["type"] == "quote":
            parts.append(f"<blockquote>{_html_escape(seg['text'])}</blockquote>")
            used += len(seg["text"])
        elif seg["type"] == "image":
            cap = seg.get("caption") or seg.get("description") or ""
            figcap = f"<figcaption>{_html_escape(cap)}</figcaption>" if cap else ""
            parts.append(f'<figure><img src="{_attr(seg["src"])}" loading="lazy"/>{figcap}</figure>')
    return "\n".join(parts)


def _episode_intro(title: str, source_name: str, language: str,
                   preview: bool = False) -> str:
    date_str = spoken_date(utcnow(), language)
    if language == "da":
        base = f"{title}. Fra {source_name}, {date_str}."
        if preview:
            base += " Bemærk: dette er kun et gratis uddrag af et betalt indlæg."
        return base
    base = f"{title}. From {source_name}, {date_str}."
    if preview:
        base += " Note: this is only the free preview of a paid post."
    return base


def _preview_outro(language: str) -> str:
    return ("Det var uddraget. Resten af dette indlæg kræver et betalt abonnement; "
            "linket findes i shownotes." if language == "da"
            else "That was the free preview. The rest of this post requires a paid "
                 "subscription; the link is in the show notes.")


def _episode_outro(language: str, has_images: bool) -> str:
    if language == "da":
        return ("Det var alt for denne artikel. Links og billeder findes i shownotes."
                if has_images else "Det var alt. Linket findes i shownotes.")
    return ("That's it for this article. Links and images are in the show notes."
            if has_images else "That's it. The original link is in the show notes.")


def _source_cover(source: SourceDef) -> bytes | None:
    from .covers import cover_path

    try:
        return cover_path(source.slug, source.name).read_bytes()
    except Exception:
        return None


def _image_marker(caption: str, description: str, n: int, language: str) -> str:
    detail = description or caption
    if language == "da":
        return f"Her er et billede. {detail}" if detail else \
            f"Artiklen har et billede her, nummer {n}. Se episodens shownotes."
    return f"There is an image here. {detail}" if detail else \
        f"The article includes an image here, number {n}. See the show notes."


def _conversation_blocks(analysis: dict, main_voice: str, language: str,
                         speaker_voice) -> list[dict]:
    """Narrate a screenshotted thread/chat: announcer intro per message in the
    main voice, message text in the (persistent) speaker voice."""
    intro = ("Her er et skærmbillede af en samtale. " if language == "da"
             else "There is a screenshot of a conversation here. ")
    desc = analysis.get("description", "")
    blocks = [{"voice": main_voice, "text": f"{intro}{desc}", "chapter": None}]
    for msg in analysis.get("messages", [])[:10]:
        speaker = str(msg.get("speaker", "")).strip() or ("Ukendt" if language == "da" else "Unknown")
        text = str(msg.get("text", "")).strip()[:600]
        if not text:
            continue
        says = f"{speaker} skriver:" if language == "da" else f"{speaker} writes:"
        blocks.append({"voice": main_voice, "text": says, "chapter": None})
        blocks.append({"voice": speaker_voice(speaker), "text": text, "chapter": None})
    return blocks


def _build_blocks(title: str, intro: str, segments: list[dict], main_voice: str,
                  quote_voice: str, describer_voice: str, language: str, max_chars: int,
                  images_meta: dict[str, dict], speaker_voice,
                  source_label: str = "") -> tuple[list[dict], list[dict]]:
    """Turn ordered segments into TTS blocks (voice switches for quotes and
    screenshot conversations, chapter marks at images). Returns (blocks, images_used)."""
    blocks: list[dict] = [{
        "voice": main_voice, "text": intro,
        "chapter": {"title": title, "image": None},
    }]
    images: list[dict] = []
    pending: list[str] = []
    used = len(title)

    def flush() -> None:
        nonlocal pending
        if pending:
            blocks.append({"voice": main_voice, "text": "\n".join(pending), "chapter": None})
            pending = []

    for seg in segments:
        if used >= max_chars:
            break
        if seg["type"] in ("text", "dialogue") and is_cruft_line(seg["text"]):
            continue
        if seg["type"] == "text":
            pending.append(scrub_light(seg["text"]))
            used += len(seg["text"])
        elif seg["type"] == "dialogue":
            # A transcribed interview turn: read in the speaker's own voice.
            # The article's author/source keeps the main voice.
            flush()
            spk = seg["speaker"]
            is_source = source_label and spk.lower() in source_label.lower()
            voice = main_voice if is_source else speaker_voice(spk)
            blocks.append({"voice": voice, "text": scrub_light(seg["text"]),
                           "chapter": None})
            used += len(seg["text"])
        elif seg["type"] == "heading":
            # A section heading starts a chapter and is spoken as a lead-in.
            flush()
            head = scrub_light(seg["text"])[:120]
            blocks.append({
                "voice": main_voice, "text": head,
                "chapter": {"title": head[:80], "image": None},
            })
            used += len(seg["text"])
        elif seg["type"] == "quote":
            flush()
            blocks.append({"voice": quote_voice, "text": scrub_light(seg["text"]), "chapter": None})
            used += len(seg["text"])
        elif seg["type"] == "image" and len(images) < 8:
            flush()
            n = len(images) + 1
            caption = seg["caption"][:200]
            meta = images_meta.get(seg["src"], {})
            analysis = meta.get("analysis") or {}
            seg["description"] = analysis.get("description", "")
            images.append(seg)
            chapter = {
                "title": caption or seg["description"][:80] or f"Image {n}",
                "image": meta.get("jpeg"),
            }
            if analysis.get("kind") == "conversation":
                convo = _conversation_blocks(analysis, main_voice, language, speaker_voice)
                convo[0]["chapter"] = chapter
                blocks.extend(convo)
                used += sum(len(b["text"]) for b in convo)
            elif analysis.get("kind") == "text" and analysis.get("text"):
                # A text screenshot: read its actual words, not just a description.
                shot_text = analysis["text"].strip()[:2000]
                marker = ("Her er et skærmbillede med tekst. " if language == "da"
                          else "There is a text screenshot here. ")
                seg["description"] = shot_text  # show notes carry the text too
                blocks.append({
                    "voice": describer_voice,
                    "text": f"{marker}{shot_text}",
                    "chapter": chapter,
                })
                used += len(shot_text)
            else:
                blocks.append({
                    "voice": describer_voice,
                    "text": _image_marker(caption, seg["description"], n, language),
                    "chapter": chapter,
                })
                used += len(seg["description"])
    flush()
    return blocks, images


async def process_episode(ep_id: int, source: SourceDef) -> None:
    with db.session() as s:
        ep = s.get(Episode, ep_id)
        if not ep or ep.status not in ("pending", "error"):
            return
        ep.status = "processing"
        ep.error = ""
        s.add(ep)
        s.commit()
        title, link, description = ep.title, ep.link, ep.description
        source_text = ep.source_text

    try:
        # PDFs (e.g. a research-proof link) don't narrate into a useful episode.
        if link and urlparse(link).path.lower().endswith(".pdf"):
            log.info("skipping PDF link (not narratable): %s", link)
            with db.session() as s:
                ep = s.get(Episode, ep_id)
                ep.status = "skipped"
                ep.error = "PDF source — not narratable"
                ep.provenance = json.dumps({
                    "pipeline_version": PIPELINE_VERSION, "skipped": "pdf", "link": link,
                })
                s.add(ep)
                s.commit()
            return

        body, segments, og_image = "", [], ""
        html_text = ""
        paywalled = False
        sref = substack_ref(source, link) if link else None
        if sref:
            # Substack: use the post API (cookie-safe, definitive audience).
            post = await fetch_post(*sref)
            if post and post["accessible"] and post["body_html"]:
                html_text = post["body_html"]
                # Substack body_html is clean article HTML — a direct DOM parse
                # keeps images and section headings that trafilatura drops here.
                _, segments = segments_from_clean_html(html_text)
                _, body = extract_article(html_text, link)
                og_image = post["cover_image"]
                title = post["title"] or title
            elif post and not post["accessible"]:
                paywalled = True
                # Keep the preview content (Substack returns the free excerpt as
                # body_html) so a substantial preview can still become an episode.
                if post["body_html"]:
                    html_text = post["body_html"]
                    _, segments = segments_from_clean_html(html_text)
                    _, body = extract_article(html_text, link)
                    og_image = post["cover_image"]
                    title = post["title"] or title
                log.warning("PAYWALL (substack %s, audience=%s) for %s",
                            sref[0], post["audience"], link)
            # post is None -> fall through to the generic HTML fetch below
        if not sref or (not html_text and not paywalled):
            if link:
                fetch_url = _substack_fetch_url(source, link)
                try:
                    html_text = await fetch_html(fetch_url)
                    extracted_title, segments = extract_segments(html_text, fetch_url)
                    _, body = extract_article(html_text, fetch_url)
                    og_image = extract_og_image(html_text, fetch_url)
                    if not title or title == "Untitled":
                        title = extracted_title or title
                except Exception as exc:
                    log.warning("fetch/extract failed for %s: %s", fetch_url, exc)
            paywalled = bool(link) and is_paywalled(body, html_text)

        # Paywalled: a substantial free preview still becomes an episode, clearly
        # bracketed as a preview (start + end). Only a thin stub is skipped.
        is_preview = False
        if paywalled:
            if len(body) >= 600:
                is_preview = True
            else:
                with db.session() as s:
                    ep = s.get(Episode, ep_id)
                    ep.status = "skipped"
                    ep.error = "paywalled — preview too short to narrate"
                    ep.provenance = json.dumps({
                        "pipeline_version": PIPELINE_VERSION, "skipped": "paywall",
                        "link": link, "body_chars": len(body),
                    })
                    s.add(ep)
                    s.commit()
                return
        if len(body) < 200:
            # Page unfetchable/paywalled: best remaining source is the feed
            # entry itself. NEVER the description — a previous bad run may have
            # overwritten it with generated show notes (episode 32 incident).
            from .summarize import looks_meta

            candidates = [c for c in (source_text, description)
                          if len(c) > len(body) and not looks_meta(c)]
            if candidates:
                body = max(candidates, key=len)
        if len(body) < 40:
            # Fetch succeeded but there's no article text — a discussion/open
            # thread or a link-only post. Skip cleanly (not a retryable error).
            if html_text or source_text or description:
                log.info("no article content for %s — skipping (thread/link post)", link)
                with db.session() as s:
                    ep = s.get(Episode, ep_id)
                    ep.status = "skipped"
                    ep.error = "no article content (discussion/thread or link-only post)"
                    ep.provenance = json.dumps({
                        "pipeline_version": PIPELINE_VERSION, "skipped": "no-content",
                        "link": link,
                    })
                    s.add(ep)
                    s.commit()
                return
            raise RuntimeError("could not extract meaningful article text")

        body = body[: source.max_chars]
        language = detect_language(f"{title}\n{body}")
        # Voice roster key: the source is the "blogger" for rss feeds; for the
        # inbox each site/domain is its own blogger and keeps its own voice.
        roster_key = source.slug
        if source.type == "inbox" and link:
            roster_key = urlparse(link).netloc.removeprefix("www.")
        voice = pick_voice(source, language, roster_key)
        # ElevenLabs main voice: only if the source is toggled on AND the whole
        # episode fits the remaining monthly budget (else stay on edge-tts).
        from . import elevenlabs
        if elevenlabs.el_enabled(source.slug):
            est = len(body) + 600  # body + intro/outro/markers
            remaining = await elevenlabs.budget_remaining()
            if elevenlabs.configured() and est <= remaining:
                voice = f"eleven:{source.el_voice or elevenlabs.DEFAULT_VOICE}"
                log.info("elevenlabs voice for [%s] (~%d chars, %d remaining)",
                         source.slug, est, remaining)
            else:
                log.info("elevenlabs enabled for [%s] but budget short (need ~%d, have %d) — edge-tts",
                         source.slug, est, remaining)

        # Interview transcripts (speaker labels at paragraph start) become
        # per-speaker dialogue segments so each voice is read differently.
        segments = mark_dialogue(segments)
        seg_chars = sum(len(s_.get("text", "")) for s_ in segments)
        images: list[dict] = []
        source_label = _source_label(source, link)
        intro = _episode_intro(title, source.name, language, preview=is_preview)
        # Outro: previews get an explicit "that was the free preview" sign-off.
        def outro(has_images: bool) -> str:
            return _preview_outro(language) if is_preview else _episode_outro(language, has_images)
        cover = _source_cover(source)
        episode_image = og_image
        if episode_image:
            art = await fetch_image_jpeg(episode_image, max_px=1400)
            cover = art or cover
        prov: dict = {
            "pipeline_version": PIPELINE_VERSION, "source": source.slug,
            "link": link, "language": language,
            "generated_at": utcnow().isoformat(),
        }
        spoken_blocks: list[dict] = []

        if source.narrate_mode == "summary" and len(body) < 400:
            # Too little content to summarize without hallucination: read the
            # source text verbatim and point at the show notes.
            script = f"{intro}\n\n{scrub_light(body)}\n\n{outro(False)}"
            show_notes = _shownotes_header(source_label, link) + \
                f"<p>{_html_escape(body[:800])}</p>"
            spoken_blocks = [{"voice": voice, "text": script}]
            prov.update({"path": "verbatim-short", "voices": {"main": voice},
                         "generator": "extract", "scrub": "light",
                         "note": f"body only {len(body)} chars; LLM summary skipped"})
            async with _tts_lock:
                filename, size, seconds = await synthesize(
                    script, voice=voice, title=title, album=source.name,
                    artist=source.name, date=str(utcnow().year), cover=cover,
                )
        elif source.narrate_mode == "summary":
            script, notes, gen_prov = await article_summary(title, body, language, link)
            script = f"{intro}\n\n{script}\n\n{outro(False)}"
            show_notes = _shownotes_header(source_label, link) + \
                "".join(f"<p>{_html_escape(ln)}</p>" for ln in notes.split("\n") if ln.strip())
            spoken_blocks = [{"voice": voice, "text": script}]
            prov.update({"path": "summary", "voices": {"main": voice}, **gen_prov})
            async with _tts_lock:
                filename, size, seconds = await synthesize(
                    script, voice=voice, title=title, album=source.name,
                    artist=source.name, date=str(utcnow().year), cover=cover,
                )
        elif segments and seg_chars >= 200:
            # Structured narration: quotes in a second voice, images described
            # (screenshot conversations acted out per speaker) and embedded as
            # chapter art.
            quote_voice = pick_voice(
                SourceDef(**{**source.__dict__, "voice": ""}),
                language, f"{roster_key}#quotes",
            )
            aliases = load_config().speaker_aliases

            def speaker_voice(name: str) -> str:
                key = aliases.get(name.lower().lstrip("@")) or \
                    f"speaker:{name.lower().lstrip('@').replace(' ', '-')[:40]}"
                return assign_voice(key, language)

            image_srcs = [s_["src"] for s_ in segments if s_["type"] == "image"][:8]
            jpeg_list = await asyncio.gather(*(fetch_image_jpeg(u) for u in image_srcs))
            images_meta: dict[str, dict] = {}
            for src, jpeg in zip(image_srcs, jpeg_list, strict=True):
                analysis = await vision_analyze(jpeg, language) if jpeg else None
                images_meta[src] = {"jpeg": jpeg, "analysis": analysis}
            # Drop undownloadable/tiny images (avatars, icons) from narration.
            segments = [
                s_ for s_ in segments
                if s_["type"] != "image" or images_meta.get(s_["src"], {}).get("jpeg")
            ]

            describer_voice = pick_voice(
                SourceDef(**{**source.__dict__, "voice": ""}),
                language, f"{roster_key}#images",
            )
            blocks, images = _build_blocks(
                title, intro, segments, voice, quote_voice, describer_voice,
                language, source.max_chars, images_meta, speaker_voice,
                source_label=source_label,
            )
            if source.danish_perspective:
                try:
                    from .summarize import danish_perspective

                    dk_text, dk_prov = await danish_perspective(title, body, language)
                    dk_voice = assign_voice(f"danish-perspective:{language}", language)
                    blocks.append({
                        "voice": dk_voice, "text": dk_text,
                        "chapter": {"title": "Set fra Danmark" if language == "da"
                                    else "The view from Denmark", "image": None},
                    })
                    prov.update(dk_prov)
                    prov["voices_dk"] = dk_voice
                except Exception as exc:
                    log.warning("danish perspective skipped for %s: %s", title[:40], exc)
                    prov["dk_error"] = str(exc)[:200]
            blocks.append({
                "voice": voice, "text": outro(bool(images)),
                "chapter": None,
            })
            spoken_blocks = [
                {"voice": b["voice"], "text": b["text"],
                 **({"chapter": b["chapter"]["title"]} if b.get("chapter") else {})}
                for b in blocks
            ]
            prov.update({
                "path": "structured",
                "voices": {"main": voice, "quote": quote_voice,
                           "images": describer_voice},
                "images": len(images),
                "conversations": sum(
                    1 for m in images_meta.values()
                    if (m.get("analysis") or {}).get("kind") == "conversation"
                ),
                "generator": "extract+vision", "scrub": "light",
            })

            # Episode artwork: the page's lead image (og/cover, fetched above),
            # else the MOST PROMINENT body image — the largest by pixel area,
            # re-fetched at cover resolution.
            if not episode_image and images:
                prominent = max(
                    (im["src"] for im in images),
                    key=lambda s_: image_area(images_meta.get(s_, {}).get("jpeg") or b""),
                    default="",
                )
                if prominent:
                    episode_image = prominent
                    art = await fetch_image_jpeg(prominent, max_px=1400)
                    cover = art or images_meta.get(prominent, {}).get("jpeg") or cover

            async with _tts_lock:
                filename, size, seconds = await synthesize_blocks(
                    blocks, title=title, album=source.name,
                    artist=source.name, date=str(utcnow().year), cover=cover,
                )
            show_notes = _interleaved_shownotes(source_label, segments, link)
        else:
            script = f"{intro}\n\n{scrub_light(body)}\n\n{outro(False)}"
            show_notes = _shownotes_header(source_label, link) + \
                "".join(f"<p>{_html_escape(p)}</p>" for p in body.split("\n") if p.strip())
            spoken_blocks = [{"voice": voice, "text": script}]
            prov.update({"path": "plain", "voices": {"main": voice},
                         "generator": "extract", "scrub": "light"})
            async with _tts_lock:
                filename, size, seconds = await synthesize(
                    script, voice=voice, title=title, album=source.name,
                    artist=source.name, date=str(utcnow().year), cover=cover,
                )

        if is_preview:
            prov["preview"] = True
            banner = ("<p><strong>⚠ Preview only — this is the free excerpt of a "
                      "paid post; the full article requires a subscription.</strong></p>")
            show_notes = banner + show_notes
        with db.session() as s:
            ep = s.get(Episode, ep_id)
            # Feed title makes the source explicit (esp. in the combined feed) and
            # flags a preview up front.
            base_title = f"{source_label}: {title}" if source.type != "digest" else title
            ep.title = f"[Preview] {base_title}" if is_preview else base_title
            ep.audio_file = filename
            ep.audio_bytes = size
            ep.audio_seconds = seconds
            ep.voice = voice
            ep.status = "ready"
            ep.published_at = utcnow()
            ep.description = show_notes[:25000]  # full scroll-along notes incl. images
            ep.image_url = episode_image
            ep.script = json.dumps({"blocks": spoken_blocks}, ensure_ascii=False)
            ep.provenance = json.dumps(prov, ensure_ascii=False)
            s.add(ep)
            s.commit()
        log.info("episode ready: [%s] %s (%ss)", source.slug, title, seconds)
    except Exception as exc:
        log.exception("episode %s failed", ep_id)
        with db.session() as s:
            ep = s.get(Episode, ep_id)
            ep.status = "error"
            ep.error = str(exc)[:500]
            s.add(ep)
            s.commit()


async def _enrich_items(items: list[dict], min_summary: int = 120) -> None:
    """Feeds like DR's carry only headlines — fetch the article lead for those."""
    semaphore = asyncio.Semaphore(4)

    async def enrich(item: dict) -> None:
        if len(strip_html(item.get("summary", ""))) >= min_summary or not item.get("link"):
            return
        async with semaphore:
            try:
                html_text = await fetch_html(item["link"])
                _, body = extract_article(html_text, item["link"])
                if body:
                    lead = body[:900]
                    if "." in lead[200:]:
                        lead = lead[: lead.rindex(".") + 1]
                    item["summary"] = lead
            except Exception as exc:
                log.warning("digest enrich failed for %s: %s", item.get("link"), exc)

    await asyncio.gather(*(enrich(i) for i in items))


async def build_digest(source: SourceDef) -> bool:
    """Build one digest episode from items newer than the last build,
    aggregated across all of the source's feed URLs."""
    feeds = await asyncio.gather(
        *(_parse_feed(u) for u in source.feed_urls()), return_exceptions=True
    )

    with db.session() as s:
        last_iso = db.kv_get(s, f"digest_last:{source.slug}")
    cutoff = (
        datetime.fromisoformat(last_iso)
        if last_iso
        else utcnow() - timedelta(hours=24)
    )

    candidates = []
    for parsed in feeds:
        if isinstance(parsed, Exception) or not getattr(parsed, "entries", None):
            log.warning("digest %s: a feed failed: %s", source.slug, parsed)
            continue
        for entry in parsed.entries:
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            when = (
                datetime(*published[:6], tzinfo=UTC) if published else None
            )
            # No date -> can't tell if it's new; skip rather than re-include it in
            # every digest (all digest feeds carry dates in practice).
            if when is None or when <= cutoff:
                continue
            candidates.append(
                {"title": entry.get("title", ""), "summary": entry.get("summary", ""),
                 "link": entry.get("link", ""), "when": when}
            )

    # Dedupe (same story via several feeds) and keep the newest first.
    seen: set[str] = set()
    items = []
    for item in sorted(candidates, key=lambda i: i["when"], reverse=True):
        key = item["link"] or item["title"].lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= source.digest_max_items:
            break
    await _enrich_items(items)
    if not items:
        log.info("digest %s: nothing new since %s", source.slug, cutoff)
        return False

    now = utcnow()
    language = source.language if source.language in ("da", "en") else "da"
    date_str = spoken_date(now, language)
    script, gen_prov = await digest_script(source.name, date_str, items, language)
    voice = pick_voice(source, language)
    title = f"{source.name} – {now.strftime('%Y-%m-%d %H:%M')}"

    async with _tts_lock:
        filename, size, seconds = await synthesize(
            script, voice=voice, title=title, album=source.name,
            artist=source.name, date=str(now.year), cover=_source_cover(source),
        )

    show_notes = "\n".join(
        f"• {i['title']}" + (f" — {i['link']}" if i["link"] else "") for i in items
    )
    prov = {
        "pipeline_version": PIPELINE_VERSION, "path": "digest",
        "source": source.slug, "language": language,
        "generated_at": now.isoformat(), "items": len(items),
        "voices": {"main": voice}, **gen_prov,
    }
    with db.session() as s:
        s.add(Episode(
            source_slug=source.slug,
            guid=f"digest:{source.slug}:{now.isoformat()}",
            title=title,
            description=show_notes[:2000],
            audio_file=filename, audio_bytes=size, audio_seconds=seconds,
            voice=voice, status="ready", published_at=now,
            script=json.dumps({"blocks": [{"voice": voice, "text": script}]},
                              ensure_ascii=False),
            provenance=json.dumps(prov, ensure_ascii=False),
        ))
        s.commit()
        db.kv_set(s, f"digest_last:{source.slug}", now.isoformat())
    log.info("digest ready: [%s] %d items, %ss", source.slug, len(items), seconds)
    return True


async def submit_url(url: str, title: str = "", language: str = "auto") -> int:
    """Create an inbox episode for a shared URL; processing happens async."""
    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        existing = s.exec(
            select(Episode).where(
                Episode.source_slug == inbox.slug, Episode.guid == url
            )
        ).first()
        if existing:
            if existing.status == "error":  # allow retry by resubmitting
                existing.status = "pending"
                s.add(existing)
                s.commit()
                spawn(process_episode(existing.id, inbox))
            return existing.id
        ep = Episode(source_slug=inbox.slug, guid=url, title=title or "Untitled", link=url)
        s.add(ep)
        try:
            s.commit()
        except IntegrityError:  # concurrent duplicate submit — return the existing row
            s.rollback()
            dup = s.exec(
                select(Episode).where(
                    Episode.source_slug == inbox.slug, Episode.guid == url
                )
            ).first()
            return dup.id if dup else 0
        s.refresh(ep)
        ep_id = ep.id
    if language in ("da", "en"):
        inbox = SourceDef(**{**inbox.__dict__, "language": language, "voice": ""})
    spawn(process_episode(ep_id, inbox))
    return ep_id


async def cleanup_old_episodes(retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    removed = 0
    with db.session() as s:
        old = s.exec(select(Episode).where(Episode.created_at < cutoff)).all()
        for ep in old:
            if ep.audio_file:
                (MEDIA_DIR / ep.audio_file).unlink(missing_ok=True)
            s.delete(ep)
            removed += 1
        s.commit()
    if removed:
        log.info("cleanup: removed %d episodes older than %d days", removed, retention_days)
    return removed
