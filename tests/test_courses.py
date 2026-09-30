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
