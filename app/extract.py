"""Fetch pages and extract readable article text."""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import xml.etree.ElementTree as ET

import httpx
import trafilatura
from PIL import Image

log = logging.getLogger("podcastfeeds")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) podcastfeeds/1.0"

# Reader proxy for pages that bot-block our direct fetch (e.g. openai.com sits
# behind a Cloudflare challenge and 403s the UA above). Used as a FALLBACK only,
# so cookie-based paid fetches still go direct first; set READER_PROXY="" to disable.
READER_PROXY = os.environ.get("READER_PROXY", "https://r.jina.ai/")

DANISH_MARKERS = {
    "og", "det", "der", "ikke", "på", "af", "til", "med", "som", "være",
    "også", "efter", "hvor", "kan", "skal", "vil", "ved", "sig", "har", "fra",
}


def _blocked_target(url: str) -> bool:
    """True if the URL resolves to a private/loopback/link-local/reserved address
    — blocks SSRF (e.g. cloud metadata 169.254.169.254, localhost, LAN) via a
    token-holder's submitted URL or a malicious <img src> on a fetched page."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True  # unresolvable → refuse
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


# Social platforms whose post pages are typically a pointer, not the payload
# (ep. 253 feedback: a tweet pointing at an essay got narrated as the tweet's
# own short excerpt instead of the essay). Mastodon is federated — any domain
# can run an instance — so only the flagship instance is listed; self-hosted
# instances aren't recognized by host alone and fall back to today's behavior.
LINK_FORWARDING_HOSTS = {
    "x.com", "twitter.com", "mobile.twitter.com",
    "bsky.app",
    "threads.net", "www.threads.net",
    "mastodon.social",
}

# Asset/CDN hosts that show up as <a href> targets on these platforms (image
# links, not article links) — skipped the same way as another platform post.
_JUNK_LINK_HOST_SUFFIXES = ("twimg.com",)

_SKIP_LINK_HOST_SUFFIXES = tuple(LINK_FORWARDING_HOSTS) + _JUNK_LINK_HOST_SUFFIXES


def is_link_forwarding_post(url: str) -> bool:
    """True if `url` is a post page on a known link-forwarding social
    platform — a candidate for `outbound_link` rather than direct narration."""
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in LINK_FORWARDING_HOSTS)


# X (and likely other JS-app-shell platforms) doesn't put the outbound link
# in an <a href> at all — live-test against the actual ep. 253 post
# (2076957440109625718) found its 40 anchors are all x.com/twitter.com nav,
# while the real link appears only as the (identical, repeated) content of
# these meta tags and inside embedded JSON. Regex, not lxml: attribute order
# in the wild isn't reliable enough to hardcode.
_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.I)
_META_ATTR_RE = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_META_LINK_NAMES = {"og:description", "twitter:description", "description"}

# The raw t.co pattern also catches the link where it's embedded in the
# page's inline JSON state — no meta tag needed for that one.
_TCO_RE = re.compile(r"https://t\.co/\w+")


def _meta_link_candidates(html_text: str) -> list[str]:
    """og:description / twitter:description / name=description content,
    when it's a bare URL — some platforms (X) render the post's own link
    there instead of the post's text."""
    out = []
    for tag in _META_TAG_RE.findall(html_text):
        attrs = dict(_META_ATTR_RE.findall(tag))
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content", "")
        if key in _META_LINK_NAMES and content.startswith("http"):
            out.append(html.unescape(content))
    return out


# Link-shorteners whose destination the poster actually wants read — a t.co
# link is the raw candidate `outbound_link` returns for an X post. Only t.co
# has direct evidence behind it; other shorteners aren't resolved (documented
# gap, not a silent failure — they'll just be treated as a normal external
# link and fetched as-is).
_SHORT_LINK_HOSTS = {"t.co"}


