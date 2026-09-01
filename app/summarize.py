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
# Naming what is in a photo is the one job where the cheap model is
# measurably weaker: on the same portrait, haiku-4.5 returned "a woman in
# professional dark business attire ... in front of a red and white flag"
# where opus named Mette Frederiksen. Landmarks it gets right either way.
# Defaults to LLM_MODEL — changing it is a real cost decision, so it is a
# switch rather than an upgrade.
VISION_MODEL = os.environ.get("VISION_MODEL", "") or LLM_MODEL

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


async def _llm_via_shim(prompt: str, model: str, tools: list[str],
                        thinking: bool) -> tuple[str, dict]:
    async with httpx.AsyncClient(timeout=650) as client:
        resp = await client.post(
            f"{LLM_URL}/v1/complete",
            json={"prompt": prompt, "model": model,
                  "allowed_tools": tools, "thinking": thinking},
        )
        resp.raise_for_status()
        payload = resp.json()
        text = payload.get("text", "").strip()
    if not text:
        raise RuntimeError("LLM shim returned empty text")
    # None means "the shim could not tell us" (a tool-less call); [] means it
    # told us the model searched for nothing. The distinction is the whole point.
    return text, {"searches": payload.get("searches")}


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
    # wait_for cancels the wait, not the child: without the kill a timed-out
    # `claude` keeps running on the host and every later timeout leaks another.
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0 or not stdout.strip():
        raise RuntimeError(f"claude CLI failed: {stderr.decode()[-300:]}")
    return stdout.decode().strip()


async def llm_with_meta(prompt: str, model: str = "", tools: list[str] | None = None,
                        thinking: bool = False) -> tuple[str, dict]:
    """Like llm(), but also returns what the backend can tell us about the call.

    Currently that is {"searches": [...] | None} — the web searches the model
    actually ran. Only the shim can observe them; the CLI fallback reports None,
    meaning unknown rather than none.
    """
    model = model or LLM_MODEL
    tools = tools or []
    async with _llm_lock:
        if LLM_URL:
            try:
                return await _llm_via_shim(prompt, model, tools, thinking)
            except Exception as exc:
                log.warning("LLM shim failed (%s), trying CLI", exc)
        if shutil.which("claude"):
            return await _llm_via_cli(prompt, model, tools, thinking), {"searches": None}
    raise RuntimeError("no LLM backend available")


