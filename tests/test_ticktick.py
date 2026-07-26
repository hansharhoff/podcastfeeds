"""TickTick phase 2: queue model, kind detection, poller, generation routing."""
import asyncio
import json
import logging

import pytest
from sqlmodel import select

from app import db, ticktick
from app.config import SourceDef, load_config
from app.db import TickTickItem
from app.ticktick import detect_kind, pdf_url


def test_ticktick_item_roundtrip():
    with db.session() as s:
        item = TickTickItem(
            task_id="t-abc123", project="Z Reading", title="Some paper",
            notes="worth a look", url="https://arxiv.org/abs/2401.00001",
            kind="pdf",
        )
        s.add(item)
        s.commit()
        s.refresh(item)
        assert item.id is not None
        assert item.status == "queued"
        assert item.episode_id is None
        assert item.last_error == ""
        assert item.first_seen is not None


def test_detect_kind():
    assert detect_kind("") == "book"  # title-only book reference
    assert detect_kind("https://example.com/paper.pdf") == "pdf"
    assert detect_kind("https://example.com/Paper.PDF?dl=1") == "pdf"
    assert detect_kind("https://arxiv.org/abs/2401.00001") == "pdf"
    assert detect_kind("https://www.arxiv.org/pdf/2401.00001") == "pdf"
    assert detect_kind("https://noahpinion.blog/p/some-post") == "article"


def test_pdf_url_arxiv_abs_becomes_pdf():
    assert pdf_url("https://arxiv.org/abs/2401.00001") == "https://arxiv.org/pdf/2401.00001"
    # non-arxiv URLs pass through untouched
    assert pdf_url("https://example.com/paper.pdf") == "https://example.com/paper.pdf"


# ── poll_ticktick: read-only upsert into the admin approval queue ──
#    (async-test convention mirrored from test_substack.py: no pytest-asyncio/
#    anyio plugin is installed, so async tests run via a local _run() helper
#    rather than a pytest.mark.anyio marker.)

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """Stands in for httpx.AsyncClient inside poll_ticktick."""

    def __init__(self, projects, tasks_by_pid, data_status=200):
        self._projects, self._tasks, self._data_status = projects, tasks_by_pid, data_status
        self.posted: list[str] = []  # any attempted POST is recorded here, then raises

    def __call__(self, *args, **kwargs):  # the code calls httpx.AsyncClient(...)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if url.endswith("/project"):
            return _Resp(self._projects)
        pid = url.split("/project/")[1].split("/")[0]
        if self._data_status != 200:
            return _Resp({}, self._data_status)
        return _Resp({"tasks": self._tasks.get(pid, [])})

    async def post(self, url, *args, **kwargs):
        # Recorded *before* raising so the read-only assertion still catches a
        # reintroduced POST even though poll_ticktick's whole body runs inside
        # a bare try/except that would otherwise swallow this exception.
        self.posted.append(url)
        raise AssertionError(f"poll_ticktick attempted a POST to {url} (read-only contract)")


def _write_conf(tmp_token="tok"):
    ticktick.TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ticktick.TOKENS_FILE.write_text(json.dumps(
        {"access_token": tmp_token, "lists": ["Z Reading"]}
    ))


def _clear_queue():
    with db.session() as s:
        for row in s.exec(select(TickTickItem)).all():
            s.delete(row)
        s.commit()


def test_poll_upserts_open_tasks_and_dedupes(monkeypatch):
    _write_conf()
    _clear_queue()
    projects = [{"id": "p1", "name": "Z Reading"}]
    tasks = {"p1": [
        {"id": "t1", "title": "Read this https://example.com/a", "status": 0,
         "createdTime": "2026-07-20T10:00:00.000+0000"},
        {"id": "t2", "title": "The Power Broker", "content": "Caro biography",
         "status": 0, "createdTime": "2026-07-21T10:00:00.000+0000"},
        {"id": "t3", "title": "done already", "status": 2},
    ]}
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", _FakeClient(projects, tasks))
    assert _run(ticktick.poll_ticktick()) == 2  # t3 is completed -> ignored
    assert _run(ticktick.poll_ticktick()) == 0  # second poll: task_id dedup
    with db.session() as s:
        rows = {r.task_id: r for r in s.exec(select(TickTickItem)).all()}
    assert rows["t1"].kind == "article" and rows["t1"].url == "https://example.com/a"
    assert rows["t2"].kind == "book" and rows["t2"].url == ""
    assert rows["t2"].notes == "Caro biography"
    assert all(r.status == "queued" for r in rows.values())


