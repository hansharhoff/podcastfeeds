# Audio Courses and Audiobooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

## TL;DR

- Seven podcastfeeds tasks (config and feed, request parsing, spoken text helpers,
  rendering, HTTP endpoints, retention, docs) and one skills-repo task (the
  `audio-course` plugin).
- Two small deviations from the spec, both called out where they happen: a JSON
  status endpoint `GET /api/course/{course_id}` (the spec said "poll the episode
  page", which is HTML), and `verbatim` changes nothing at render time because
  the plain narration path has no LLM scrub to skip. It is still accepted and
  recorded.
- Nothing is pushed or deployed by this plan. Push, container rebuild and the
  listening acceptance happen after Hans says so.

**Goal:** Let a Claude Code session submit an approved audio course or public-book audiobook that podcastfeeds narrates into a separate `learning` feed.

**Architecture:** A new `app/courses.py` owns the course request (parse, create rows, render in order, spoken intro/outro, show notes). `process_episode` learns one thing: an episode whose provenance carries a `course` key uses the course's language, intro, outro, title and show notes, keeps its `published_at`, and keeps the `course` key through every exit. The feed builder, config and nightly cleanup each get a small learning-aware change. The skill lives in the hansharhoff-skills repo and talks to the server with curl.

**Tech Stack:** Python 3, FastAPI, SQLModel/SQLite, pytest (plain `asyncio` runner, no pytest-asyncio), Jinja2, bash/curl for the skill.

**Spec:** `docs/superpowers/specs/2026-09-29-audio-courses-design.md`

## Global Constraints

- Run Python as `.venv/bin/python` on the host (tests and lint run on the host, not in the container).
- Tests: `.venv/bin/python -m pytest -q`. Lint: `.venv/bin/ruff check .`
- No em dashes or en dashes in code comments, docs, commit messages or skill text.
- Commit messages: plain-English imperative subject, no `feat:` prefix (match `git log`), ending with the attribution lines from the session.
- The pre-commit leak hook must stay enabled; never `--no-verify`.
- `course_id` matches `^[a-z0-9-]{1,80}$`. Caps: 8 parts (course), 60 (audiobook). Script max 60000 chars. Languages: `da`, `en` only.
- The public repo stays secret-free: no token, hostname or reading-queue content in tracked files or in the skill.
- Async tests use `asyncio.new_event_loop().run_until_complete(coro)` (see `tests/test_ticktick.py`).
- Tests share one temp DB per session (`tests/conftest.py`): every test uses its own unique `course_id` / guid.

## Review Focus

1. **Code-like text in a script** (`Vec<T>`, `a < b`, `R&D`): it must reach the TTS unchanged. The no-link fallback runs `strip_html`, which would eat `<T>`. Pinned in Task 4 (`test_script_part_is_narrated_verbatim_with_course_framing`).
2. **Redo of a course part, including an archived one:** it must keep its course title, intro and position in the feed (`published_at` unchanged) and get a fresh 60 days before being archived again. Pinned in Task 4 (`test_redo_keeps_course_meta_and_order`) and Task 6 (`test_a_restored_part_gets_a_fresh_retention_window`).
3. **A chapter that is skipped rather than failed** (fetched page with no article text): the skip exits of `process_episode` rewrite `provenance`, which would drop the `course` key and make a later redo render it as a plain article. Pinned in Task 4 (`test_skip_path_keeps_course_meta`).
4. **One bad chapter in the middle of a book:** later parts must still render. Pinned in Task 4 (`test_failed_part_does_not_block_later_parts`).
5. **Hostile or odd show notes markdown** (`<script>`, `javascript:` links, a `"` in a URL): it must come out as escaped text, never live markup in a podcast app webview. Pinned in Task 3 (`test_markdown_escapes_markup_and_only_links_http`).

---

## File Structure

| File | Responsibility |
|---|---|
| `app/config.py` (modify) | `SourceDef.in_all_feed`; auto-add the `learning` source |
| `app/feedgen.py` (modify) | combined feed excludes `in_all_feed=False` sources |
| `app/courses.py` (create) | course request parsing, row creation, ordered rendering, status, intro/outro/title/show-notes text |
| `app/ingest.py` (modify) | `process_episode` course hooks; `cleanup_old_episodes` archives learning parts; `PIPELINE_VERSION` bump |
| `app/web.py` (modify) | `POST /api/course`, `GET /api/course/{course_id}` |
| `app/templates/index.html` (modify) | `archived` status style and a restore button |
| `README.md` (modify) | how to use and configure courses |
| `tests/test_feedgen.py` (modify) | feed separation tests |
| `tests/test_courses.py` (create) | everything course-specific |
| `~/workspaces/skills/plugins/audio-course/**` (create, separate repo) | the skill |

---

### Task 1: `learning` source and feed separation

**Files:**
- Modify: `app/config.py` (`SourceDef`, `load_config`)
- Modify: `app/feedgen.py:66-72` (`build_feed` query)
- Test: `tests/test_feedgen.py`

**Interfaces:**
- Produces: `SourceDef.in_all_feed: bool = True`; `load_config().sources` always contains a source with `type == "learning"`, `slug == "learning"`, `in_all_feed is False` unless `sources.yaml` defines one.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_feedgen.py`)

```python
def _ready(slug: str, guid: str):
    from app import db
    from app.db import Episode, utcnow

    with db.session() as s:
        s.add(Episode(source_slug=slug, guid=guid, title=f"{slug} {guid}",
                      status="ready", audio_file="a.mp3", audio_bytes=1,
                      published_at=utcnow()))
        s.commit()


def test_learning_source_is_auto_added_and_kept_out_of_all():
    from app.config import load_config

    learning = next(s for s in load_config().sources if s.type == "learning")
    assert learning.slug == "learning"
    assert learning.in_all_feed is False


def test_learning_episodes_stay_out_of_the_combined_feed():
    """Courses are listened to on purpose, in order; mixed into all.xml they
    would bury the news and scatter a series across the day's queue."""
    from app.config import load_config
    from app.feedgen import build_feed

    _ready("learning", "feedtest-course:1")
    _ready("inbox", "https://feedtest.example/a")
    config = load_config()
    combined = build_feed(config, "https://b", "tok")
    assert "feedtest-course:1" not in combined
    assert "feedtest.example/a" in combined
    learning = next(s for s in config.sources if s.slug == "learning")
    assert "feedtest-course:1" in build_feed(config, "https://b", "tok", source=learning)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_feedgen.py -q`
Expected: 2 failures (`StopIteration` for the missing learning source).

- [ ] **Step 3: Implement**

In `app/config.py`, change the `type` comment and add the field after `paid`:

```python
    type: str  # rss | digest | breaking | inbox | learning
```

```python
    in_all_feed: bool = True  # False keeps the source out of all.xml (learning)
```

In `load_config`, directly after the `inbox` auto-add block:

```python
    if not any(s.type == "learning" for s in sources):
        # Audio courses and audiobooks from the audio-course skill. Never in
        # all.xml: a series is listened to on purpose, not between news items.
        # A sources.yaml override must set in_all_feed: false itself.
        sources.append(
            SourceDef(
                slug="learning",
                name=meta.get("learning_name", "Learning"),
                type="learning",
                description="Audio courses and audiobooks requested from Claude Code",
                voice=defaults.get("voice", ""),
                in_all_feed=False,
            )
        )
```

In `app/feedgen.py` `build_feed`, replace the `if source is not None:` query line with:

```python
        if source is not None:
            query = query.where(Episode.source_slug == source.slug)
        else:
            hidden = [s.slug for s in config.sources if not s.in_all_feed]
            if hidden:
                query = query.where(Episode.source_slug.not_in(hidden))  # type: ignore[attr-defined]
```

No scheduler change: `start_scheduler` only schedules `rss`, `breaking` and `digest` types.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_feedgen.py -q && .venv/bin/ruff check app tests`
Expected: all pass, lint clean.

- [ ] **Step 5: Commit**

```bash
git add app/config.py app/feedgen.py tests/test_feedgen.py
git commit -m "Add a learning source that stays out of the combined feed"
```

---

### Task 2: Parse a course request and create its episodes

**Files:**
- Create: `app/courses.py`
- Test: `tests/test_courses.py` (create)

**Interfaces:**
- Consumes: `load_config()` learning source from Task 1.
- Produces (in `app/courses.py`):
  - `class CourseError(ValueError)`
  - `@dataclass CoursePart(part: int, title: str, script: str = "", url: str = "", verbatim: bool = False, show_notes_md: str = "")`
  - `@dataclass CourseRequest(course_id: str, title: str, kind: str, language: str, parts: list[CoursePart])`
  - `parse_course(data: object) -> CourseRequest` (raises `CourseError`)
  - `learning_source() -> SourceDef` (the configured learning source with `allow_pdf=True`)
  - `episode_title(meta: dict) -> str`
  - `create_course(req: CourseRequest) -> list[int]` (episode ids in part order)
  - `course_meta(provenance: str) -> dict | None`
  - Meta dict keys: `course_id, course_title, kind, language, part, parts, part_title, verbatim, show_notes_md`

- [ ] **Step 1: Write the failing tests** (`tests/test_courses.py`)

```python
"""Audio courses: request parsing, episode creation, rendering, endpoints."""
import asyncio
from datetime import timedelta

import pytest

from app import db
from app.db import Episode

SCRIPT = "Ownership is the rule that every value has exactly one owner. " * 10


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _payload(course_id, n=2, **over):
    body = {
        "course_id": course_id, "title": "Rust ownership", "kind": "course",
        "language": "en",
        "episodes": [{"part": i, "title": f"Part {i} title", "script": SCRIPT}
                     for i in range(1, n + 1)],
    }
    return {**body, **over}


def test_parse_accepts_a_valid_course_and_sorts_parts():
    from app.courses import parse_course

    data = _payload("parse-ok")
    data["episodes"].reverse()
    req = parse_course(data)
    assert [p.part for p in req.parts] == [1, 2]
    assert req.kind == "course" and req.language == "en"


@pytest.mark.parametrize("mutate, reason", [
    (lambda d: d.update(course_id="Bad_ID"), "course_id"),
    (lambda d: d.update(title=""), "title"),
    (lambda d: d.update(kind="lecture"), "kind"),
    (lambda d: d.update(language="auto"), "language"),
    (lambda d: d.update(episodes=[]), "episodes"),
    (lambda d: d["episodes"][1].update(part=3), "1..N"),
    (lambda d: d["episodes"][1].update(part=1), "1..N"),
    (lambda d: d["episodes"][0].update(url="https://x.test/c1"), "exactly one"),
    (lambda d: d["episodes"][0].pop("script"), "exactly one"),
    (lambda d: d["episodes"][0].update(script="too short"), "script"),
    (lambda d: d["episodes"][0].update(script="x" * 60001), "script"),
    (lambda d: d["episodes"][0].update(script=None, url="ftp://x.test/c1"), "url"),
    (lambda d: d["episodes"][0].update(part="1"), "integer"),
    (lambda d: d["episodes"][0].update(show_notes_md="x" * 20001), "show_notes_md"),
])
def test_parse_rejects_bad_requests_with_a_reason(mutate, reason):
    from app.courses import CourseError, parse_course

    data = _payload("parse-bad")
    mutate(data)
    with pytest.raises(CourseError, match=reason):
        parse_course(data)


def test_parse_enforces_the_part_cap_per_kind():
    from app.courses import CourseError, parse_course

    with pytest.raises(CourseError, match="at most 8"):
        parse_course(_payload("cap-course", n=9))
    assert len(parse_course(_payload("cap-book", n=9, kind="audiobook")).parts) == 9


def test_create_course_makes_ordered_rows_with_course_meta():
    from app.courses import course_meta, create_course, parse_course

    ids = create_course(parse_course(_payload("create-order", n=3)))
    with db.session() as s:
        eps = [s.get(Episode, i) for i in ids]
    assert [e.guid for e in eps] == ["create-order:1", "create-order:2", "create-order:3"]
    assert all(e.source_slug == "learning" and e.status == "pending" for e in eps)
    assert eps[0].published_at < eps[1].published_at < eps[2].published_at
    assert eps[1].title == "Rust ownership (2/3): Part 2 title"
    meta = course_meta(eps[1].provenance)
    assert meta["part"] == 2 and meta["parts"] == 3 and meta["kind"] == "course"
    assert eps[1].source_text == SCRIPT.strip()


def test_create_course_is_idempotent_and_fills_gaps():
    from app.courses import create_course, parse_course

    first = create_course(parse_course(_payload("create-gaps", n=1)))
    # Resubmitting with changed text for part 1 leaves part 1 alone; part 2 is new.
    data = _payload("create-gaps", n=2)
    data["episodes"][0]["script"] = SCRIPT + " Changed."
    second = create_course(parse_course(data))
    assert second[0] == first[0]
    with db.session() as s:
        assert s.get(Episode, first[0]).source_text == SCRIPT.strip()
        assert s.get(Episode, second[1]).guid == "create-gaps:2"


def test_course_meta_tolerates_non_course_provenance():
    from app.courses import course_meta

    assert course_meta("") is None
    assert course_meta("not json") is None
    assert course_meta('{"pipeline_version": "x"}') is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.courses'`.

- [ ] **Step 3: Implement `app/courses.py`**

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q && .venv/bin/ruff check app tests`
Expected: all pass, lint clean.

- [ ] **Step 5: Commit**

```bash
git add app/courses.py tests/test_courses.py
git commit -m "Parse course requests and create one learning episode per part"
```

---

### Task 3: Spoken framing and show notes for course parts

**Files:**
- Modify: `app/courses.py`
- Test: `tests/test_courses.py`

**Interfaces:**
- Consumes: meta dict from Task 2.
- Produces: `course_intro(meta) -> str`, `course_outro(meta) -> str`, `markdown_to_html(md: str) -> str`, `course_show_notes(meta) -> str`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _meta(**over):
    base = {"course_id": "m", "course_title": "Shape Up", "kind": "course",
            "language": "en", "part": 2, "parts": 4, "part_title": "Borrowing",
            "verbatim": False, "show_notes_md": ""}
    return {**base, **over}


@pytest.mark.parametrize("over, expected", [
    ({}, "Shape Up, part 2 of 4: Borrowing."),
    ({"language": "da"}, "Shape Up, del 2 af 4: Borrowing."),
    ({"kind": "audiobook"}, "Shape Up, chapter 2: Borrowing."),
    ({"kind": "audiobook", "language": "da"}, "Shape Up, kapitel 2: Borrowing."),
])
def test_course_intro(over, expected):
    from app.courses import course_intro

    assert course_intro(_meta(**over)) == expected


def test_course_outro_points_ahead_and_at_notes():
    from app.courses import course_outro

    assert course_outro(_meta()) == "That was part 2 of 4. Next up: part 3."
    assert course_outro(_meta(part=4)) == "That was the final part of Shape Up."
    assert course_outro(_meta(show_notes_md="`x`")).endswith(
        "Code and links are in the show notes.")
    assert course_outro(_meta(language="da", part=4)) == "Det var sidste del af Shape Up."
    assert course_outro(_meta(kind="audiobook")) == "End of chapter 2."
    assert course_outro(_meta(kind="audiobook", part=4)) == "The end of Shape Up."


def test_markdown_escapes_markup_and_only_links_http():
    from app.courses import markdown_to_html

    html = markdown_to_html(
        "Intro with `Vec<T>` and <script>alert(1)</script>.\n\n"
        "- [docs](https://doc.rust-lang.org/book/)\n"
        "- [bad](javascript:alert(1))\n"
        '- [quote](https://x.test/a"b)\n\n'
        "```rust\nfn main() { let v: Vec<u8> = vec![]; }\n```\n"
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<code>Vec&lt;T&gt;</code>" in html
    assert '<a href="https://doc.rust-lang.org/book/">docs</a>' in html
    assert 'href="javascript' not in html
    assert 'href="https://x.test/a&quot;b"' in html
    assert "<pre><code>fn main() { let v: Vec&lt;u8&gt; = vec![]; }</code></pre>" in html
    assert html.count("<li>") == 3


def test_course_show_notes_has_a_header_and_the_notes():
    from app.courses import course_show_notes

    html = course_show_notes(_meta(show_notes_md="See `cargo build`."))
    assert html.startswith("<p><strong>Shape Up</strong>: Borrowing</p>")
    assert "<code>cargo build</code>" in html
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q -k "intro or outro or markdown or show_notes"`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement** (append to `app/courses.py`)

```python
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
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q && .venv/bin/ruff check app tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/courses.py tests/test_courses.py
git commit -m "Frame course parts with their own intro, outro and show notes"
```

---

### Task 4: Render course parts through `process_episode`

**Files:**
- Modify: `app/ingest.py` (`process_episode`, `PIPELINE_VERSION` is in `app/config.py`)
- Modify: `app/config.py` (`PIPELINE_VERSION`)
- Modify: `app/courses.py` (add `render_course`)
- Test: `tests/test_courses.py`

**Interfaces:**
- Consumes: `course_meta`, `course_intro`, `course_outro`, `course_show_notes`, `episode_title`, `learning_source`, `create_course`, `parse_course`.
- Produces: `async render_course(ep_ids: list[int]) -> None`.

Spec deviation, stated in the TL;DR: the plain narration path (the one a script part takes) only runs `scrub_light`, which has no LLM step. `verbatim` is therefore stored in provenance and otherwise inert. No code branches on it.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _fake_tts(monkeypatch, calls):
    from app import ingest

    async def fake_synthesize(script, **kw):
        calls.append({"title": kw.get("title"), "text": script})
        return "course.mp3", 100, 60

    async def fake_blocks(blocks, **kw):
        calls.append({"title": kw.get("title"),
                      "text": "\n\n".join(b["text"] for b in blocks)})
        return "course.mp3", 100, 60

    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)
    monkeypatch.setattr(ingest, "synthesize_blocks", fake_blocks)


