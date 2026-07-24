"""Substack content via the public post API.

Fetching the API on {subdomain}.substack.com (rather than scraping the web
page) is robust in three ways the HTML path is not:
  * it stays on substack.com, so the substack.com session cookie always
    applies — custom-domain publications (slowboring.com etc.) 301-redirect
    their web pages and drop the cookie, paywalling paid posts;
  * the `audience` field ("everyone" / "only_paid" / "founding") is a
    definitive paid/free signal — no guessing from paywall text;
  * `body_html` is clean article HTML.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import httpx

from .config import SourceDef
from .extract import UA, _cookie_for, is_paywalled

log = logging.getLogger("podcastfeeds")

# A paid post whose delivered body falls below this share of the API's
# `wordcount` (full-post length) is a truncated logged-out preview. Full
# bodies land near 1.0; observed previews land at 0.05–0.35.
_TRUNCATION_RATIO = 0.7


def _delivered_words(body_html: str) -> int:
    return len(re.sub(r"<[^>]+>", " ", body_html).split())


def substack_ref(source: SourceDef, link: str) -> tuple[str, str] | None:
    """Return (subdomain, slug) if this source+link is a Substack post, else None."""
    feed_host = urlparse(source.url).netloc
    if not feed_host.endswith(".substack.com"):
        return None
    sub = feed_host[: -len(".substack.com")]
    path = urlparse(link).path
    if "/p/" not in path:
        return None
    slug = path.rstrip("/").split("/")[-1]
    return (sub, slug) if slug else None


async def _api_json(url: str, cookie_url: str | None = None) -> dict | None:
    headers = {"User-Agent": UA, "Accept": "application/json"}
    cookie = _cookie_for(cookie_url or url)
    if cookie:
        headers["Cookie"] = cookie
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30,
                                     headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        log.warning("substack API failed for %s: %s", url, exc)
        return None


async def fetch_post(sub: str, slug: str) -> dict | None:
    """Return {title, body_html, cover_image, audience, accessible, ...} or None."""
    data = await _api_json(f"https://{sub}.substack.com/api/v1/posts/{slug}")
    if data is None:
        return None
    post = post_from_api(data)
    if not post["accessible"] and data.get("id"):
        # Some subscription types (e.g. reader-app billing, unlike web/Stripe
        # subs) are honored by the substack.com host but NOT the publication
        # subdomain: the same session gets a truncated body from
        # {sub}.substack.com yet the full one from substack.com's by-id
        # endpoint (noahpinion, 2026-07-24).
        alt = await _api_json(
            f"https://substack.com/api/v1/posts/by-id/{data['id']}")
        if alt and isinstance(alt.get("post"), dict):
            alt_post = post_from_api(alt["post"])
            if alt_post["accessible"]:
                log.info("substack by-id fallback recovered the full body "
                         "for %s/%s", sub, slug)
                return alt_post
    return post


def post_from_api(data: dict) -> dict:
    """Build the fetch_post result from the API JSON (pure, testable).

    Accessible = free post, or a paid post whose full body came back (i.e. the
    cookie is a live subscriber session). Truncation is judged against the
    API's `wordcount` (full-post length): an expired/logged-out session gets
    HTTP 200 with a truncated body_html that carries NO paywall CTA, so the
    is_paywalled text check alone misses it (ep. 243, silent since 2026-07-17).
    """
    body_html = data.get("body_html") or ""
    audience = data.get("audience") or "everyone"
    wordcount = int(data.get("wordcount") or 0)
    delivered = _delivered_words(body_html)
    truncated = wordcount > 0 and delivered < _TRUNCATION_RATIO * wordcount
    accessible = audience == "everyone" or (
        bool(body_html) and not truncated and not is_paywalled("", body_html)
    )
    return {
        "title": data.get("title") or "",
        "body_html": body_html,
        "cover_image": data.get("cover_image") or "",
        "audience": audience,
        "accessible": accessible,
        "wordcount": wordcount,
        "delivered_words": delivered,
    }