async def llm(prompt: str, model: str = "", tools: list[str] | None = None,
              thinking: bool = False) -> str:
    """Raises if no LLM backend is available/working."""
    text, _ = await llm_with_meta(prompt, model, tools, thinking)
    return text


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
    text = re.sub(r"~~+", "", text)                  # strikethrough markers only:
    # a lone "~" is an approximation ("~$5B", "~50 mio."), and stripping it
    # turns a rounded figure into an exact claim.
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
    r"i(’|')?ll need (access|permission)|unable to (fetch|access|retrieve)|"
    # Harness self-talk: the CLI answering about its own skills/config instead
    # of doing the job (ep. 337 — a book-brief prompt tripped the brainstorming
    # skill and the shim narrated Claude discussing that skill). The shim now
    # isolates the CLI; this is the second line of defence. Phrase-level on
    # purpose — a bare "skill"/"tool" appears in legitimate article prose.
    r"i(’|')?ve loaded|i have loaded|loaded the .{0,30}skill|"
    r"(this|the) (skill|slash command|subagent) (is|was) (designed|meant|intended)|"
    r"invoking [a-z-]{3,30} (skill|plan)|"
    r"my (instructions|configuration|system prompt)\b)",
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
  "messages"/"text". For charts/graphs/tables, EXPLAIN the result the way you'd tell
  a friend: lead with the takeaway, then give the key figures ROUNDED (e.g. "about
  two-thirds", "roughly one in five") and the main comparison or ratio — do NOT
  recite every cell, category, or exact decimal. Two or three sentences is fine here.
IDENTIFY WHAT YOU CAN. "Mette Frederiksen speaking outside Christiansborg" tells a
listener far more than "a politician outside a building", so name the well-known
people, organisations, landmarks and places you genuinely recognise, and put those
names in "description". Recognition only — never infer a name from a logo, a jersey,
a caption, a setting or who the article is probably about. If you are not certain,
describe the person or place generically instead and say nothing about identity; an
unnamed description is always better than a wrong name. Do not guess at private
individuals at all.
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
            "--model", VISION_MODEL, "--allowedTools", "Read",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
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
        # Same lock as llm(): vision goes through the same one-subprocess-per-
        # request shim, so unserialized image calls exhaust exactly what the
        # lock was added to protect.
        async with _llm_lock:
            if LLM_URL:
                async with httpx.AsyncClient(timeout=300) as client:
                    resp = await client.post(f"{LLM_URL}/v1/vision", json={
                        "prompt": prompt,
                        "image_b64": base64.b64encode(image).decode(),
                        "mime": "image/jpeg",
                        "model": VISION_MODEL,
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


# ── Attribution guard for researched digests ─────────────────────────────
# The digest prompt asks the model to research an item up to length. Told to do
# that, it will attribute what it finds to nobody in particular: ep. 572
# (2026-08-18) grew 168 characters of feed text into four hard statistics —
# "eighty-seven percent of security professionals…" — sourced entirely to
# "According to recent reporting". That phrasing satisfies "attribute anything
# you did find" while being unfalsifiable, so the rule has to name the failure.
#
# Deliberately a CLOSED list of vague heads. "According to OpenAI" and
# "according to the Financial Times" are exactly what we want and must never
# match — see the negative tests in tests/test_summarize.py.
_VAGUE_HEAD = (
    r"(?:recent |new |one |a |some |several |various |industry |the )*"
    r"(?:reporting|reports|coverage|sources|analysts|experts|observers|"
    r"researchers|commentators|studies|study|research|data|surveys|survey|"
    r"estimates|reviewers)"
)
_VAGUE_ATTRIBUTION_RE = re.compile(
    r"(?:"
    rf"\b(?:according to|per|citing|based on)\s+{_VAGUE_HEAD}\b"
    r"|\b(?:reportedly|allegedly|supposedly)\b"
    r"|\b(?:reports?|studies|research|data|surveys?|experts?|analysts?|sources?)\s+"
    r"(?:show|shows|suggest|suggests|indicate|indicates|say|says|found|find)\b"
    r"|\bit (?:has been|is|was) (?:widely )?(?:reported|estimated|claimed)\b"
    r"|\bwidely reported\b"
    # Danish equivalents — the digest format is language-agnostic.
    r"|\bifølge\s+(?:nylige\s+|nye\s+|en\s+|nogle\s+)*"
    r"(?:rapporter|kilder|eksperter|analytikere|forskere|undersøgelser|undersøgelse|data)\b"
    r"|\bangiveligt\b"
    r"|\b(?:rapporter|undersøgelser|eksperter|analytikere|kilder)\s+"
    r"(?:viser|tyder|siger|angiver)\b"
    r")",
    re.I,
)

# Coarse on purpose: this only decides whether to RECORD that a researched-
# sounding figure appeared, never whether to change the script.
_FIGURE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|procent|million|billion|milliard|millioner|milliarder)\b"
    r"|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred)[- ]?(?:\w+[- ]?)?\s*"
    r"(?:percent|procent|million|billion)\b",
    re.I,
)


def vague_attributions(script: str) -> list[str]:
    """Phrases that credit a claim to nobody checkable. Empty list is the pass."""
    return [m.group(0) for m in _VAGUE_ATTRIBUTION_RE.finditer(script)]


def has_figures(script: str) -> bool:
    """True when the script states percentages or magnitudes out loud."""
    return bool(_FIGURE_RE.search(script))


async def _repair_vague_attribution(script: str, language: str) -> str:
    """One corrective pass over unnamed attributions; the script is kept as-is
    if the repair fails or comes back mangled.

    A prompt rule alone was not enough — ep. 572 broke the previous one — so the
    output is checked and, when it offends, sent back with the offending phrases
    quoted at it. Anything still vague after this is recorded in provenance.
    """
    offenders = vague_attributions(script)
    if not offenders:
        return script
    lang_name = "Danish" if language == "da" else "English"
    quoted = ", ".join(f'"{o}"' for o in dict.fromkeys(offenders))
    prompt = (
        "The podcast script below credits claims to nobody checkable. These phrases "
        f"are the problem: {quoted}. For each one, either name the specific "
        "organisation, publication or document the claim came from, or delete the "
        "claim entirely — deleting is the right call whenever you cannot name a "
        "source, and a slightly shorter script is fine. Change nothing else: keep "
        "every other sentence exactly as written. Never comment on what you changed. "
        f"The script is in {lang_name}; reply in {lang_name} with ONLY the script.\n\n"
        f"Script:\n{script}"
    )
    try:
        fixed = (await llm(prompt)).strip()
    except Exception as exc:
        log.warning("attribution repair unavailable (%s); keeping original", exc)
        return script
    if looks_meta(fixed) or len(fixed) < len(script) * 0.5:
        # A repair that eats half the episode is a failed repair, not a strict one.
        log.warning("attribution repair returned %d chars for a %d-char script; keeping original",
                    len(fixed), len(script))
        return script
    remaining = vague_attributions(fixed)
    log.info("attribution repair: %d vague phrase(s) -> %d", len(offenders), len(remaining))
    return fixed


def _research_provenance(script: str, searches: list[dict] | None,
                         items: list[dict]) -> dict:
    """Record what the research actually consisted of, so a later reader can
    tell a researched digest from a fluent one.

    `searches` is None when the backend could not observe tool use (the CLI
    fallback) and [] when it observed the model searching for nothing.
    """
    prov: dict = {
        "input_chars": sum(len(strip_html(i.get("summary", ""))) + len(i.get("title", ""))
                           for i in items),
        "script_chars": len(script),
        "vague_attribution": len(vague_attributions(script)),
    }
    if searches is None:
        prov["searches"] = None
        return prov
    prov["searches"] = len(searches)
    prov["search_queries"] = [s.get("query", "") for s in searches][:10]
    urls: list[str] = []
    for s_ in searches:
        for u in s_.get("urls", []):
            if u not in urls:
                urls.append(u)
    prov["search_urls"] = urls[:20]
    # The signature of the failure this whole change exists to catch: hard
    # numbers in the script, nothing looked up to get them.
    if not searches and has_figures(script):
        log.warning("digest states figures but ran no searches (%d chars in, %d out)",
                    prov["input_chars"], len(script))
        prov["unsourced_figures"] = True
    return prov


async def digest_script(source_name: str, date_str: str, items: list[dict],
                        language: str, window: str = "since the last edition",
                        ) -> tuple[str, dict]:
    """Returns (script, provenance-fragment).

    `window` is the period the digest actually covers. Without it the model
    guessed, and a daily digest opened "This week, we're looking at…" and closed
    "That's this week's digest" (ep. 443 feedback).
    """
    lang_name = "Danish" if language == "da" else "English"
    bulletin = "\n\n".join(
        f"### {i['title']}\n{strip_html(i.get('summary', ''))[:1500]}" for i in items
    )
    prompt = (
        f"Write a spoken news digest script in {lang_name} for a podcast episode called "
        f"'{source_name}' dated {date_str}. This edition covers {window} — say so if you "
        "refer to the period at all, and never describe it as any other span of time. "
        "Don't just read the announcements back: rephrase "
        "them so the listener gets the overview first, then the perspective — what is new, "
        "why it matters, how items relate, and a calibrated sense of how significant each is. "
        "Group related items; drop pure marketing fluff. "
        # Some days these feeds carry one item and the episode came out at 66
        # seconds. Research is what fills the time; padding the same thin facts
        # with adjectives would be worse than a short episode.
        "Aim for three to five minutes of speech (roughly 450 to 750 words). Use web search "
        "to earn that length: what led up to each item, how it was received, who it affects, "
        "what comparable efforts exist. Add substance, never filler — if the material is "
        "genuinely thin after searching, write a shorter script rather than padding it. "
        # Told to reach a word count, the model will otherwise manufacture
        # specifics that sound like reporting: a dry run invented "after
        # watching thousands of Claude Code sessions" out of a two-line summary.
        "State only what the items or your search results actually support. Never invent "
        "quotes, figures, dates, internal details or claims about what a company observed "
        "or intended. "
        # "attribute anything you did find to where it came from" was the old rule
        # and it was met by "According to recent reporting" in front of four
        # invented-looking statistics (ep. 572). Naming the failure is the fix.
        "Every figure, statistic or quoted claim must name the specific organisation, "
        "publication or document it came from, in the sentence that states it. Vague "
        "attribution is forbidden: never write 'according to recent reporting', 'reports "
        "suggest', 'studies show', 'experts say', 'reportedly' or anything like them. If "
        "you cannot name the source, leave the figure out — a shorter script is fine. "
        "Plain text only — no markdown, no "
        "headings, no stage directions, no URLs (say 'the link is in the show notes' if needed); "
        "the text is fed directly to text-to-speech. Start with a one-sentence intro, end with "
        "a one-sentence sign-off. Reply with ONLY the script itself — no framing before or "
        f"after.\n\nItems:\n{bulletin}"
    )
    try:
        raw, meta = await llm_with_meta(prompt, tools=["WebSearch"])
        raw = await _repair_vague_attribution(raw, language)
        script, scrub = await scrub_script(raw, language)
        if looks_meta(script) or len(script) < 200:
            raise RuntimeError("digest output invalid (meta or too short)")
        prov = {"generator": "llm", "model": LLM_MODEL, "scrub": scrub}
        prov.update(_research_provenance(script, meta.get("searches"), items))
        return script, prov
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