def test_script_part_is_narrated_verbatim_with_course_framing(monkeypatch):
    from app.courses import course_meta, create_course, parse_course, render_course

    calls = []
    _fake_tts(monkeypatch, calls)
    data = _payload("render-script", n=2)
    tricky = SCRIPT + " A Vec<T> owns its buffer, and R&D says a < b."
    data["episodes"][0].update(script=tricky, show_notes_md="```\nlet v: Vec<u8>;\n```")
    ids = create_course(parse_course(data))
    with db.session() as s:
        before = s.get(Episode, ids[0]).published_at
    _run(render_course(ids))

    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "ready"
    assert ep.title == "Rust ownership (1/2): Part 1 title"
    assert ep.published_at == before
    assert course_meta(ep.provenance)["part"] == 1
    assert "<pre><code>let v: Vec&lt;u8&gt;;</code></pre>" in ep.description
    text = calls[0]["text"]
    assert text.startswith("Rust ownership, part 1 of 2: Part 1 title.")
    assert "A Vec<T> owns its buffer, and R&D says a < b." in text
    assert text.rstrip().endswith("Code and links are in the show notes.")
    # render order is part order; the ID3 title is the full episode title
    assert [c["title"] for c in calls] == ["Rust ownership (1/2): Part 1 title",
                                           "Rust ownership (2/2): Part 2 title"]