def is_short_link(url: str) -> bool:
    """True if `url`'s host is a known link-shortener that must be resolved
    to its real destination BEFORE the same-platform skip check runs (ep. 253
    live-test: t.co resolves back to an x.com article, and skipping that has
    to happen before we'd otherwise fetch it in full)."""
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower() in _SHORT_LINK_HOSTS


async def resolve_short_link(url: str) -> str:
    """Follow a short-link redirect to its real destination, reading only
    response headers — never downloads the target body just to learn its
    host. Returns `url` unchanged if resolution fails (the caller re-applies
    its skip/fetch logic to whatever comes back, so an unresolved t.co simply
    gets treated — and probably rejected — as an ordinary external link)."""
    if _blocked_target(url):
        return url
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15, headers={"User-Agent": UA}
        ) as client, client.stream("GET", url) as resp:
            return str(resp.url)
    except Exception:
        return url


_JS_SHELL_RE = re.compile(
    r"javascript is disabled|enable javascript|something went wrong|supported browsers",
    re.I,
)


def looks_like_boilerplate(text: str) -> bool:
    """True when extracted text is app-shell/error boilerplate rather than
    real content — e.g. an X-native long-form article renders nothing without
    JS (ep. 253 live-test: fetching the resolved target came back "We've
    detected that JavaScript is disabled in this browser..."). Checked before
    letting a followed link replace the post's own extraction, since a short
    boilerplate stub can otherwise slip past a bare length check."""
    return bool(_JS_SHELL_RE.search(text[:2000]))


def outbound_link(html_text: str, source_url: str) -> str:
    """First plausible external article link on a fetched social-post page —
    the thing the poster is actually pointing at. Checks, in order: <a href>
    links, then og:description/twitter:description/description meta content,
    then raw t.co occurrences anywhere in the page (X puts the link in the
    latter two, not in an anchor — see `_meta_link_candidates`). Returns ""
    if nothing plausible is found, so the caller can fall back to narrating
    the post itself (never a hard error).

    Skips: links back to the same host (other posts, profile, nav), other
    known social-platform hosts (never chain from one post to another), and
    that platform's own CDN. A short link (t.co) is returned as-is — the
    caller resolves it (see `resolve_short_link`) before deciding whether to
    fetch it in full.
    """
    from urllib.parse import urlparse

    from lxml import html as lh

    source_host = (urlparse(source_url).hostname or "").lower()

    def plausible(href: str) -> str:
        href = (href or "").strip()
        if not href.startswith("http"):
            return ""  # skip #anchors, mailto:, javascript:, relative nav links
        host = (urlparse(href).hostname or "").lower()
        if not host or host == source_host:
            return ""  # link back to another post/page on the same platform
        if any(host == h or host.endswith("." + h) for h in _SKIP_LINK_HOST_SUFFIXES):
            return ""  # another social platform's post, or its CDN
        return href

    anchors = []
    try:
        root = lh.fromstring(html_text)
        anchors = [a.get("href", "") for a in root.iter("a")]
    except Exception:
        pass  # unparsable markup — meta/regex candidates below still apply

    for href in anchors + _meta_link_candidates(html_text) + _TCO_RE.findall(html_text):
        candidate = plausible(href)
        if candidate:
            return candidate
    return ""


def _cookie_for(url: str) -> str:
    """Match the request host against configured cookie domains (suffix match),
    so paid publications are fetched as the logged-in subscriber. The most
    specific (longest) matching domain wins, so a per-publication session
    (e.g. "noahpinion.substack.com" for a sub that lives on a second account)
    overrides the generic "substack.com" one."""
    from urllib.parse import urlparse

    from .config import load_cookies

    host = urlparse(url).netloc.lower()
    best = ""
    best_len = -1
    for domain, cookie in load_cookies().items():
        bare = domain.lstrip(".")
        if (host == bare or host.endswith("." + bare)) and len(bare) > best_len:
            best, best_len = cookie, len(bare)
    return best