def test_poll_never_writes_to_ticktick(monkeypatch, caplog):
    """Read-only contract: the poller must never POST (no task completion).

    Pinned two independent ways, because poll_ticktick's entire body runs
    inside a bare `try: ... except Exception: log.exception(...)` — a
    reintroduced `await client.post(...)` would raise (since _FakeClient.post
    always raises) but that exception would be swallowed and logged rather
    than propagating, so a bare "must not raise" assertion alone is a soft
    pin. Instead: (1) _FakeClient.post appends to `.posted` *before* raising,
    so the call is recorded even though the exception itself gets caught —
    asserting `.posted == []` catches a reintroduced POST regardless of the
    swallow; and (2) caplog confirms the poll's try/except never actually
    caught anything (i.e. nothing "failed" silently) during a supposedly
    clean, read-only poll.

    Uses a non-empty, still-open task list — not the empty list a prior draft
    used — so that a regression reintroducing the old per-task "mark
    complete" POST (which only fires while iterating actual open tasks) is
    exercised at all; an empty task list would let such a regression through
    for the wrong reason (the loop body simply never running), independent of
    whether the exception it raises is swallowed.
    """
    _write_conf()
    _clear_queue()
    tasks = {"p1": [{"id": "t-readonly-1", "title": "https://example.com/readonly",
                     "status": 0, "createdTime": "2026-07-20T10:00:00.000+0000"}]}
    client = _FakeClient([{"id": "p1", "name": "Z Reading"}], tasks)
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", client)
    with caplog.at_level(logging.ERROR, logger="podcastfeeds"):
        assert _run(ticktick.poll_ticktick()) == 1  # confirms the task loop actually ran
    assert client.posted == []
    assert "ticktick poll failed" not in caplog.text


def test_gone_task_auto_dismissed_only_on_clean_poll(monkeypatch):
    _write_conf()
    _clear_queue()
    projects = [{"id": "p1", "name": "Z Reading"}]
    t1 = {"id": "t1", "title": "https://example.com/a", "status": 0,
          "createdTime": "2026-07-20T10:00:00.000+0000"}
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", _FakeClient(projects, {"p1": [t1]}))
    _run(ticktick.poll_ticktick())
    # A failing list fetch must NOT dismiss anything (API blip guard, spec §1).
    monkeypatch.setattr(ticktick.httpx, "AsyncClient",
                        _FakeClient(projects, {"p1": []}, data_status=500))
    _run(ticktick.poll_ticktick())
    with db.session() as s:
        assert s.exec(select(TickTickItem)).first().status == "queued"
    # A clean poll where the task is gone (Hans completed it in TickTick) dismisses.
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", _FakeClient(projects, {"p1": []}))
    _run(ticktick.poll_ticktick())
    with db.session() as s:
        assert s.exec(select(TickTickItem)).first().status == "dismissed"


# ── process_episode: PDF narration path behind SourceDef.allow_pdf ─────────

def test_process_episode_narrates_pdf_when_allowed(monkeypatch):
    from app import ingest
    from app.db import Episode

    async def fake_fetch_pdf_text(url):
        return "Deep learning content. " * 40  # >400 chars -> summary branch

    async def fake_article_summary(title, body, language, link):
        return "A short spoken summary.", "notes line", {"generator": "test"}

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 12345, 60

    monkeypatch.setattr(ingest, "fetch_pdf_text", fake_fetch_pdf_text)
    monkeypatch.setattr(ingest, "article_summary", fake_article_summary)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid="https://example.com/x.pdf",
                     title="A paper", link="https://example.com/x.pdf")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    source = SourceDef(**{**inbox.__dict__, "allow_pdf": True,
                          "narrate_mode": "summary", "voice": ""})
    _run(ingest.process_episode(ep_id, source))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    assert "summary" in done.provenance


def test_process_episode_still_skips_pdf_without_allow_pdf():
    from app import ingest
    from app.db import Episode

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid="https://example.com/y.pdf",
                     title="Another paper", link="https://example.com/y.pdf")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))  # inbox has allow_pdf=False

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "skipped"
    assert "PDF" in done.error


# ── generate_item: kind-routed generation, triggered only by Hans' click ───
#    (async-test convention: see _run() above — no pytest.mark.anyio here.)

def _queue_item(**over):
    defaults = {
        "task_id": f"t-{over.get('kind', 'x')}-{id(over)}", "project": "Z Reading",
        "title": "Item", "notes": "", "url": "", "kind": "book", "status": "queued",
    }
    defaults.update(over)
    with db.session() as s:
        item = TickTickItem(**defaults)
        s.add(item)
        s.commit()
        s.refresh(item)
        return item.id