def test_url_part_uses_article_pipeline_with_course_framing(monkeypatch):
    from app import ingest
    from app.courses import create_course, parse_course, render_course

    calls = []
    _fake_tts(monkeypatch, calls)
    page = ("<html><head><title>Chapter One | Book</title></head><body><article>"
            # varied text: the extractor may drop repeated identical paragraphs
            + "".join(f"<p>Paragraph {i}: shaping sets boundaries on project {i} "
                      "before anyone commits to building it.</p>" for i in range(12))
            + "</article></body></html>")

    async def fake_fetch_html(url):
        return page

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    data = _payload("render-url", n=1, kind="audiobook", title="Shape Up")
    data["episodes"][0] = {"part": 1, "title": "Principles of Shaping",
                           "url": "https://book.example/shape-up/1"}
    ids = create_course(parse_course(data))
    _run(render_course(ids))

    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "ready", ep.error
    assert ep.title == "Shape Up (1/1): Principles of Shaping"
    assert calls[0]["text"].startswith("Shape Up, chapter 1: Principles of Shaping.")
    assert calls[0]["text"].rstrip().endswith("The end of Shape Up.")


def test_failed_part_does_not_block_later_parts(monkeypatch):
    from app import ingest
    from app.courses import create_course, parse_course, render_course

    calls = []
    _fake_tts(monkeypatch, calls)

    async def boom(url):
        raise RuntimeError("chapter host down")

    monkeypatch.setattr(ingest, "fetch_html", boom)
    data = _payload("render-fail", n=2, kind="audiobook")
    data["episodes"][0] = {"part": 1, "title": "Gone",
                           "url": "https://book.example/gone"}
    ids = create_course(parse_course(data))
    _run(render_course(ids))
    with db.session() as s:
        assert s.get(Episode, ids[0]).status == "error"
        assert s.get(Episode, ids[1]).status == "ready"


