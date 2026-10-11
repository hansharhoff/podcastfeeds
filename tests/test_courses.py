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

    first = create_course(parse_course(_payload("create-gaps", n=2)))
    with db.session() as s:  # a part that went missing
        s.delete(s.get(Episode, first[1]))
        s.commit()
    # Resubmitting with changed text for part 1 leaves part 1 alone; part 2 is back.
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
    ({"kind": "audiobook"}, "Shape Up: Borrowing."),
    ({"kind": "audiobook", "language": "da"}, "Shape Up: Borrowing."),
])
def test_course_intro(over, expected):
    from app.courses import course_intro

    assert course_intro(_meta(**over)) == expected


def test_course_outro_points_ahead_and_at_notes():
    from app.courses import course_outro

    assert course_outro(_meta()) == "That was part 2 of 4. Next up: part 3."
    assert course_outro(_meta(part=4)) == "That was the final part of Shape Up."
    assert course_outro(_meta(show_notes_md="`x`")).endswith(
        "There's more in the show notes.")
    assert course_outro(_meta(language="da", part=4)) == "Det var sidste del af Shape Up."
    assert course_outro(_meta(kind="audiobook")) == "End of Borrowing."


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
    assert html.startswith("<p><strong>Shape Up</strong>: Borrowing<br/><small>Part 2 of 4</small></p>")
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
    _run(render_course(ids, "t"))

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
    assert text.rstrip().endswith("There's more in the show notes.")
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
    _run(render_course(ids, "t"))

    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "ready", ep.error
    assert ep.title == "Shape Up: Principles of Shaping"
    assert calls[0]["text"].startswith("Shape Up: Principles of Shaping.")
    assert calls[0]["text"].rstrip().endswith("End of Principles of Shaping.")


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
    _run(render_course(ids, "t"))
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
    _run(render_course(ids, "t"))
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
    _run(render_course(ids, "t"))
    with db.session() as s:
        first = s.get(Episode, ids[0])
        published, first.status = first.published_at, "pending"
        s.add(first)
        s.commit()
    _run(ingest.process_episode(ids[0], learning_source()))
    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "ready" and ep.published_at == published
    assert ep.title == "Rust ownership: Part 1 title"
    assert course_meta(ep.provenance)["part"] == 1
    assert calls[-1]["text"].startswith("Rust ownership: Part 1 title.")


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

    ep_id, _ = _aged("learning", "ret-feed:1")
    _run(cleanup_old_episodes(60))
    with db.session() as s:  # kept, not deleted: absent from the feed for the right reason
        assert s.get(Episode, ep_id).status == "archived"
    config = load_config()
    learning = next(s for s in config.sources if s.slug == "learning")
    assert "ret-feed:1" not in build_feed(config, "https://b", "tok", source=learning)


def test_admin_list_always_includes_archived_parts():
    """Archived parts are by construction the oldest rows, so a newest-N list
    never reaches them and their restore button is unreachable."""
    from app.web import _admin_episodes

    old_id, _ = _aged("learning", "fix-archived:1", status="archived", days_old=400)
    _aged("inbox", "https://fix.example/newer", status="skipped", days_old=0)
    ids = [e.id for e in _admin_episodes(limit=1)]
    assert old_id in ids
    assert len(ids) == len(set(ids))


def test_verbatim_script_skips_the_light_scrub(monkeypatch):
    """scrub_light strips #, _, * and [n]: a programming book read verbatim
    would say "C" for C# and "init" for __init__."""
    from app.courses import create_course, parse_course, render_course

    calls = []
    _fake_tts(monkeypatch, calls)
    code = " In C# you write __init__ and items[0] or a * b."
    data = _payload("fix-verbatim", n=2, kind="audiobook")
    data["episodes"][0].update(script=SCRIPT + code, verbatim=True)
    data["episodes"][1].update(script=SCRIPT + code)  # not verbatim: scrubbed as before
    _run(render_course(create_course(parse_course(data)), "t"))
    assert code.strip() in calls[0]["text"]
    assert "C#" not in calls[1]["text"]


# ── Minor fixes from the final review ──────────────────────────────────────

