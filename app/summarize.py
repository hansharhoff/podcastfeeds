"""LLM-backed (or extractive-fallback) script writing for digests and articles.

LLM access order:
  1. LLM_URL       — HTTP shim on the docker host that wraps the local `claude`
                     CLI (scripts/llm_shim.py). Uses the Claude subscription.
  2. claude CLI    — direct subprocess, when running outside Docker.
  3. extractive    — no LLM: headlines + article leads. Never blocks an episode.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil

import httpx

from .extract import strip_html

log = logging.getLogger("podcastfeeds")

LLM_URL = os.environ.get("LLM_URL", "").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")

# The shim runs one `claude` subprocess per request; concurrent heavy calls
# (several episodes summarizing / writing Danish segments at once) can exhaust
# it and fail with "no LLM backend". Serialize all LLM work through one lock.
_llm_lock = asyncio.Lock()

DA_MONTHS = ["januar", "februar", "marts", "april", "maj", "juni", "juli",
             "august", "september", "oktober", "november", "december"]

SHOWNOTES_DELIM = "---SHOWNOTES---"


def spoken_date(dt, language: str) -> str:
    if language == "da":
        return f"{dt.day}. {DA_MONTHS[dt.month - 1]} {dt.year}"
    return dt.strftime("%B %-d, %Y")


async def _llm_via_shim(prompt: str, model: str, tools: list[str], thinking: bool) -> str:
    async with httpx.AsyncClient(timeout=650) as client:
        resp = await client.post(
            f"{LLM_URL}/v1/complete",
            json={"prompt": prompt, "model": model,
                  "allowed_tools": tools, "thinking": thinking},
        )
        resp.raise_for_status()
        text = resp.json().get("text", "").strip()
    if not text:
        raise RuntimeError("LLM shim returned empty text")
    return text


async def _llm_via_cli(prompt: str, model: str, tools: list[str], thinking: bool) -> str:
    cmd = ["claude", "-p", prompt, "--model", model]
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    env = dict(os.environ)
    if thinking:
        env["MAX_THINKING_TOKENS"] = "10000"
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    if proc.returncode != 0 or not stdout.strip():
        raise RuntimeError(f"claude CLI failed: {stderr.decode()[-300:]}")
    return stdout.decode().strip()


async def llm(prompt: str, model: str = "", tools: list[str] | None = None,
              thinking: bool = False) -> str:
    """Raises if no LLM backend is available/working."""
    model = model or LLM_MODEL
    tools = tools or []
    async with _llm_lock:
        if LLM_URL:
            try:
                return await _llm_via_shim(prompt, model, tools, thinking)
            except Exception as exc:
                log.warning("LLM shim failed (%s), trying CLI", exc)
        if shutil.which("claude"):
            return await _llm_via_cli(prompt, model, tools, thinking)
    raise RuntimeError("no LLM backend available")


# ── Script scrubbing (TTS-awareness) ─────────────────────────────────────
# Everything in a script is read aloud verbatim by edge-tts, which understands
# plain prose only. Two passes: cheap regexes always, then an LLM editor pass
# for LLM-generated scripts (they occasionally include assistant framing like
# "Here is a spoken digest script:", markdown, or stage directions).

_PREAMBLE_RE = re.compile(
    r"^(here('|’)?s?( is)?\b|sure[,.!]|certainly[,.!]|of course[,.!]|below is\b|"
    r"i('|’)?(ve| have)? (written|created|prepared)\b).{0,120}[:.]?\s*$",
    re.I,
)
_TRAILER_RE = re.compile(
    r"^(i hope (this|that)|let me know\b|feel free\b|\(?note[:s]\b).*$", re.I
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^)]+)\)")
_URL_RE = re.compile(r"\(?(?:https?://|www\.)\S+\)?")


def _spoken_domain(match: re.Match) -> str:
    from urllib.parse import urlparse

    raw = match.group(0).strip("()")
    host = urlparse(raw if raw.startswith("http") else f"https://{raw}").netloc
    host = host.removeprefix("www.")
    # Short domains read fine aloud; long paths never do.
    return host if 0 < len(host) <= 25 else ""


_FOOTNOTE_RE = re.compile(r"\[\d{1,3}\]")  # inline footnote markers: [1], [12]

# A markdown table separator row, e.g. "---|---|---" or "| :--- | ---: |".
_TABLE_SEP_RE = re.compile(r"^[\s|:.-]*-{2,}[\s|:.-]*$")


def _is_table_row(line: str) -> bool:
    return line.count("|") >= 2


def has_markdown_table(text: str) -> bool:
    """True if the text contains a pipe/markdown table (header + separator row)."""
    lines = text.splitlines()
    return any(
        _is_table_row(lines[i]) and i + 1 < len(lines)
        and "|" in lines[i + 1] and _TABLE_SEP_RE.match(lines[i + 1])
        for i in range(len(lines))
    )


def linearize_markdown_tables(text: str) -> str:
    """Rewrite markdown/pipe tables as spoken prose so TTS never reads pipes and
    dashes aloud. Each data row becomes 'Header: value; Header: value.' using the
    table's own header cells. Non-table text is returned unchanged."""
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if (_is_table_row(lines[i]) and i + 1 < n
                and "|" in lines[i + 1] and _TABLE_SEP_RE.match(lines[i + 1])):
            headers = [c.strip() for c in lines[i].split("|")]
            i += 2  # consume header + separator row
            sentences: list[str] = []
            while i < n and _is_table_row(lines[i]):
                cells = [c.strip() for c in lines[i].split("|")]
                parts = [
                    (f"{h}: {c}" if h and h != c else c)
                    for h, c in zip(headers, cells, strict=False)  # rows may be ragged
                    if c
                ]
                if parts:
                    sentences.append("; ".join(parts) + ".")
                i += 1
            out.append(" ".join(sentences))
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def scrub_light(text: str) -> str:
    """URL/markdown cleanup safe for article prose (no framing heuristics)."""
    text = linearize_markdown_tables(text)           # pipe tables -> spoken prose
    text = _MD_LINK_RE.sub(r"\1", text)              # [text](url) -> text
    text = _URL_RE.sub(_spoken_domain, text)         # bare URLs -> domain or gone
    text = re.sub(r"[*_`#]+", "", text)              # markdown emphasis/headers
    text = _FOOTNOTE_RE.sub("", text)                # inline footnote markers [1] -> gone
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# ── Substack CTA / widget cruft ──────────────────────────────────────────
# Standalone footer/widget lines (subscribe buttons, share prompts) get read
# aloud verbatim. Drop SHORT segments that are essentially just the CTA, so a
# real paragraph that happens to mention "subscribe" is kept.
_CRUFT_PHRASES = (
    "subscribe now", "share this post", "share", "leave a comment",
    "give a gift subscription", "thanks for reading", "this post is public",
    "read more", "no posts", "continue reading",
)
# Match at the start of a short segment, on a word boundary so the bare word
# "share" matches "Share this post" but not "shareholders" / "shares".
_CRUFT_RE = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in _CRUFT_PHRASES) + r")\b",
    re.I,
)


