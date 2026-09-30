"""Audio courses and audiobooks requested from a Claude Code session.

The audio-course skill (hansharhoff-skills) writes each part's spoken script,
or picks a chapter URL, and POSTs the approved course to /api/course. This
module validates that request, creates one learning episode per part, and
renders the parts in order through process_episode. Course metadata rides in
Episode.provenance under "course", so a redo months later still knows it is
part 3 of 5.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from . import db
from .config import SourceDef, load_config
from .db import Episode, utcnow

MAX_PARTS = {"course": 8, "audiobook": 60}
MAX_SCRIPT_CHARS = 60000  # the same bound RSS source_text uses
MIN_SCRIPT_CHARS = 200    # shorter than this is not a part, it is a mistake
MAX_NOTES_CHARS = 20000
_COURSE_ID_RE = re.compile(r"^[a-z0-9-]{1,80}$")


class CourseError(ValueError):
    """A request /api/course rejects with 400; the message says why."""


@dataclass
class CoursePart:
    part: int
    title: str
    script: str = ""
    url: str = ""
    verbatim: bool = False
    show_notes_md: str = ""


@dataclass
class CourseRequest:
    course_id: str
    title: str
    kind: str
    language: str
    parts: list[CoursePart]


def parse_course(data: object) -> CourseRequest:
    if not isinstance(data, dict):
        raise CourseError("body must be a JSON object")
    course_id = data.get("course_id")
    if not isinstance(course_id, str) or not _COURSE_ID_RE.match(course_id):
        raise CourseError("course_id must be 1-80 chars of a-z, 0-9 and -")
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise CourseError("title is required (max 200 chars)")
    kind = data.get("kind", "course")
    if kind not in MAX_PARTS:
        raise CourseError("kind must be 'course' or 'audiobook'")
    language = data.get("language")
    if language not in ("da", "en"):
        raise CourseError("language must be 'da' or 'en'")
    raw = data.get("episodes")
    if not isinstance(raw, list) or not raw:
        raise CourseError("episodes must be a non-empty list")
    if len(raw) > MAX_PARTS[kind]:
        raise CourseError(f"a {kind} has at most {MAX_PARTS[kind]} parts, got {len(raw)}")
    parts = sorted((_parse_part(i, e) for i, e in enumerate(raw)), key=lambda p: p.part)
    if [p.part for p in parts] != list(range(1, len(parts) + 1)):
        raise CourseError("parts must be numbered 1..N with no gaps or duplicates")
    return CourseRequest(course_id, title.strip(), kind, language, parts)


def _parse_part(i: int, e: object) -> CoursePart:
    where = f"episodes[{i}]"
    if not isinstance(e, dict):
        raise CourseError(f"{where} must be an object")
    part = e.get("part")
    if not isinstance(part, int) or isinstance(part, bool):
        raise CourseError(f"{where}.part must be an integer")
    title = e.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        raise CourseError(f"{where}.title is required (max 200 chars)")
    script = e.get("script") or ""
    url = e.get("url") or ""
    notes = e.get("show_notes_md") or ""
    if not all(isinstance(v, str) for v in (script, url, notes)):
        raise CourseError(f"{where}: script, url and show_notes_md must be strings")
    if bool(script.strip()) == bool(url.strip()):
        raise CourseError(f"{where} needs exactly one of script or url")
    if script and not MIN_SCRIPT_CHARS <= len(script.strip()) <= MAX_SCRIPT_CHARS:
        raise CourseError(
            f"{where}.script must be {MIN_SCRIPT_CHARS}-{MAX_SCRIPT_CHARS} chars")
    if url and not url.strip().startswith(("http://", "https://")):
        raise CourseError(f"{where}.url must be http(s)")
    if len(notes) > MAX_NOTES_CHARS:
        raise CourseError(f"{where}.show_notes_md is over {MAX_NOTES_CHARS} chars")
    return CoursePart(part, title.strip(), script.strip(), url.strip(),
                      bool(e.get("verbatim")), notes)


def learning_source() -> SourceDef:
    src = next(s for s in load_config().sources if s.type == "learning")
    # A chapter can be a PDF. The course was approved in the session, which is
    # exactly the deliberate request allow_pdf exists to permit.
    return SourceDef(**{**src.__dict__, "allow_pdf": True})


def episode_title(meta: dict) -> str:
    return f"{meta['course_title']} ({meta['part']}/{meta['parts']}): {meta['part_title']}"


def course_meta(provenance: str) -> dict | None:
    try:
        data = json.loads(provenance or "{}")
    except ValueError:
        return None
    meta = data.get("course") if isinstance(data, dict) else None
    return meta if isinstance(meta, dict) else None


def create_course(req: CourseRequest) -> list[int]:
    """One learning episode per part, in part order. A part that already
    exists (same course_id and part) is left untouched, so resubmitting a
    course only fills the gaps. Returns every part's episode id, in order.

    published_at is fixed here, one second apart, so podcast apps sort the
    parts correctly no matter which finishes rendering first."""
    slug = learning_source().slug
    base = utcnow()
    ids: list[int] = []
    with db.session() as s:
        for p in req.parts:
            guid = f"{req.course_id}:{p.part}"
            existing = _find(s, slug, guid)
            if existing:
                ids.append(existing.id)
                continue
            meta = {
                "course_id": req.course_id, "course_title": req.title,
                "kind": req.kind, "language": req.language,
                "part": p.part, "parts": len(req.parts), "part_title": p.title,
                "verbatim": p.verbatim, "show_notes_md": p.show_notes_md,
            }
            ep = Episode(
                source_slug=slug, guid=guid, title=episode_title(meta),
                link=p.url, source_text=p.script,
                published_at=base + timedelta(seconds=p.part),
                provenance=json.dumps({"course": meta}, ensure_ascii=False),
            )
            s.add(ep)
            try:
                s.commit()
            except IntegrityError:  # a concurrent identical POST won the race
                s.rollback()
                ids.append(_find(s, slug, guid).id)
                continue
            s.refresh(ep)
            ids.append(ep.id)
    return ids


def _find(s, slug: str, guid: str) -> Episode | None:
    return s.exec(select(Episode).where(
        Episode.source_slug == slug, Episode.guid == guid)).first()


def course_intro(meta: dict) -> str:
    title, part, parts, pt = (meta["course_title"], meta["part"],
                              meta["parts"], meta["part_title"])
    da = meta["language"] == "da"
    if meta["kind"] == "audiobook":
        return f"{title}, {'kapitel' if da else 'chapter'} {part}: {pt}."
    return (f"{title}, del {part} af {parts}: {pt}." if da
            else f"{title}, part {part} of {parts}: {pt}.")


def course_outro(meta: dict) -> str:
    title, part, parts = meta["course_title"], meta["part"], meta["parts"]
    last = part >= parts
    da = meta["language"] == "da"
    if meta["kind"] == "audiobook":
        if da:
            text = f"Slut på {title}." if last else f"Slut på kapitel {part}."
        else:
            text = f"The end of {title}." if last else f"End of chapter {part}."
    elif da:
        text = (f"Det var sidste del af {title}." if last
                else f"Det var del {part} af {parts}. Næste gang: del {part + 1}.")
    else:
        text = (f"That was the final part of {title}." if last
                else f"That was part {part} of {parts}. Next up: part {part + 1}.")
    if meta.get("show_notes_md"):
        text += (" Kode og links findes i shownotes." if da
                 else " Code and links are in the show notes.")
    return text


def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```[ \t]*$", re.S | re.M)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# Runs on already-escaped text, so a '"' in the URL is &quot; by now.
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def _inline(text: str) -> str:
    text = _INLINE_CODE_RE.sub(r"<code>\1</code>", _esc(text))
    return _LINK_RE.sub(r'<a href="\2">\1</a>', text)


def _blocks(text: str) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        if all(ln.startswith(("- ", "* ")) for ln in lines):
            items = "".join(f"<li>{_inline(ln[2:])}</li>" for ln in lines)
            out.append(f"<ul>{items}</ul>")
        elif lines[0].startswith("#"):
            out.append(f"<h3>{_inline(lines[0].lstrip('#').strip())}</h3>")
            if lines[1:]:
                out.append(f"<p>{_inline(' '.join(lines[1:]))}</p>")
        else:
            out.append(f"<p>{_inline(' '.join(lines))}</p>")
    return out


def markdown_to_html(md: str) -> str:
    """Just enough markdown for show notes: fenced and inline code, http(s)
    links, bullet lists, headings and paragraphs. Everything else is escaped,
    so nothing in a script author's notes becomes live markup in a podcast
    app's webview."""
    out: list[str] = []
    pos = 0
    for m in _FENCE_RE.finditer(md):
        out += _blocks(md[pos:m.start()])
        out.append(f"<pre><code>{_esc(m.group(1).rstrip())}</code></pre>")
        pos = m.end()
    out += _blocks(md[pos:])
    return "\n".join(out)


def course_show_notes(meta: dict) -> str:
    head = (f"<p><strong>{_esc(meta['course_title'])}</strong>: "
            f"{_esc(meta['part_title'])}</p>")
    notes = markdown_to_html(meta.get("show_notes_md") or "")
    return head + ("\n" + notes if notes else "")


async def render_course(ep_ids: list[int]) -> None:
    """Render parts one at a time, in part order. process_episode records its
    own failures (status error, retry button), so one bad chapter never stops
    the parts after it. Parts already ready are a no-op."""
    from .ingest import process_episode  # ingest imports this module

    source = learning_source()
    for ep_id in ep_ids:
        await process_episode(ep_id, source)