def test_url_part_show_notes_include_the_course_notes(monkeypatch):
    """The outro promises code and links in the show notes; a URL part's notes
    are the article's, so the course notes must be appended there too."""
    from app import ingest
    from app.courses import create_course, parse_course, render_course

    calls = []
    _fake_tts(monkeypatch, calls)

    async def page(url):
        return ("<html><body><article>" + "".join(
            f"<p>Paragraph {i}: shaping sets boundaries on project {i} before "
            "anyone commits to building it.</p>" for i in range(12))
            + "</article></body></html>")

    monkeypatch.setattr(ingest, "fetch_html", page)
    data = _payload("minor-notes", n=1, kind="audiobook")
    data["episodes"][0] = {"part": 1, "title": "Ch", "url": "https://book.example/n",
                           "show_notes_md": "See `cargo doc`."}
    ids = create_course(parse_course(data))
    _run(render_course(ids, "minor-notes"))
    with db.session() as s:
        ep = s.get(Episode, ids[0])
    assert ep.status == "ready", ep.error
    assert "<code>cargo doc</code>" in ep.description


def test_one_course_never_renders_two_parts_at_once(monkeypatch):
    """A client retry POSTs the same course twice; the two renders must queue
    behind each other, not run parts side by side."""
    from app import ingest
    from app.courses import render_course

    active = peak = 0

    async def fake_process(ep_id, source):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    monkeypatch.setattr(ingest, "process_episode", fake_process)

    async def both():
        await asyncio.gather(render_course([1, 2], "minor-lock"),
                             render_course([1, 2], "minor-lock"))

    _run(both())
    assert peak == 1


def test_course_status_does_not_treat_the_id_as_a_pattern(api):
    client, token, _ = api
    client.post(f"/{token}/api/course", json=_payload("esc-a", n=1))
    assert client.get(f"/{token}/api/course/esc%25").status_code == 404
    assert client.get(f"/{token}/api/course/esc_a").status_code == 404


def test_repost_reports_existing_parts_and_rejects_a_new_shape(api):
    client, token, _ = api
    client.post(f"/{token}/api/course", json=_payload("minor-repost", n=2))
    r = client.post(f"/{token}/api/course", json=_payload("minor-repost", n=2))
    assert r.status_code == 200 and r.json()["existing"] == [1, 2]
    r = client.post(f"/{token}/api/course", json=_payload("minor-repost", n=3))
    assert r.status_code == 400 and "already has 2 parts" in r.json()["detail"]


def test_markdown_heading_needs_a_space_and_links_skip_code():
    from app.courses import markdown_to_html

    assert "<h3>" not in markdown_to_html("#include <stdio.h>")
    html = markdown_to_html("[x](https://a.test/`b`)")
    assert 'href="https://a.test/<code>' not in html


def test_a_learning_override_stays_out_of_all_unless_asked(monkeypatch, tmp_path):
    from app import config

    (tmp_path / "sources.yaml").write_text(
        "sources:\n"
        "  - {slug: learning, name: Learning, type: learning, voice: en-GB-RyanNeural}\n"
        "  - {slug: learning2, name: L2, type: learning, in_all_feed: true}\n")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    by_slug = {s.slug: s for s in config.load_config().sources}
    assert by_slug["learning"].in_all_feed is False
    assert by_slug["learning2"].in_all_feed is True


def test_cleanup_counts_archived_parts_too():
    from app.ingest import cleanup_old_episodes

    _aged("learning", "minor-count:1", days_old=400)
    assert _run(cleanup_old_episodes(365)) >= 1


def test_episodes_are_kept_for_a_year_by_default(monkeypatch, tmp_path):
    from app import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)  # no sources.yaml at all
    assert config.load_config().retention_days == 365
    assert config.AppConfig().retention_days == 365


def test_a_one_part_course_does_not_say_part_1_of_1():
    """"Part 1 of 1" and "the final part" sound wrong on a single episode."""
    from app.courses import course_intro, course_outro

    one = _meta(part=1, parts=1)
    assert course_intro(one) == "Shape Up: Borrowing."
    assert course_outro(one) == "That's the end of Shape Up."
    da = _meta(part=1, parts=1, language="da")
    assert course_intro(da) == "Shape Up: Borrowing."
    assert course_outro(da) == "Det var Shape Up."
    assert course_outro(_meta(part=1, parts=1, show_notes_md="x")) == \
        "That's the end of Shape Up. There's more in the show notes."