def is_cruft_line(text: str) -> bool:
    """True for a short standalone Substack CTA/widget line that shouldn't be
    narrated. Long paragraphs (>=200 chars) are never treated as cruft, so a
    real paragraph that merely mentions e.g. 'subscribe' is kept."""
    stripped = text.strip()
    if not stripped or len(stripped) >= 200:
        return False
    return bool(_CRUFT_RE.match(stripped))


def scrub_regex(script: str) -> str:
    """Deterministic cleanup: markdown, raw URLs, obvious assistant framing."""
    script = scrub_light(script)
    script = re.sub(r"^\s*[-•]\s+", "", script, flags=re.M)  # bullet markers
    lines = [ln.rstrip() for ln in script.split("\n")]
    while lines and (not lines[0].strip() or _PREAMBLE_RE.match(lines[0].strip())):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _TRAILER_RE.match(lines[-1].strip())):
        lines.pop()
    out = "\n".join(lines)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# The editor/summarizer talking ABOUT the input/tooling instead of returning a
# script (assistant refusals, tool-permission requests, meta-commentary):
_META_RE = re.compile(
    r"(the (text|script|content|input) (you )?(provided|given|shared)|"
    r"no actual (podcast )?script|there is no (script|content)|as an ai\b|"
    r"i cannot|i(’|')?m unable|i am unable|composed of content|"
    r"i need permission|grant .{0,20}permission|webfetch|allowedtools|"
    r"interactive claude code|could you (either|please )?(grant|provide)|"
    r"i(’|')?ll need (access|permission)|unable to (fetch|access|retrieve))",
    re.I,
)