def test_skip_path_keeps_course_meta(monkeypatch):
    """The skip exits rewrite provenance; without the course key a later redo
    would narrate the chapter as a plain article with the wrong title."""
    from app import ingest
    from app.courses import course_meta, create_course, parse_course, render_course

    async def near_empty(url):
        return "<html><body><p>Hi.</p></body></html>"

    monkeypatch.setattr(ingest, "fetch_html", near_empty)
    data = _payload("render-skip", n=1, kind="audiobook")
    data["episodes"][0] = {"part": 1, "title": "Empty",
                           "url": "https://book.example/empty"}
    ids = create_course(parse_course(data))
    _run(render_course(ids))
    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "skipped"
    assert course_meta(ep.provenance)["course_id"] == "render-skip"


def test_redo_keeps_course_meta_and_order(monkeypatch):
    from app import ingest
    from app.courses import (
        course_meta, create_course, learning_source, parse_course, render_course,
    )

    calls = []
    _fake_tts(monkeypatch, calls)
    ids = create_course(parse_course(_payload("render-redo", n=1)))
    _run(render_course(ids))
    with db.session() as s:
        first = s.get(Episode, ids[0])
        published, first.status = first.published_at, "pending"
        s.add(first)
        s.commit()
    _run(ingest.process_episode(ids[0], learning_source()))
    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "ready" and ep.published_at == published
    assert ep.title == "Rust ownership (1/1): Part 1 title"
    assert course_meta(ep.provenance)["part"] == 1
    assert calls[-1]["text"].startswith("Rust ownership, part 1 of 1: Part 1 title.")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q -k "render or part or redo"`
Expected: FAIL with `ImportError: cannot import name 'render_course'`.

- [ ] **Step 3: Implement**

`app/courses.py`, append:

```python
async def render_course(ep_ids: list[int]) -> None:
    """Render parts one at a time, in part order. process_episode records its
    own failures (status error, retry button), so one bad chapter never stops
    the parts after it. Parts already ready are a no-op."""
    from .ingest import process_episode  # ingest imports this module

    source = learning_source()
    for ep_id in ep_ids:
        await process_episode(ep_id, source)