def test_audiobook_framing_follows_the_book_not_the_part_number():
    """With a foreword sent as part 1, "chapter 2: Introduction" contradicted
    the book, and the last part sent claimed "The end of Shape Up" two
    chapters in. The chapter title carries the book's own numbering."""
    from app.courses import course_intro, course_outro

    intro_ch = _meta(kind="audiobook", part=2, parts=3,
                     part_title="Chapter 1: Introduction")
    assert course_intro(intro_ch) == "Shape Up: Chapter 1: Introduction."
    last = _meta(kind="audiobook", part=3, parts=3,
                 part_title="Chapter 2: Principles of Shaping")
    assert course_outro(last) == "End of Chapter 2: Principles of Shaping."
    assert course_outro({**last, "language": "da"}) == \
        "Slut på Chapter 2: Principles of Shaping."


@pytest.mark.parametrize("over, expected", [
    ({}, "Shape Up (2/4): Borrowing"),
    ({"part": 1, "parts": 1}, "Shape Up: Borrowing"),
    ({"kind": "audiobook", "part": 3, "parts": 3}, "Shape Up: Borrowing"),
])
def test_titles_only_count_parts_of_a_multi_part_course(over, expected):
    """"Shape Up (3/3)" read as the end of the book two chapters in, and
    "(1/1)" is noise; the count moves to the show notes instead."""
    from app.courses import episode_title

    assert episode_title(_meta(**over)) == expected


def test_show_notes_header_keeps_the_part_count():
    from app.courses import course_show_notes

    book = course_show_notes(_meta(kind="audiobook", part=3, parts=3))
    assert "<small>Part 3 of the 3 chapters sent so far</small>" in book
    one = course_show_notes(_meta(part=1, parts=1))
    assert "<small>Part 1 of 1</small>" in one


def test_script_part_show_notes_carry_the_transcript(monkeypatch):
    from app.courses import create_course, parse_course, render_course

    _fake_tts(monkeypatch, [])
    data = _payload("notes-transcript", n=1)
    data["episodes"][0]["script"] = SCRIPT + "\n\nA second paragraph about Vec<T>."
    ids = create_course(parse_course(data))
    _run(render_course(ids, "notes-transcript"))
    with db.session() as s:
        desc = s.get(Episode, ids[0]).description
    assert "<h3>Transcript</h3>" in desc
    assert "<p>A second paragraph about Vec&lt;T&gt;.</p>" in desc


def test_url_part_show_notes_have_the_course_header_and_the_article(monkeypatch):
    from app import ingest
    from app.courses import create_course, parse_course, render_course

    _fake_tts(monkeypatch, [])

    async def page(url):
        return ("<html><body><article>" + "".join(
            f"<p>Paragraph {i}: shaping sets boundaries on project {i} before "
            "anyone commits to building it.</p>" for i in range(12))
            + "</article></body></html>")

    monkeypatch.setattr(ingest, "fetch_html", page)
    data = _payload("notes-url", n=1, kind="audiobook", title="Shape Up")
    data["episodes"][0] = {"part": 1, "title": "Chapter 1: Intro",
                           "url": "https://book.example/notes"}
    ids = create_course(parse_course(data))
    _run(render_course(ids, "notes-url"))
    with db.session() as s:
        desc = s.get(Episode, ids[0]).description
    assert desc.startswith("<p><strong>Shape Up</strong>: Chapter 1: Intro<br/>")
    assert "Paragraph 11: shaping sets boundaries" in desc


# ── feed compression: feeds carry full show notes and run to megabytes ──────

def test_feed_is_gzipped_when_the_client_accepts_it(api):
    import gzip

    import httpx

    from app import web

    client, token, _ = api
    plain = client.get(f"/{token}/feeds/learning.xml", headers={"Accept-Encoding": "identity"})
    assert plain.status_code == 200 and "content-encoding" not in plain.headers
    assert plain.headers["vary"] == "Accept-Encoding"
    # Ask for the raw bytes: the test client would otherwise decompress them.
    transport = httpx.ASGITransport(app=web.app)

    async def raw():
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
            req = c.build_request("GET", f"/{token}/feeds/learning.xml",
                                  headers={"Accept-Encoding": "gzip, br"})
            resp = await c.send(req, stream=True)
            body = b"".join([chunk async for chunk in resp.aiter_raw()])
            return resp, body

    resp, body = _run(raw())
    assert resp.headers["content-encoding"] == "gzip"
    assert resp.headers["content-type"].startswith("application/rss+xml")
    assert gzip.decompress(body) == plain.content


def test_feed_respects_a_gzip_refusal(api):
    client, token, _ = api
    r = client.get(f"/{token}/feeds/learning.xml", headers={"Accept-Encoding": "gzip;q=0, identity"})
    assert "content-encoding" not in r.headers