async def fetch_html(url: str) -> str:
    if _blocked_target(url):
        raise RuntimeError(f"refusing to fetch non-public address: {url}")
    headers = {"User-Agent": UA}
    cookie = _cookie_for(url)
    if cookie:
        headers["Cookie"] = cookie
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=30, headers=headers
    ) as client:
        resp = await client.get(url)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Bot-block (Cloudflare-style 403/429): retry via the reader proxy,
            # which fetches the public page for us. No cookies to the proxy.
            if READER_PROXY and exc.response.status_code in (403, 429):
                try:
                    return await _fetch_via_proxy(url)
                except Exception:
                    raise exc from None  # proxy failed too -> surface the original error
            raise
        return resp.text


async def _fetch_via_proxy(url: str) -> str:
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=45,
        headers={"User-Agent": UA, "X-Return-Format": "html"},
    ) as client:
        resp = await client.get(READER_PROXY + url)
        resp.raise_for_status()
        return resp.text


def pdf_text(data: bytes) -> str:
    """Text content of a PDF, pages joined by blank lines. v1 of the queue's
    PDF path is text-only (spec §3) — figures/layout are not preserved."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


async def fetch_pdf_text(url: str) -> str:
    """Download a PDF and extract its text (queue-approved PDFs only — the
    RSS-side skip in ingest.process_episode stays)."""
    if _blocked_target(url):
        raise RuntimeError(f"refusing to fetch non-public address: {url}")
    headers = {"User-Agent": UA}
    async with httpx.AsyncClient(
        timeout=120, follow_redirects=True, headers=headers
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return pdf_text(resp.content)


def extract_article(html_text: str, url: str = "") -> tuple[str, str]:
    """Returns (title, body_text)."""
    meta = trafilatura.extract_metadata(html_text, default_url=url or None)
    title = (meta.title if meta else "") or ""
    # Page titles usually carry site-name cruft: "Headline | Section | Site"
    if " | " in title:
        title = title.split(" | ")[0]
    body = trafilatura.extract(
        html_text, url=url or None, include_comments=False, include_tables=False,
        favor_recall=True,
    ) or ""
    return title.strip(), body.strip()


def _img_src(el) -> str:
    """Best image URL from an <img>/<figure>/captioned-image element: prefer the
    full-res link (Substack wraps images in <a href=cdn>), then <img src>, then
    the first <source srcset> URL."""
    for a in el.iter("a"):
        href = a.get("href", "")
        if "substackcdn.com/image" in href or href.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return href
    for img in ([el] if el.tag == "img" else el.iter("img")):
        if img.get("src", "").startswith("http"):
            return img.get("src")
    for src in el.iter("source"):
        first = (src.get("srcset", "").split(",")[0] or "").strip().split(" ")[0]
        if first.startswith("http"):
            return first
    return ""


# Substack renders a quoted tweet as an empty <div class="twitter-embed"
# data-attrs="{json}"> — the tweet body lives ONLY in that JSON, so a DOM walk
# sees no text and drops it (ep 380: "spotted by :" trailed off into silence).
# Other embeds on the same mechanism (subscribe widgets, share buttons) carry
# no article content and are deliberately absent from this map.
_NARRATABLE_EMBEDS = ("twitter-embed",)

_URL_RE = re.compile(r"https?://\S+")


def _embed_attrs(el) -> dict | None:
    """Decoded data-attrs JSON for a narratable embed div, else None."""
    cls = el.get("class", "") or ""
    if not any(name in cls for name in _NARRATABLE_EMBEDS):
        return None
    raw = el.get("data-attrs") or ""
    if not raw:
        return None
    try:
        attrs = json.loads(raw)
    except (ValueError, TypeError):
        log.warning("unparseable embed data-attrs on .%s", cls)
        return None
    return attrs if isinstance(attrs, dict) else None


def _speakable_tweet(text: str) -> str:
    """One narratable paragraph from a tweet's raw text.

    Tweets are line-broken prose, often with `>` greentext markers that a
    voice would either skip or read as "greater than". Each line becomes a
    sentence instead."""
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*>+\s*", "", line).strip()
        line = re.sub(r"\s+", " ", line)
        if line:
            lines.append(line if line[-1] in ".!?,;:" else line + ".")
    return " ".join(lines)


def _embed_segments(attrs: dict) -> list[dict]:
    """Tweet embed -> a quote segment (attributed) plus any attached photos.

    The photos matter as much as the words here: on screenshot-heavy posts the
    tweet's image IS the content being pointed at, and it feeds the vision
    captioner downstream."""
    # full_text arrives double-escaped (&amp;gt; in the attribute -> &gt; after
    # lxml unescapes it), and bare t.co URLs read terribly aloud.
    text = html.unescape(str(attrs.get("full_text") or "")).strip()
    text = _URL_RE.sub("", text)
    text = _speakable_tweet(text)
    segments: list[dict] = []
    if text:
        who = str(attrs.get("name") or "").strip()
        handle = str(attrs.get("username") or "").strip()
        if not who and handle:
            who = f"@{handle}"
        segments.append({
            "type": "quote",
            "text": f"{who} on X: {text}" if who else text,
        })
    for photo in attrs.get("photos") or []:
        src = (photo or {}).get("img_url", "") if isinstance(photo, dict) else ""
        if isinstance(src, str) and src.startswith("http"):
            segments.append({"type": "image", "src": src, "caption": ""})
    return segments


def segments_from_clean_html(body_html: str) -> tuple[str, list[dict]]:
    """Reading-order segments from ALREADY-CLEAN article HTML (Substack API
    body_html, DR article JSON, reader-proxy output) — a direct DOM walk that,
    unlike trafilatura, reliably keeps images and headings from a fragment.

    Segment types: text | heading | quote | image (same shape as extract_segments).
    """
    from lxml import html as lh

    try:
        root = lh.fromstring(f"<div>{body_html}</div>")
    except Exception:
        return "", []
    segments: list[dict] = []
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def is_image_block(el) -> bool:
        cls = el.get("class", "") or ""
        return el.tag == "figure" or "captioned-image" in cls or (
            el.tag == "img"
        )

    def emit_image(el):
        src = _img_src(el)
        cap_el = el.find(".//figcaption")
        caption = cap_el.text_content().strip() if cap_el is not None else ""
        if src.startswith("http"):
            segments.append({"type": "image", "src": src, "caption": caption})

    def _walk_list(lst):
        # Each <li>'s OWN text (nested lists excluded), then its non-text
        # blocks in document order — otherwise the outer item's text_content()
        # already contains every nested item and they get narrated twice
        # (ep 236). Images and embeds are blocks too: a listicle whose every
        # point ends in a screenshot lost all of them while this loop only
        # looked for text (ep 380).
        for li in lst.iterchildren("li"):
            parts = [li.text or ""]
            blocks = []
            for sub in li:
                is_block = isinstance(sub.tag, str) and (
                    sub.tag in ("ul", "ol")
                    or is_image_block(sub)
                    or _embed_attrs(sub) is not None
                )
                if is_block:
                    blocks.append(sub)
                else:
                    parts.append(sub.text_content())
                parts.append(sub.tail or "")
            t = " ".join(p.strip() for p in parts if p and p.strip())
            if t:
                segments.append({"type": "text", "text": t})
            for sub in blocks:
                if sub.tag in ("ul", "ol"):
                    _walk_list(sub)
                elif is_image_block(sub):
                    emit_image(sub)
                else:
                    segments.extend(_embed_segments(_embed_attrs(sub) or {}))

    def walk(el):
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            if not tag:
                continue
            embed = _embed_attrs(child)
            if embed is not None:
                segments.extend(_embed_segments(embed))
            elif tag in HEADINGS:
                t = child.text_content().strip()
                if t:
                    segments.append({"type": "heading", "text": t})
            elif tag == "blockquote":
                t = child.text_content().strip()
                if t:
                    segments.append({"type": "quote", "text": t})
            elif is_image_block(child):
                emit_image(child)
            elif tag == "p":
                # A paragraph may embed an inline image (rare) — capture text then it.
                t = child.text_content().strip()
                if t:
                    segments.append({"type": "text", "text": t})
                for img in child.iter("img"):
                    if img.get("src", "").startswith("http"):
                        segments.append({"type": "image", "src": img.get("src"), "caption": ""})
            elif tag in ("ul", "ol"):
                _walk_list(child)
            else:
                walk(child)  # descend into wrappers (div, section, article, a…)

    walk(root)
    return "", segments


def extract_segments(html_text: str, url: str = "") -> tuple[str, list[dict]]:
    """Structured extraction preserving reading order.

    Returns (title, segments) where each segment is one of:
      {"type": "text",    "text": ...}
      {"type": "heading", "text": ...}                  — starts a chapter (section)
      {"type": "quote",   "text": ...}                  — read in a second voice
      {"type": "image", "src": ..., "caption": ...}     — announced + show notes + chapter art
    Empty segment list means the caller should fall back to plain extraction.
    """
    meta = trafilatura.extract_metadata(html_text, default_url=url or None)
    title = ((meta.title if meta else "") or "").split(" | ")[0].strip()

    xml = trafilatura.extract(
        html_text, url=url or None, output_format="xml", include_images=True,
        include_comments=False, include_tables=False, favor_recall=True,
    )
    if not xml:
        return title, []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return title, []
    main = root.find("main")
    if main is None:
        return title, []

    segments: list[dict] = []

    def text_of(el) -> str:
        return " ".join("".join(el.itertext()).split())

    def add_graphics(el) -> None:
        for g in el.iter("graphic"):
            src = g.get("src", "")
            if src.startswith("http"):
                segments.append({
                    "type": "image", "src": src,
                    "caption": (g.get("alt") or g.get("title") or "").strip(),
                })

    def walk(el) -> None:
        for child in el:
            if child.tag == "graphic":
                src = child.get("src", "")
                if src.startswith("http"):
                    segments.append({
                        "type": "image", "src": src,
                        "caption": (child.get("alt") or child.get("title") or "").strip(),
                    })
            elif child.tag == "quote":
                t = text_of(child)
                if t:
                    segments.append({"type": "quote", "text": t})
                add_graphics(child)
            elif child.tag == "head":
                t = text_of(child)
                if t and t != title:
                    segments.append({"type": "heading", "text": t})
                add_graphics(child)
            elif child.tag in ("p", "item"):
                t = text_of(child)
                if t and t != title:
                    segments.append({"type": "text", "text": t})
                add_graphics(child)
            else:
                walk(child)

    walk(main)
    return title, segments


_DIALOGUE_RE = re.compile(
    r"^([A-Z][A-Za-z.'’-]{1,25}(?:\s[A-Z][A-Za-z.'’-]{1,25}){0,2}):\s+(\S.*)$",
    re.S,
)


def mark_dialogue(segments: list[dict]) -> list[dict]:
    """Detect interview/transcript posts (speaker labels at paragraph start) and
    convert text segments to {"type":"dialogue","speaker","text"} segments.

    A post counts as an interview only if at least two distinct speaker labels
    each appear >= 2 times — this avoids false positives from one-off leads like
    'She asks:'. Unlabelled paragraphs that continue a speaker's turn inherit the
    current speaker; headings/quotes/images reset it. Pure, no I/O.
    """
    counts: dict[str, int] = {}
    for seg in segments:
        if seg.get("type") != "text":
            continue
        m = _DIALOGUE_RE.match(seg["text"])
        if m:
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
    speakers = {label for label, n in counts.items() if n >= 2}
    if len(speakers) < 2:
        return segments  # not an interview transcript — leave untouched

    out: list[dict] = []
    current_speaker: str | None = None
    for seg in segments:
        if seg.get("type") == "text":
            m = _DIALOGUE_RE.match(seg["text"])
            if m and m.group(1) in speakers:
                current_speaker = m.group(1)
                out.append({"type": "dialogue", "speaker": current_speaker,
                            "text": m.group(2)})
            elif current_speaker is not None:
                # Continuation of a multi-paragraph turn by the current speaker.
                out.append({"type": "dialogue", "speaker": current_speaker,
                            "text": seg["text"]})
            else:
                out.append(seg)
        else:
            current_speaker = None  # heading/quote/image ends a turn
            out.append(seg)
    return out


_QA_MAX_QUESTION = 400  # a reader question is usually concise; longer = probably prose


def _is_question(text: str) -> bool:
    t = text.strip()
    return t.endswith("?") and 0 < len(t) <= _QA_MAX_QUESTION


def mark_qa(segments: list[dict]) -> list[dict]:
    """Detect an unlabelled reader mailbag / Q&A post (question paragraphs each
    followed by an answer paragraph, with no 'Q:'/'A:' labels) and tag the question
    segments as {"type":"question"} so they can be read in a distinct voice with a
    spoken cue. Requires several Q->A pairs AND a meaningful density of them, so a
    normal essay with a few rhetorical questions is left untouched. Pure, no I/O.
    """
    text_count = sum(1 for s in segments if s.get("type") == "text")
    if text_count < 6:
        return segments
    q_positions: set[int] = set()
    for i, seg in enumerate(segments):
        if seg.get("type") != "text" or not _is_question(seg["text"]):
            continue
        nxt = segments[i + 1] if i + 1 < len(segments) else None
        if nxt and nxt.get("type") == "text" and not _is_question(nxt["text"]):
            q_positions.add(i)
    if len(q_positions) < 3 or len(q_positions) < 0.15 * text_count:
        return segments  # not a mailbag — too few / too sparse questions
    return [
        {"type": "question", "text": seg["text"]} if i in q_positions else seg
        for i, seg in enumerate(segments)
    ]


def extract_og_image(html_text: str, url: str = "") -> str:
    """The page's lead/social image, if any (used as episode artwork)."""
    meta = trafilatura.extract_metadata(html_text, default_url=url or None)
    image = (getattr(meta, "image", "") or "") if meta else ""
    return image if image.startswith("http") else ""


