"""Generate RSS 2.0 podcast XML (with iTunes tags) for one source or all."""
from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime
from xml.sax.saxutils import escape

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
        f'      <enclosure url="{escape(audio_url)}" length="{ep.audio_bytes}" type="audio/mpeg"/>',
        f"      <itunes:author>{escape(source_name)}</itunes:author>",
        f"      <itunes:episode>{ep.id}</itunes:episode>",
    ]
    if ep.image_url:
        parts.append(f'      <itunes:image href="{escape(ep.image_url)}"/>')
    if ep.audio_seconds:
        parts.append(f"      <itunes:duration>{_duration(ep.audio_seconds)}</itunes:duration>")
    parts.append("    </item>")
    return "\n".join(parts)


def build_feed(
    config: AppConfig,
    base: str,
    token: str,
    source: SourceDef | None = None,
    limit: int = 100,
) -> str:
    """source=None builds the combined 'all' feed."""
    names = {s.slug: s.name for s in config.sources}
    with db.session() as s:
        query = select(Episode).where(Episode.status == "ready")
        if source is not None:
            query = query.where(Episode.source_slug == source.slug)
        query = query.order_by(Episode.published_at.desc()).limit(limit)  # type: ignore[union-attr]
        episodes = s.exec(query).all()

    slug = source.slug if source else "all"
    title = source.name if source else f"{config.author} – All feeds"
    description = (source.description or source.name) if source else \
        "Combined feed of all podcastfeeds sources"
    feed_url = f"{base}/{token}/feeds/{slug}.xml"
    cover_url = f"{base}/{token}/covers/{slug}.png"

    items = "\n".join(
        _item_xml(ep, base, token, names.get(ep.source_slug, config.author))
        for ep in episodes
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(title)}</title>
    <link>{escape(base)}</link>
    <description>{escape(description)}</description>
    <language>da</language>
    <atom:link href="{escape(feed_url)}" rel="self" type="application/rss+xml"/>
    <lastBuildDate>{_rfc2822(None)}</lastBuildDate>
    <itunes:author>{escape(config.author)}</itunes:author>
    <itunes:image href="{escape(cover_url)}"/>
    <itunes:category text="News"/>
    <itunes:explicit>false</itunes:explicit>
{items}
  </channel>
</rss>
"""