def looks_meta(text: str) -> bool:
    """True when LLM output is commentary about the task rather than a script."""
    return bool(_META_RE.search(text[:400]))


async def scrub_script(script: str, language: str) -> tuple[str, str]:
    """Full scrub for LLM-generated scripts: regex pass + LLM editor pass.
    Returns (clean_script, method) where method is 'regex' or 'regex+llm'."""
    script = scrub_regex(script)
    lang_name = "Danish" if language == "da" else "English"
    prompt = (
        "You are the final editor for a text-to-speech podcast script. The text below "
        "will be read aloud VERBATIM by a TTS voice. Remove anything that should not "
        "be spoken: assistant preambles or framing (e.g. 'Here is the script'), "
        "markdown or formatting syntax, headings, stage directions, editorial notes, "
        "model self-references, and raw URLs (rewrite naturally, e.g. 'the link is in "
        "the show notes', or just the domain name). Fix anything a TTS voice would "
        "stumble on. Do NOT shorten, summarize, or rephrase legitimate content — "
        "only remove/repair. Never comment on the text or explain what you did: if "
        "nothing needs fixing return it unchanged, and if there is no legitimate "
        f"script content at all reply with an empty response. The script is in "
        f"{lang_name}; reply in {lang_name} with ONLY the cleaned script text.\n\n"
        f"Script:\n{script}"
    )
    try:
        cleaned = scrub_regex(await llm(prompt))
        if looks_meta(cleaned):
            log.warning("scrub: LLM editor returned meta-commentary; keeping regex-only")
        elif len(cleaned) >= len(script) * 0.6:
            return cleaned, "regex+llm"
        else:
            log.warning("scrub: LLM pass shrank script %d -> %d chars; keeping regex-only",
                        len(script), len(cleaned))
    except Exception as exc:
        log.warning("scrub: LLM pass unavailable (%s)", exc)
    return script, "regex"


# ── Entry classification (llm_filter) ────────────────────────────────────

async def matches_criteria(title: str, summary: str, criteria: str) -> bool | None:
    """LLM yes/no: does this feed entry match the source's criteria?
    Returns None when no LLM is available (caller decides the default)."""
    import json

    prompt = (
        "Decide whether this feed entry matches the criteria. Reply with ONLY a "
        'JSON object: {"match": true/false, "reason": "few words"}\n\n'
        f"Criteria: {criteria}\n\nEntry title: {title}\nEntry summary: {strip_html(summary)[:800]}"
    )
    try:
        text = await llm(prompt)
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1])
        return bool(data.get("match"))
    except Exception as exc:
        log.warning("llm_filter classification failed: %s", exc)
        return None


# ── Image understanding ──────────────────────────────────────────────────

VISION_PROMPT = """Analyze this image from an article. Reply with ONLY a JSON object, no markdown fence:
{{
  "kind": "conversation" | "text" | "image",
  "description": "1-2 sentences describing the image for a podcast listener, in {lang_name}",
  "messages": [{{"speaker": "name as shown", "text": "the post text verbatim"}}],
  "text": "verbatim transcription of the text in the image, original language"
}}
- kind "conversation": a back-and-forth of social-media posts, a thread, or a chat
  (multiple tweets/X posts, text messages, forum replies) — fill "messages" in order.
- kind "text": the image is essentially a block of readable PROSE (a screenshot of
  an article excerpt, a note, a single post/tweet, a quoted passage) — put the full
  verbatim text in "text". This is for when the point of the screenshot IS its words.
- kind "image": a photo, chart, diagram, figure, OR A TABLE of data — omit
  "messages"/"text". For charts/graphs/tables the description must state the main
  takeaway and the key figures in plain spoken prose, not just the axes.
NEVER use a markdown table, pipes (|), or column layout in any field — this is read
aloud, so write every number and comparison as a spoken sentence. A screenshot of a
data table is kind "image" (prose takeaway in "description"), NOT kind "text"."""