def image_area(jpeg: bytes) -> int:
    """Pixel area of a JPEG (0 if undecodable) — for picking the largest image."""
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            return img.width * img.height
    except Exception:
        return 0


async def fetch_image_jpeg(url: str, max_px: int = 1000) -> bytes | None:
    """Download an image and normalize it to a reasonably-sized JPEG
    (used as embedded chapter art)."""
    if _blocked_target(url):
        log.warning("refusing to fetch image from non-public address: %s", url)
        return None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30, headers={"User-Agent": UA}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        if min(img.size) < 200:  # avatars, icons, tracking pixels
            return None
        img = img.convert("RGB")
        img.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception as exc:
        log.warning("chapter image fetch failed for %s: %s", url, exc)
        return None


def strip_html(text: str) -> str:
    """Turn an RSS description/content fragment into plain text."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


_PAYWALL_RE = re.compile(
    r"paid subscriber|subscribe to (keep|continue) reading|this post is for pa|"
    r"upgrade to paid|become a paid|only paid subscribers|for paying subscribers",
    re.I,
)


def is_paywalled(body: str, html: str = "") -> bool:
    """True when extracted text looks like a truncated paywall stub."""
    if _PAYWALL_RE.search(body):
        return True
    # Very short body + paywall marker anywhere in the page = truncated post.
    return len(body) < 600 and bool(_PAYWALL_RE.search(html[:20000]))


def detect_language(text: str) -> str:
    """Crude da/en detection via Danish stopwords and characters."""
    sample = text[:4000].lower()
    words = re.findall(r"[a-zæøå]+", sample)
    if not words:
        return "en"
    danish_hits = sum(1 for w in words if w in DANISH_MARKERS)
    special = sum(sample.count(c) for c in "æøå")
    ratio = (danish_hits + special) / max(len(words), 1)
    return "da" if ratio > 0.08 else "en"
