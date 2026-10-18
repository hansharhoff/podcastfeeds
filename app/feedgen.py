"""Generate RSS 2.0 podcast XML (with iTunes tags) for one source or all."""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape, quoteattr

from sqlalchemy.orm import defer
from sqlmodel import select

from . import db
from .config import AppConfig, SourceDef
from .db import Episode


def _rfc2822(dt: datetime | None) -> str:
    if dt is None:
        dt = datetime.now(UTC)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return format_datetime(dt)


def _duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _item_xml(ep: Episode, base: str, token: str, source_name: str) -> str:
    audio_url = f"{base}/{token}/media/{ep.audio_file}" if ep.audio_file else ep.audio_url
    raw_desc = ep.description or ep.title
    if raw_desc.lstrip().startswith("<"):  # HTML show notes (images etc.)
        desc = f"<![CDATA[{raw_desc.replace(']]>', ']]&gt;')}]]>"
    else:
        desc = escape(raw_desc)
    link = escape(ep.link or audio_url)
    parts = [
        "    <item>",
        f"      <title>{escape(ep.title)}</title>",
        f"      <description>{desc}</description>",
        f"      <link>{link}</link>",
        f'      <guid isPermaLink="false">{escape(f"{ep.source_slug}:{ep.guid}")}</guid>',
        f"      <pubDate>{_rfc2822(ep.published_at or ep.created_at)}</pubDate>",
        # quoteattr, not escape: escape() leaves '"' alone, and one quote in a
        # feed-supplied enclosure URL or og:image would close the attribute and
        # invalidate the whole feed — every episode of that source disappears,
        # not just the bad one.
        f'      <enclosure url={quoteattr(audio_url)} length="{ep.audio_bytes}" type="audio/mpeg"/>',
        f"      <itunes:author>{escape(source_name)}</itunes:author>",
        f"      <itunes:episode>{ep.id}</itunes:episode>",
    ]
    if ep.image_url:
        parts.append(f"      <itunes:image href={quoteattr(ep.image_url)}/>")
    if ep.audio_seconds:
        parts.append(f"      <itunes:duration>{_duration(ep.audio_seconds)}</itunes:duration>")
    parts.append("    </item>")
    return "\n".join(parts)


# Newest episodes listed per feed. Retention keeps a year of episodes, so a
# small cap here hid most of them: at 100 the combined feed reached back only
# about two weeks.
FEED_ITEM_LIMIT = 1000


def build_feed(
    config: AppConfig,
    base: str,
    token: str,
    source: SourceDef | None = None,
    limit: int = FEED_ITEM_LIMIT,
) -> str:
    """source=None builds the combined 'all' feed."""
    names = {s.slug: s.name for s in config.sources}
    with db.session() as s:
        query = select(Episode).where(Episode.status == "ready")
        if source is not None:
            query = query.where(Episode.source_slug == source.slug)
        else:
            hidden = [s.slug for s in config.sources if not s.in_all_feed]
            if hidden:
                query = query.where(Episode.source_slug.not_in(hidden))  # type: ignore[attr-defined]
        query = query.order_by(Episode.published_at.desc()).limit(limit)  # type: ignore[union-attr]
        # The feed never reads these, and at 1000 episodes they are ~18 MB of
        # scripts and article text per request.
        query = query.options(*(defer(col) for col in (  # type: ignore[arg-type]
            Episode.script, Episode.source_text, Episode.provenance, Episode.feedback)))
        episodes = s.exec(query).all()

    slug = source.slug if source else "all"
    title = source.name if source else f"{config.author} – All feeds"
    description = (source.description or source.name) if source else \
        "Combined feed of all podcastfeeds sources"
    # The newest episode, not "now": a feed that changes on every request can
    # never be answered with 304 Not Modified.
    newest = max((ep.published_at or ep.created_at for ep in episodes), default=None)
    built = _rfc2822(newest or datetime(2026, 1, 1, tzinfo=UTC))
    # Per-source language when it is known; the combined feed mixes languages.
    lang = source.language if source and source.language in ("da", "en") else ""
    lang_xml = f"\n    <language>{lang}</language>" if lang else ""
    feed_url = f"{base}/{token}/feeds/{slug}.xml"
    cover_url = f"{base}/{token}/covers/{slug}.png"

    items = "\n".join(
        _item_xml(ep, base, token, names.get(ep.source_slug, config.author))
        for ep in episodes
    )
    if source is None:
        from .monitor import alert_item_xml

        alert = alert_item_xml(base, token)
        items = f"{alert}\n{items}" if alert else items
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(title)}</title>
    <link>{escape(base)}</link>
    <description>{escape(description)}</description>{lang_xml}
    <atom:link href={quoteattr(feed_url)} rel="self" type="application/rss+xml"/>
    <lastBuildDate>{built}</lastBuildDate>
    <itunes:author>{escape(config.author)}</itunes:author>
    <itunes:image href={quoteattr(cover_url)}/>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
{items}
  </channel>
</rss>
"""
