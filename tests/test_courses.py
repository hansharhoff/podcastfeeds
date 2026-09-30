"""Audio courses: request parsing, episode creation, rendering, endpoints."""
import asyncio

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
        course_meta,
        create_course,
        learning_source,
        parse_course,
        render_course,
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