async def _vision_via_cli(prompt: str, image: bytes) -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(image)
        path = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", f"First use the Read tool on the image file {path}, then:\n{prompt}",
            "--model", LLM_MODEL, "--allowedTools", "Read",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    finally:
        os.unlink(path)
    if proc.returncode != 0 or not stdout.strip():
        raise RuntimeError(f"claude CLI vision failed: {stderr.decode()[-300:]}")
    return stdout.decode().strip()


async def vision_analyze(image: bytes, language: str) -> dict | None:
    """Describe an image; transcribe screenshotted conversations.
    Returns {"kind", "description", "messages"} or None when no LLM/parse."""
    import base64
    import json

    lang_name = "Danish" if language == "da" else "English"
    prompt = VISION_PROMPT.format(lang_name=lang_name)
    try:
        if LLM_URL:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(f"{LLM_URL}/v1/vision", json={
                    "prompt": prompt,
                    "image_b64": base64.b64encode(image).decode(),
                    "mime": "image/jpeg",
                })
                resp.raise_for_status()
                text = resp.json().get("text", "")
        elif shutil.which("claude"):
            text = await _vision_via_cli(prompt, image)
        else:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        data = json.loads(text[start:end + 1])
        if not isinstance(data, dict) or not data.get("description"):
            return None
        if data.get("kind") == "conversation" and not data.get("messages"):
            data["kind"] = "image"
        if data.get("kind") == "text" and not (data.get("text") or "").strip():
            data["kind"] = "image"
        return data
    except Exception as exc:
        log.warning("vision analysis failed: %s", exc)
        return None


# ── Danish perspective segment ───────────────────────────────────────────

DANISH_PERSPECTIVE_MODEL = os.environ.get("DK_MODEL", "claude-opus-4-8")


async def danish_perspective(title: str, body: str, language: str) -> tuple[str, dict]:
    """1-2 minute 'view from Denmark' segment for a blog-post episode:
    is this a US-only issue, and what does the Danish data/situation say?
    Returns (segment_text, provenance-fragment); raises on failure/meta."""
    lang_name = "Danish" if language == "da" else "English"
    opening = "Set fra Danmark." if language == "da" else "And now, the view from Denmark."
    prompt = (
        f"You are a segment writer for a podcast episode narrating the blog post below. "
        f"Write a short closing segment in {lang_name} (150-250 spoken words, about 1-2 "
        "minutes) giving the DANISH perspective on the post's core topic: what is the "
        "relevant situation, data, or policy in Denmark, and — explicitly — whether this "
        "issue is mostly US-specific or applies in Denmark too. Use web search to check "
        "or fetch current Danish figures where they strengthen the segment (Danmarks "
        "Statistik, ministry data, recent coverage); prefer a verified number over a "
        "remembered one, and where you can't verify, say so plainly rather than "
        "inventing numbers. Plain text read aloud verbatim by TTS: no markdown, no "
        "headings, no URLs, no citation brackets, no framing before or after. Begin "
        f"with exactly: '{opening}'\n\n"
        f"Blog post: {title}\n\n{body[:16000]}"
    )
    raw = await llm(prompt, model=DANISH_PERSPECTIVE_MODEL,
                    tools=["WebSearch"], thinking=True)
    segment, scrub = await scrub_script(raw, language)
    if looks_meta(segment) or not (400 <= len(segment) <= 2600):
        raise RuntimeError(f"danish perspective invalid ({len(segment)} chars)")
    return segment, {"dk_model": DANISH_PERSPECTIVE_MODEL, "dk_scrub": scrub}


# ── Digests ──────────────────────────────────────────────────────────────

