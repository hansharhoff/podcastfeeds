"""Fetch pages and extract readable article text."""
from __future__ import annotations

import html
import io
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

    def walk(el):
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            if not tag:
                continue
            if tag in HEADINGS:
                t = child.text_content().strip()
                if t:
                    segments.append({"type": "heading", "text": t})
            elif tag == "blockquote":
                t = child.text_content().strip()
                if t:
                    segments.append({"type": "quote", "text": t})
            elif is_image_block(child):
                src = _img_src(child)
                cap_el = child.find(".//figcaption")
                caption = cap_el.text_content().strip() if cap_el is not None else ""
                if src.startswith("http"):
                    segments.append({"type": "image", "src": src, "caption": caption})
            elif tag == "p":
                # A paragraph may embed an inline image (rare) — capture text then it.
                t = child.text_content().strip()
                if t:
                    segments.append({"type": "text", "text": t})
                for img in child.iter("img"):
                    if img.get("src", "").startswith("http"):
                        segments.append({"type": "image", "src": img.get("src"), "caption": ""})
            elif tag in ("ul", "ol"):
                for li in child.iter("li"):
                    t = li.text_content().strip()
                    if t:
                        segments.append({"type": "text", "text": t})
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