```

`app/config.py`: set `PIPELINE_VERSION = "2026.09.30-audio-courses"`.

`app/ingest.py`, top-level import (next to the other `from .` imports):

```python
from .courses import (
    course_intro,
    course_meta,
    course_outro,
    course_show_notes,
    episode_title,
)
```

`app/ingest.py` `process_episode`, in order:

1. In the opening `with db.session()` block, after `source_text = ep.source_text`:
   ```python
           course = course_meta(ep.provenance)
   ```
2. Define a helper right after that block (before `try:`), used by all three skip exits:
   ```python
       def _keep_course(prov_: dict) -> dict:
           # Skip exits rewrite provenance; the course key must survive them or
           # a later redo narrates the part as a plain article.
           return {**prov_, "course": course} if course else prov_
   ```
   Wrap the three `ep.provenance = json.dumps({...})` calls in the PDF skip, paywall skip and no-content skip: `ep.provenance = json.dumps(_keep_course({...}))`.
3. Immediately before `if len(body) < 200:` add:
   ```python
           if course and not link:
               # A course script is the finished spoken text: nothing to fetch,
               # and the strip_html fallback below would eat "Vec<T>" or "a < b".
               body = source_text
   ```
4. Replace `language = detect_language(f"{title}\n{body}")` with:
   ```python
           language = course["language"] if course else detect_language(f"{title}\n{body}")
   ```
   and after the `title = title.removeprefix(...)` line add:
   ```python
           if course:
               title = episode_title(course)
   ```
5. Replace the `intro = _episode_intro(...)` assignment and `outro` helper with:
   ```python
           intro = course_intro(course) if course else _episode_intro(
               title, source.name, language, preview=is_preview, fetch_issue=fetch_issue)

           def outro(has_images: bool) -> str:
               if course:
                   return course_outro(course)
               return (_preview_outro(language, fetch_issue=fetch_issue)
                       if is_preview else _episode_outro(language, has_images))
   ```
6. After `prov: dict = {...}` add `if course: prov["course"] = course`.
7. Right before `if is_preview:` (after the narration branches) add:
   ```python
           if course and not link:
               show_notes = course_show_notes(course)
   ```
8. In the final save block replace the title and `published_at` lines:
   ```python
               if course:
                   ep.title = title  # already episode_title(course); no source label
               else:
                   base_title = f"{source_label}: {title}" if source.type != "digest" else title
                   ep.title = f"[Preview] {base_title}" if is_preview else base_title
   ```
   ```python
               # A course part keeps the slot create_course gave it, so a redo
               # or a slow chapter never jumps the series order.
               if not (course and ep.published_at):
                   ep.published_at = utcnow()
   ```

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests`
Expected: all pass (existing ingest and ticktick tests included).

- [ ] **Step 5: Commit**

```bash
git add app/courses.py app/ingest.py app/config.py tests/test_courses.py
git commit -m "Narrate course parts in order with course framing"
```

---

### Task 5: HTTP endpoints

**Files:**
- Modify: `app/courses.py` (add `course_status`)
- Modify: `app/web.py`
- Test: `tests/test_courses.py`

**Interfaces:**
- Produces: `POST /{token}/api/course` returns `{"ok": true, "course_id", "episode_ids": [...]}` or 400 `{"detail": reason}`; `GET /{token}/api/course/{course_id}` returns `{"course_id", "parts": [{"part", "episode_id", "title", "status", "error", "audio_seconds"}]}` or 404; `course_status(course_id) -> list[dict]`.

Spec deviation, stated in the TL;DR: the status endpoint is new. The spec had the skill poll the HTML episode page.

- [ ] **Step 1: Write the failing tests** (append)