def _extractive_digest(source_name: str, date_str: str, items: list[dict],
                       language: str) -> str:
    if language == "da":
        intro = f"{source_name}, {date_str}. Her er overblikket."
        outro = "Det var alt for denne gang."
    else:
        intro = f"{source_name}, {date_str}. Here are the latest items."
        outro = "That's all for this update."
    parts = [intro]
    for item in items:
        summary = strip_html(item.get("summary", ""))
        if len(summary) > 600:
            summary = summary[:600].rsplit(".", 1)[0] + "."
        parts.append(f"{item['title']}.\n{summary}")
    parts.append(outro)
    return "\n\n".join(parts)


async def digest_script(source_name: str, date_str: str, items: list[dict],
                        language: str) -> tuple[str, dict]:
    """Returns (script, provenance-fragment)."""
    lang_name = "Danish" if language == "da" else "English"
    bulletin = "\n\n".join(
        f"### {i['title']}\n{strip_html(i.get('summary', ''))[:1500]}" for i in items
    )
    prompt = (
        f"Write a spoken news digest script in {lang_name} for a podcast episode called "
        f"'{source_name}' dated {date_str}. Don't just read the announcements back: rephrase "
        "them so the listener gets the overview first, then the perspective — what is new, "
        "why it matters, how items relate, and a calibrated sense of how significant each is. "
        "Group related items; drop pure marketing fluff. Plain text only — no markdown, no "
        "headings, no stage directions, no URLs (say 'the link is in the show notes' if needed); "
        "the text is fed directly to text-to-speech. Start with a one-sentence intro, end with "
        "a one-sentence sign-off. Reply with ONLY the script itself — no framing before or "
        f"after.\n\nItems:\n{bulletin}"
    )
    try:
        raw = await llm(prompt)
        script, scrub = await scrub_script(raw, language)
        if looks_meta(script) or len(script) < 200:
            raise RuntimeError("digest output invalid (meta or too short)")
        return script, {"generator": "llm", "model": LLM_MODEL, "scrub": scrub}
    except Exception as exc:
        log.warning("digest LLM unavailable/invalid (%s); using extractive fallback", exc)
        script = scrub_regex(_extractive_digest(source_name, date_str, items, language))
        return script, {"generator": "extractive", "scrub": "regex"}


# ── Single articles (narrate_mode: summary) ─────────────────────────────

async def article_summary(title: str, body: str, language: str,
                          link: str = "") -> tuple[str, str, dict]:
    """Return (narration_script, show_notes, provenance-fragment) for one article.

    Used for sources like Home Assistant release notes where reading the full
    text (changelogs!) aloud would be unbearable.
    """
    lang_name = "Danish" if language == "da" else "English"
    prompt = (
        f"You get the text of an announcement/release-notes post titled '{title}'. "
        f"Produce two things in {lang_name}, separated by a line containing exactly "
        f"{SHOWNOTES_DELIM}\n"
        "1) A spoken narration script (2-5 minutes): the overview first, then the "
        "highlights that actually matter to a technical listener, with perspective on "
        "why they matter. Plain text for text-to-speech: no markdown, no headings, "
        "no URLs, no framing before or after — the text is read aloud verbatim.\n"
        "2) Show notes: a compact bullet list of the key points (plain text bullets "
        "using •), suitable for a podcast episode description.\n\n"
        f"Text:\n{body[:24000]}"
    )
    try:
        result = await llm(prompt)
        if SHOWNOTES_DELIM in result:
            narration, notes = result.split(SHOWNOTES_DELIM, 1)
        else:
            narration, notes = result, ""
        narration, scrub = await scrub_script(narration.strip(), language)
        if looks_meta(narration) or len(narration) < 200:
            raise RuntimeError(
                f"summary output invalid (meta={looks_meta(narration)}, "
                f"{len(narration)} chars)"
            )
        notes = notes.strip()
        if not notes:
            notes = narration[:800]
        prov = {"generator": "llm", "model": LLM_MODEL, "scrub": scrub}
    except Exception as exc:
        log.warning("article LLM unavailable (%s); using lead extraction", exc)
        narration = body[:2500]
        if "." in narration[500:]:
            narration = narration[: narration.rindex(".") + 1]
        narration = scrub_light(narration)
        notes = body[:800]
        prov = {"generator": "lead-extract", "scrub": "light"}
    if link:
        notes = f"{notes}\n\nOriginal: {link}"
    return narration, notes, prov