def test_generate_article_routes_to_submit_url(monkeypatch):
    calls = {}

    async def fake_submit_url(url, title="", language="auto"):
        calls["url"], calls["title"] = url, title
        return 42

    monkeypatch.setattr(ticktick, "submit_url", fake_submit_url)
    item_id = _queue_item(kind="article", url="https://example.com/post", title="A post")
    ep_id = _run(ticktick.generate_item(item_id))
    assert ep_id == 42 and calls["url"] == "https://example.com/post"
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
    assert item.status == "generated" and item.episode_id == 42


def test_generate_pdf_spawns_allow_pdf_source(monkeypatch):
    spawned = {}

    def fake_spawn(coro):
        spawned["coro"] = coro
        coro.close()  # don't actually run the pipeline in this test

    monkeypatch.setattr(ticktick, "spawn", fake_spawn)
    item_id = _queue_item(kind="pdf", url="https://arxiv.org/abs/2401.00001",
                          title="A paper", task_id="t-pdf-1")
    ep_id = _run(ticktick.generate_item(item_id, mode="full"))
    assert "coro" in spawned
    with db.session() as s:
        from app.db import Episode
        ep = s.get(Episode, ep_id)
        item = s.get(TickTickItem, item_id)
    assert ep.link == "https://arxiv.org/pdf/2401.00001"  # abs -> pdf
    assert ep.status == "pending"
    assert item.status == "generated" and item.episode_id == ep_id


def test_book_brief_renders_via_source_text(monkeypatch):
    from app import ingest, summarize
    from app.db import Episode

    async def fake_llm(prompt, **kwargs):
        assert "The Power Broker" in prompt
        return "This is a brief about The Power Broker, not the book itself. ..."

    processed = {}

    async def fake_process_episode(ep_id, source):
        processed["ep_id"] = ep_id

    monkeypatch.setattr(summarize, "llm", fake_llm)
    monkeypatch.setattr(ingest, "process_episode", fake_process_episode)
    item_id = _queue_item(kind="book", title="The Power Broker",
                          notes="Caro", task_id="t-book-1")
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
        task_id = item.task_id
    config = load_config()
    inbox = next(s_ for s_ in config.sources if s_.type == "inbox")
    ep_id = ticktick._create_episode(inbox.slug, f"ticktick:{task_id}",
                                     "The Power Broker", "")
    _run(ticktick._render_book_brief(ep_id, item_id, inbox,
                                      "The Power Broker", "Caro"))
    with db.session() as s:
        ep = s.get(Episode, ep_id)
    assert "brief about The Power Broker" in ep.source_text
    assert processed["ep_id"] == ep_id


def test_book_brief_failure_requeues_item(monkeypatch):
    from app import summarize
    from app.db import Episode

    async def boom(prompt, **kwargs):
        raise RuntimeError("shim down")

    monkeypatch.setattr(summarize, "llm", boom)
    item_id = _queue_item(kind="book", title="Some Book", task_id="t-book-2")
    config = load_config()
    inbox = next(s_ for s_ in config.sources if s_.type == "inbox")
    ep_id = ticktick._create_episode(inbox.slug, "ticktick:t-book-2", "Some Book", "")
    _run(ticktick._render_book_brief(ep_id, item_id, inbox, "Some Book", ""))
    with db.session() as s:
        ep = s.get(Episode, ep_id)
        item = s.get(TickTickItem, item_id)
    assert ep.status == "error"
    assert item.status == "queued" and "shim down" in item.last_error


def test_generate_dismissed_item_refused():
    item_id = _queue_item(kind="article", url="https://example.com/z",
                          status="dismissed", task_id="t-dis-1")
    with pytest.raises(ValueError):
        _run(ticktick.generate_item(item_id))


def test_generate_missing_item_refused():
    with pytest.raises(ValueError):
        _run(ticktick.generate_item(999999))


def test_generate_article_failure_requeues_item(monkeypatch):
    async def boom(url, title="", language="auto"):
        raise RuntimeError("boom")

    monkeypatch.setattr(ticktick, "submit_url", boom)
    item_id = _queue_item(kind="article", url="https://example.com/post",
                          title="A post", task_id="t-art-fail-1")
    with pytest.raises(RuntimeError):
        _run(ticktick.generate_item(item_id))
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
    assert item.status == "queued"
    assert "boom" in item.last_error