```python
@pytest.fixture
def api(monkeypatch):
    from fastapi.testclient import TestClient

    from app import web
    from app.config import get_token

    spawned = []

    def fake_spawn(coro, name=None):
        spawned.append(name)
        coro.close()  # never render in endpoint tests

    monkeypatch.setattr(web, "spawn", fake_spawn)
    return TestClient(web.app), get_token(), spawned


def test_post_course_creates_parts_and_starts_rendering(api):
    client, token, spawned = api
    r = client.post(f"/{token}/api/course", json=_payload("api-ok", n=2))
    assert r.status_code == 200
    assert len(r.json()["episode_ids"]) == 2
    assert spawned == ["course:api-ok"]


def test_post_course_rejects_with_reason(api):
    client, token, _ = api
    r = client.post(f"/{token}/api/course", json=_payload("api-bad", language="fr"))
    assert r.status_code == 400 and "language" in r.json()["detail"]
    r = client.post(f"/{token}/api/course", content=b"not json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_post_course_needs_the_token(api):
    client, _, _ = api
    assert client.post("/wrong/api/course", json=_payload("api-tok")).status_code == 404


def test_course_status_lists_parts_in_order(api):
    client, token, _ = api
    client.post(f"/{token}/api/course", json=_payload("api-status", n=2))
    r = client.get(f"/{token}/api/course/api-status")
    assert r.status_code == 200
    parts = r.json()["parts"]
    assert [p["part"] for p in parts] == [1, 2]
    assert parts[0]["status"] == "pending"
    # a prefix of another course's id must not match
    client.post(f"/{token}/api/course", json=_payload("api-status-2", n=1))
    assert len(client.get(f"/{token}/api/course/api-status").json()["parts"]) == 2
    assert client.get(f"/{token}/api/course/nope").status_code == 404
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q -k "post_course or course_status"`
Expected: 404/405 assertion failures (routes don't exist).

- [ ] **Step 3: Implement**

`app/courses.py`, append:

```python
def course_status(course_id: str) -> list[dict]:
    slug = learning_source().slug
    with db.session() as s:
        rows = s.exec(select(Episode).where(
            Episode.source_slug == slug,
            Episode.guid.startswith(f"{course_id}:"),  # type: ignore[attr-defined]
        )).all()
    parts = []
    for ep in rows:
        meta = course_meta(ep.provenance) or {}
        parts.append({
            "part": meta.get("part"), "episode_id": ep.id, "title": ep.title,
            "status": ep.status, "error": ep.error,
            "audio_seconds": ep.audio_seconds,
        })
    return sorted(parts, key=lambda p: p["part"] or 0)
```

`app/web.py`: add `from .courses import CourseError, course_status, create_course, parse_course, render_course` and, after `api_submit`:

```python
@app.post("/{token}/api/course")
async def api_course(token: str, request: Request):
    """Accept an approved course or audiobook from the audio-course skill."""
    _check(token)
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be JSON") from None
    try:
        req = parse_course(data)
    except CourseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    ep_ids = create_course(req)
    spawn(render_course(ep_ids), name=f"course:{req.course_id}")
    return {"ok": True, "course_id": req.course_id, "episode_ids": ep_ids}


@app.get("/{token}/api/course/{course_id}")
async def api_course_status(token: str, course_id: str):
    _check(token)
    parts = course_status(course_id)
    if not parts:
        raise HTTPException(status_code=404)
    return {"course_id": course_id, "parts": parts}
```

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/courses.py app/web.py tests/test_courses.py
git commit -m "Accept courses over HTTP and report their progress"
```

---

### Task 6: Keep course text, drop course audio after the retention window

**Files:**
- Modify: `app/ingest.py` (`cleanup_old_episodes`)
- Modify: `app/templates/index.html` (status style, restore button)
- Test: `tests/test_courses.py`

**Interfaces:**
- Consumes: learning source (Task 1), provenance `generated_at` written by `process_episode`.
- Produces: episode status `archived` (no audio, text kept, not in feeds).

- [ ] **Step 1: Write the failing tests** (append)

```python
def _aged(slug, guid, status="ready", generated_days_ago=None, days_old=90):
    import json as _json

    from app.config import MEDIA_DIR
    from app.db import utcnow

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{guid.replace(':', '_').replace('/', '_')}.mp3"
    (MEDIA_DIR / fname).write_bytes(b"x")
    prov = {}
    if generated_days_ago is not None:
        prov["generated_at"] = (utcnow() - timedelta(days=generated_days_ago)).isoformat()
    with db.session() as s:
        ep = Episode(source_slug=slug, guid=guid, title=guid, status=status,
                     audio_file=fname, audio_bytes=1, audio_seconds=60,
                     source_text=SCRIPT, provenance=_json.dumps(prov),
                     created_at=utcnow() - timedelta(days=days_old))
        s.add(ep)
        s.commit()
        s.refresh(ep)
        return ep.id, MEDIA_DIR / fname


def test_retention_archives_learning_and_deletes_the_rest():
    from app.ingest import cleanup_old_episodes

    course_id, course_mp3 = _aged("learning", "ret-course:1")
    news_id, news_mp3 = _aged("inbox", "https://ret.example/a")
    failed_id, _ = _aged("learning", "ret-failed:1", status="error")
    _run(cleanup_old_episodes(60))
    with db.session() as s:
        course = s.get(Episode, course_id)
        assert s.get(Episode, news_id) is None
        assert s.get(Episode, failed_id).status == "error"
    assert course.status == "archived"
    assert course.audio_file == "" and course.audio_seconds == 0
    assert course.source_text == SCRIPT
    assert not course_mp3.exists() and not news_mp3.exists()
    _run(cleanup_old_episodes(60))  # a second night is a no-op
    with db.session() as s:
        assert s.get(Episode, course_id).status == "archived"


def test_a_restored_part_gets_a_fresh_retention_window():
    """Redo keeps created_at, so age must come from when the audio was made."""
    from app.ingest import cleanup_old_episodes

    ep_id, mp3 = _aged("learning", "ret-restored:1", generated_days_ago=1)
    _run(cleanup_old_episodes(60))
    with db.session() as s:
        assert s.get(Episode, ep_id).status == "ready"
    assert mp3.exists()


def test_archived_parts_stay_out_of_the_learning_feed():
    from app.config import load_config
    from app.feedgen import build_feed
    from app.ingest import cleanup_old_episodes

    _aged("learning", "ret-feed:1")
    _run(cleanup_old_episodes(60))
    config = load_config()
    learning = next(s for s in config.sources if s.slug == "learning")
    assert "ret-feed:1" not in build_feed(config, "https://b", "tok", source=learning)
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_courses.py -q -k "retention or restored or archived"`
Expected: FAIL (learning rows are deleted outright).

- [ ] **Step 3: Implement**

Replace `cleanup_old_episodes` in `app/ingest.py`:

```python
def _audio_made_at(ep: Episode) -> datetime:
    """When this episode's current audio was rendered. A redo keeps
    created_at, so a restored course part would otherwise be archived again
    the very next night."""
    try:
        made = datetime.fromisoformat(json.loads(ep.provenance)["generated_at"])
    except (ValueError, KeyError, TypeError):
        made = ep.created_at
    return made if made.tzinfo else made.replace(tzinfo=UTC)


async def cleanup_old_episodes(retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    # Course parts keep their text: the script is the personal, expensive
    # part and redo can re-narrate it. Only the MP3 is worth reclaiming.
    keep_text = {s.slug for s in load_config().sources if s.type == "learning"}
    removed = archived = 0
    with db.session() as s:
        old = s.exec(select(Episode).where(
            Episode.created_at < cutoff, Episode.status != "archived",
        )).all()
        for ep in old:
            if ep.source_slug in keep_text:
                if ep.status != "ready" or _audio_made_at(ep) >= cutoff:
                    continue
                if ep.audio_file:
                    (MEDIA_DIR / ep.audio_file).unlink(missing_ok=True)
                ep.audio_file, ep.audio_bytes, ep.audio_seconds = "", 0, 0
                ep.status = "archived"
                s.add(ep)
                archived += 1
                continue
            if ep.audio_file:
                (MEDIA_DIR / ep.audio_file).unlink(missing_ok=True)
            s.delete(ep)
            removed += 1
        s.commit()
    if removed or archived:
        log.info("cleanup: removed %d, archived %d episodes older than %d days",
                 removed, archived, retention_days)
    return removed
```

`app/templates/index.html`:
- CSS line 15 area: add `.status-archived { color: #888; }`.
- In the row actions, after the `skipped` unskip form:
  ```jinja
      {% if ep.status == 'archived' %}
      <form method="post" action="/{{ token }}/api/redo/{{ ep.id }}" onsubmit="return confirm('Re-narrate this archived part from its kept script?')"><button title="re-narrate from the kept script">↻ restore</button></form>
      {% endif %}
  ```

`_requeue` already resets any status to `pending`, and `_source_for` finds the learning source, so the redo route needs no change.

- [ ] **Step 4: Run all tests**

Run: `.venv/bin/python -m pytest -q && .venv/bin/ruff check app tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add app/ingest.py app/templates/index.html tests/test_courses.py
git commit -m "Archive old course parts instead of deleting their scripts"
```

---

### Task 7: Document it

**Files:**
- Modify: `README.md` (new section after "Intake & special sources")

- [ ] **Step 1: Add the section**

```markdown
## Audio courses and audiobooks

The `audio-course` skill (hansharhoff-skills marketplace) turns a topic into an
approved audio course, or a public book into an audiobook, from any Claude Code
session. It POSTs the finished parts here; they land in the **Learning** feed
(`/feeds/learning.xml`), which is deliberately **not** part of `all.xml`, so
subscribe to it separately.

- `POST /{token}/api/course` with `{course_id, title, kind: course|audiobook,
  language: da|en, episodes: [{part, title, script | url, show_notes_md?,
  verbatim?}]}`. Caps: 8 parts per course, 60 per audiobook. Reposting the same
  `course_id` only fills in missing parts and retries failed ones.
- `GET /{token}/api/course/{course_id}` reports each part's status.
- Parts render one at a time in order and keep their feed order across redos.
- After `retention_days` the MP3 is deleted but the part is kept as
  `archived` with its script; **↻ restore** on the admin page re-narrates it.
- Skill setup: export `PODCASTFEEDS_URL` (e.g. `http://localhost:8080`) and
  either `PODCASTFEEDS_TOKEN` or `PODCASTFEEDS_TOKEN_FILE` (path to
  `data/token.txt`).
- Overriding the source in `sources.yaml` (e.g. a fixed teacher `voice`) needs
  `type: learning` and `in_all_feed: false`.
```

Also update the Architecture block's source list line to mention `learning`: add a line `│  learning: approved courses POSTed by the audio-course skill` under `inbox:`.

- [ ] **Step 2: Check for dashes and commit**

```bash
grep -nP '[\x{2013}\x{2014}]' README.md docs/superpowers/plans/2026-09-30-audio-courses.md app/courses.py || echo clean
git add README.md docs/superpowers/plans/2026-09-30-audio-courses.md
git commit -m "Document audio courses and the learning feed"
```

(Existing README em dashes predate the rule; only lines this task writes must be dash-free.)

---

### Task 8: The `audio-course` skill plugin

**Files (repo `~/workspaces/skills`, a fresh clone of `git@github.com:hansharhoff/skills.git`; never edit the Claude-managed checkout under `~/.claude/plugins/marketplaces`):**
- Create: `plugins/audio-course/.claude-plugin/plugin.json`
- Create: `plugins/audio-course/README.md`
- Create: `plugins/audio-course/skills/audio-course/SKILL.md`
- Modify: `.claude-plugin/marketplace.json` (add entry)

**Interfaces:**
- Consumes: the two endpoints from Task 5, env vars `PODCASTFEEDS_URL`, `PODCASTFEEDS_TOKEN` / `PODCASTFEEDS_TOKEN_FILE`.

- [ ] **Step 1: Clone and read conventions**

```bash
git clone git@github.com:hansharhoff/skills.git ~/workspaces/skills
cat ~/workspaces/skills/CLAUDE.md
```

- [ ] **Step 2: `plugin.json`** (the four verified fields only)

```json
{
  "name": "audio-course",
  "description": "Turn a topic into an approved audio course, or a public book into a chapter-per-episode audiobook, and send it to your podcastfeeds Learning feed.",
  "author": { "name": "Hans Harhoff Andersen" }
}
```

- [ ] **Step 3: `SKILL.md`**

````markdown
---
name: audio-course
description: >
  Build a passive audio course (or an audiobook of a free public book) and
  deliver it to the user's podcastfeeds Learning feed. Use when the user says
  "make me an audio course on X", "audio tutorial about X", "teach me X as a
  podcast", "turn this book into an audiobook", "put X in my podcast feed as a
  course", or points at a free online book and wants to listen to it.
---

# Audio course

A hands-free course for listening on a commute. Unlike teach-tech, never stop
for the listener to type anything: everything is taught by explanation,
analogy and repetition. Code goes in show notes.

## 1. Classify

- A **topic** goes to the course path.
- A **book** (a URL, or a title with a free official online edition) gets a
  choice via AskUserQuestion: "Audiobook (the author's text, one episode per
  chapter)" or "Course about it". Only offer the audiobook when the full text
  is freely and officially published online.

## 2. Course path

1. **Clarify** with one AskUserQuestion call (up to three questions,
   multiple choice): why they want to learn it, which aspects matter, what they
   already know. Check what they usually work with (their CLAUDE.md, the
   current repo) to pick analogies.
2. **Research** with web search: current best practice and facts you would
   otherwise state from memory.
3. **Size and outline.** One episode for a narrow topic, up to 8 parts of 15 to
   20 minutes (about 2200 to 3000 spoken words) for a broad one. Show: course
   title, language (`da` or `en`, ask if unclear), and per part a title plus
   one line on what the listener will understand afterwards. **Stop and wait
   for an explicit yes.** Loop on edits.
4. **Write each part for the ear:**
   - flowing spoken prose; no markdown, lists, tables, headings or URLs
   - describe code in words ("a function that borrows the string and returns
     its length"); never read symbols out
   - open with a two-sentence recap of the previous part (from part 2 on)
   - close with a short summary of the part's key ideas
   - use analogies from the listener's own stack
   - put snippets and source links in `show_notes_md` (markdown: fenced code,
     inline code, `[text](https://...)` links, `- ` bullets)
   - do not add a title line or a sign-off; the server adds "X, part 2 of 4:
     Y." and "Next up: part 3."

## 3. Audiobook path

1. Fetch the table of contents. Show the chapter list, marking front matter and
   appendices in or out, and whether each chapter goes as a **URL** (the book
   has a page per chapter; preferred, the server extracts text and images
   itself) or as **verbatim text** (the whole book is one page: split it by
   chapter heading and send each chapter as `script` with `"verbatim": true`,
   unchanged). **Stop and wait for an explicit yes.**
2. Cap: 60 chapters.

## 4. Submit

1. Build the request and save it before sending, so nothing is lost if the
   server is down:

   ```bash
   mkdir -p ~/.cache/audio-course
   # write ~/.cache/audio-course/<course_id>.json with:
   # {"course_id": "<a-z0-9- slug, e.g. rust-ownership-2026-09-30>",
   #  "title": "...", "kind": "course" | "audiobook", "language": "en" | "da",
   #  "episodes": [{"part": 1, "title": "...", "script": "...",
   #                "show_notes_md": "..."} | {"part": 1, "title": "...",
   #                "url": "https://..."}]}
   ```

2. Send it:

   ```bash
   URL="${PODCASTFEEDS_URL:?set PODCASTFEEDS_URL}"
   TOKEN="${PODCASTFEEDS_TOKEN:-$(cat "${PODCASTFEEDS_TOKEN_FILE:?set PODCASTFEEDS_TOKEN or PODCASTFEEDS_TOKEN_FILE}")}"
   curl -sS --fail-with-body -X POST "$URL/$TOKEN/api/course" \
     -H 'content-type: application/json' \
     --data @"$HOME/.cache/audio-course/<course_id>.json"
   ```

   Never print the token or put it in any file.
3. On failure, show the error and tell the user the file path and that
   rerunning the same curl resubmits it (resubmits are safe: existing parts are
   kept, failed ones retried). A 400 names the problem: fix the JSON and resend.
4. On success, poll every 30 seconds until part 1 is `ready` or `error`
   (give up after 15 minutes and say so):

   ```bash
   curl -sS "$URL/$TOKEN/api/course/<course_id>"
   ```

   Report part 1's status and length, and that the rest follow in order in the
   Learning feed. Do not wait for every part.
````

- [ ] **Step 4: `README.md`** (marketplace-facing, short)

```markdown
# audio-course

Ask for an audio course on any topic, or an audiobook of a free public book, and
it lands in your self-hosted podcastfeeds "Learning" feed.

The session asks why you want to learn it and what you already know, researches
the topic, and shows an outline for approval. Only then does it write one
listening-friendly script per part and send them to podcastfeeds, which
narrates them in order. Code and links go in the show notes.

Requires a running podcastfeeds instance with `/api/course` and these
environment variables: `PODCASTFEEDS_URL`, and `PODCASTFEEDS_TOKEN` or
`PODCASTFEEDS_TOKEN_FILE`.
```

- [ ] **Step 5: Add to the catalog** in `.claude-plugin/marketplace.json` `plugins[]`:

```json
{
  "name": "audio-course",
  "source": "./plugins/audio-course",
  "description": "Turn a topic into an approved audio course, or a free public book into a chapter-per-episode audiobook, delivered to a podcastfeeds Learning feed."
}
```

- [ ] **Step 6: Validate and commit locally (do not push)**

```bash
cd ~/workspaces/skills
python3 -m json.tool .claude-plugin/marketplace.json >/dev/null
python3 -m json.tool plugins/audio-course/.claude-plugin/plugin.json >/dev/null
grep -rnP '[\x{2013}\x{2014}]' plugins/audio-course && echo "DASHES FOUND" || echo clean
git add plugins/audio-course .claude-plugin/marketplace.json
git commit -m "Add audio-course skill: topic or public book to a podcast feed"
```

---

## After the tasks (needs Hans's go-ahead)

1. Push podcastfeeds and rebuild the container (podcastfeeds-ship skill).
2. Push the skills repo; Hans runs `/plugin` refresh, `/plugin install audio-course@hansharhoff/skills`, `/reload-plugins`, and checks the skill count goes up by one.
3. Set `PODCASTFEEDS_URL` and `PODCASTFEEDS_TOKEN_FILE` in the shell profile.
4. Acceptance by listening: one short course end to end, and Shape Up chapters 1 and 2 as an audiobook.
