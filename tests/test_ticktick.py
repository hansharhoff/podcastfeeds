"""TickTick phase 2: queue model, kind detection, poller, generation routing."""
import asyncio
import json
import logging

import pytest
from sqlmodel import select

from app import db, ticktick
from app.config import SourceDef, load_config
from app.db import Episode, TickTickItem
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


def test_missing_watched_list_skips_auto_dismiss(monkeypatch):
    """Two watched lists configured; a poll where one has vanished from the
    /project listing entirely (renamed/archived, or a partial 200) must not
    auto-dismiss anything — not even tasks from the list that's still present
    (spec §1: auto-dismiss only runs when EVERY watched list was fetched)."""
    ticktick.TOKENS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ticktick.TOKENS_FILE.write_text(json.dumps(
        {"access_token": "tok", "lists": ["Z Reading", "Y Papers"]}
    ))
    _clear_queue()
    projects = [{"id": "p1", "name": "Z Reading"}, {"id": "p2", "name": "Y Papers"}]
    t1 = {"id": "t1", "title": "https://example.com/a", "status": 0,
          "createdTime": "2026-07-20T10:00:00.000+0000"}
    t2 = {"id": "t2", "title": "https://example.com/b", "status": 0,
          "createdTime": "2026-07-20T10:00:00.000+0000"}
    monkeypatch.setattr(ticktick.httpx, "AsyncClient",
                        _FakeClient(projects, {"p1": [t1], "p2": [t2]}))
    _run(ticktick.poll_ticktick())
    with db.session() as s:
        rows = {r.task_id: r for r in s.exec(select(TickTickItem)).all()}
    assert rows["t1"].status == "queued" and rows["t2"].status == "queued"

    # Y Papers disappears from /project (only Z Reading is returned this time),
    # and t1 (still in Z Reading) is also gone from that list's tasks. Because
    # a watched list is missing, all_lists_ok must be False: neither t1's nor
    # t2's absence from open_ids may be trusted, so both must stay queued.
    projects_missing = [{"id": "p1", "name": "Z Reading"}]
    monkeypatch.setattr(ticktick.httpx, "AsyncClient",
                        _FakeClient(projects_missing, {"p1": []}))
    _run(ticktick.poll_ticktick())
    with db.session() as s:
        rows = {r.task_id: r for r in s.exec(select(TickTickItem)).all()}
    assert rows["t1"].status == "queued"  # would be wrongly auto-dismissed pre-fix
    assert rows["t2"].status == "queued"  # from the vanished list — must survive


# ── process_episode: PDF narration path behind SourceDef.allow_pdf ─────────

def test_process_episode_narrates_pdf_when_allowed(monkeypatch):
    from app import ingest
    from app.db import Episode

    async def fake_fetch_pdf(url):
        # (title, text); >400 chars -> summary branch
        return "Doc Title", "Deep learning content. " * 40

    async def fake_article_summary(title, body, language, link):
        return "A short spoken summary.", "notes line", {"generator": "test"}

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 12345, 60

    monkeypatch.setattr(ingest, "fetch_pdf", fake_fetch_pdf)
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
    # the PDF's own title is a fallback, not an override
    assert "A paper" in done.title


def test_process_episode_uses_the_pdf_title_when_the_share_had_none(monkeypatch):
    """A PDF shared by hand carries no page title, so the episode used to stay
    "Untitled" and the narration opened by saying so (ep. 403)."""
    from app import ingest
    from app.db import Episode

    async def fake_fetch_pdf(url):
        return "Security Now! Special Edition - GRAM", "Deep learning content. " * 40

    async def fake_article_summary(title, body, language, link):
        return "A short spoken summary.", "notes line", {"generator": "test"}

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 12345, 60

    monkeypatch.setattr(ingest, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.setattr(ingest, "article_summary", fake_article_summary)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid="https://example.com/untitled.pdf",
                     title="Untitled", link="https://example.com/untitled.pdf")
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
    assert "Security Now! Special Edition - GRAM" in done.title
    assert "Untitled" not in done.title


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


def test_book_brief_verdict_brief_strips_verdict_line(monkeypatch):
    """A normal 'VERDICT: brief' reply still narrates — but the machine-
    readable verdict line itself must never reach the listener."""
    from app import ingest, summarize
    from app.db import Episode

    async def fake_llm(prompt, **kwargs):
        assert "VERDICT" in prompt  # the new protocol is asked for
        return "VERDICT: brief\nThis is a brief about a real book. ..."

    processed = {}

    async def fake_process_episode(ep_id, source):
        processed["ep_id"] = ep_id

    monkeypatch.setattr(summarize, "llm", fake_llm)
    monkeypatch.setattr(ingest, "process_episode", fake_process_episode)
    item_id = _queue_item(kind="book", title="A Real Book", task_id="t-book-verdict-1")
    config = load_config()
    inbox = next(s_ for s_ in config.sources if s_.type == "inbox")
    ep_id = ticktick._create_episode(inbox.slug, "ticktick:t-book-verdict-1",
                                     "A Real Book", "")
    _run(ticktick._render_book_brief(ep_id, item_id, inbox, "A Real Book", ""))
    with db.session() as s:
        ep = s.get(Episode, ep_id)
    assert "VERDICT" not in ep.source_text
    assert "brief about a real book" in ep.source_text
    assert processed["ep_id"] == ep_id


def test_book_brief_not_a_book_verdict_creates_no_episode_and_proposes(monkeypatch):
    """'example web tool' / 'example build guide' bug: when the LLM says
    honestly that a reading-list reference isn't a book, the pipeline must not
    turn that demurral into a published dud episode. Instead: no episode, item
    back to queued, and an actionable proposal in the queue."""
    from app import ingest, summarize
    from app.db import Episode

    async def fake_llm(prompt, **kwargs):
        return ("VERDICT: not-a-book — looks like a website/tool. Suggested: "
                "retag as article and supply a URL.")

    def boom(*a, **k):
        raise AssertionError("process_episode must not run for a not-a-book verdict")

    monkeypatch.setattr(summarize, "llm", fake_llm)
    monkeypatch.setattr(ingest, "process_episode", boom)
    item_id = _queue_item(kind="book", title="example web tool", task_id="t-book-notabook-1")
    config = load_config()
    inbox = next(s_ for s_ in config.sources if s_.type == "inbox")
    ep_id = ticktick._create_episode(inbox.slug, "ticktick:t-book-notabook-1",
                                     "example web tool", "")
    _run(ticktick._render_book_brief(ep_id, item_id, inbox, "example web tool", ""))
    with db.session() as s:
        ep = s.get(Episode, ep_id)
        item = s.get(TickTickItem, item_id)
    assert ep is None  # no episode left behind
    assert item.status == "queued"
    assert item.episode_id is None
    assert item.last_error == ""
    assert "website/tool" in item.proposal
    assert "retag as article" in item.proposal


def test_retag_article_route_flips_kind():
    """The queue's 'retag as article' action for a not-a-book proposal: just
    flips kind, so Hans can supply/confirm a URL and Generate next."""
    from app.config import get_token
    from app.web import api_ticktick_retag_article

    item_id = _queue_item(kind="book", title="example web tool", task_id="t-retag-1",
                          status="queued")
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
        item.proposal = "Not a book — looks like a website/tool."
        s.add(item)
        s.commit()
    _run(api_ticktick_retag_article(get_token(), item_id))
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
    assert item.kind == "article"


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


# ── _ticktick_queue_rows: admin-queue row building (app/web.py) ────────────
#    Pure-helper extraction so the "generated but the episode secretly failed"
#    seam is testable without HTTP (spec §5: failed generates stay visible).

def test_queue_rows_surfaces_skipped_episode_not_just_error():
    """A book brief for an unknown book can produce an honest, short "I'm not
    confident I know this book" reply. That trips the RSS incident-32
    looks_meta() guard in app/ingest.py (len(body) < 200 -> source_text
    filtered out as meta-commentary), so the episode lands as status
    'skipped' with no error of its own — not 'error'. Pre-fix, the queue view
    only resurfaced status == 'error', so a 'generated' item whose episode is
    merely 'skipped' vanished from the queue with no episode and no trace."""
    from app.web import _ticktick_queue_rows

    item = TickTickItem(task_id="t-skip-1", project="Z Reading", title="Some Book",
                        kind="book", status="generated", episode_id=7,
                        last_error="")
    ep = Episode(id=7, source_slug="inbox", guid="ticktick:t-skip-1",
                title="Some Book", status="skipped",
                error="no article content (discussion/thread or link-only post)")
    rows = _ticktick_queue_rows([item], {7: ep})
    assert len(rows) == 1
    assert rows[0]["item"] is item
    assert rows[0]["error"] == ep.error


def test_queue_rows_still_surfaces_error_episode():
    from app.web import _ticktick_queue_rows

    item = TickTickItem(task_id="t-err-1", project="Z Reading", title="A post",
                        kind="article", status="generated", episode_id=8,
                        last_error="")
    ep = Episode(id=8, source_slug="inbox", guid="https://example.com/x",
                title="A post", status="error", error="fetch failed")
    rows = _ticktick_queue_rows([item], {8: ep})
    assert len(rows) == 1 and rows[0]["error"] == "fetch failed"


def test_queue_rows_hides_generated_item_with_healthy_episode():
    from app.web import _ticktick_queue_rows

    item = TickTickItem(task_id="t-ok-1", project="Z Reading", title="A post",
                        kind="article", status="generated", episode_id=9)
    ep = Episode(id=9, source_slug="inbox", guid="https://example.com/y",
                title="A post", status="ready")
    assert _ticktick_queue_rows([item], {9: ep}) == []


def test_queue_rows_keeps_still_queued_item():
    from app.web import _ticktick_queue_rows

    item = TickTickItem(task_id="t-q-1", project="Z Reading", title="A post",
                        kind="article", status="queued", last_error="boom")
    rows = _ticktick_queue_rows([item], {})
    assert len(rows) == 1 and rows[0]["error"] == "boom"


def test_queue_rows_surfaces_proposal():
    """A not-a-book verdict leaves the item queued with no error but a
    proposal — the row builder must surface it so the admin page can render
    it distinctly from a plain failure."""
    from app.web import _ticktick_queue_rows

    item = TickTickItem(task_id="t-prop-1", project="Z Reading", title="example web tool",
                        kind="book", status="queued", last_error="",
                        proposal="Not a book — looks like a website/tool. "
                                 "Suggested: retag as article and supply a URL.")
    rows = _ticktick_queue_rows([item], {})
    assert len(rows) == 1
    assert rows[0]["error"] == ""
    assert "retag as article" in rows[0]["proposal"]


# ── redo/unskip on a queue-generated episode must re-run the kind-routed
#    generate path, not the generic requeue (eps 330/332/337, 2026-07-31).
#    Those episodes carry no link, so process_episode fell into its no-link
#    fallback and re-narrated the stale brief already on the row — which also
#    meant a pre-fix-3 dud could never pick up the not-a-book verdict. ──

def test_redo_of_queue_episode_routes_through_generate_item(monkeypatch):
    from app import ticktick as tt
    from app import web

    called = {}

    async def fake_generate_item(item_id, mode="summary"):
        called["item_id"] = item_id
        return 1

    monkeypatch.setattr(tt, "generate_item", fake_generate_item)
    # Run the spawned coroutine inline so the routing is actually exercised.
    monkeypatch.setattr(
        web, "spawn",
        lambda coro: asyncio.new_event_loop().run_until_complete(coro))

    with db.session() as s:
        ep = Episode(source_slug="inbox", guid="ticktick:redo-1",
                     title="example build guide", link="")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id
        item = TickTickItem(task_id="redo-1", project="Z Reading",
                            title="example build guide", kind="book",
                            status="generated", episode_id=ep_id)
        s.add(item)
        s.commit()
        s.refresh(item)
        item_id = item.id

    assert web._regenerate_from_queue(ep_id) == item_id
    assert called["item_id"] == item_id


def test_redo_of_ordinary_episode_does_not_touch_the_queue(monkeypatch):
    from app import web

    with db.session() as s:
        ep = Episode(source_slug="inbox", guid="https://example.com/plain",
                     title="An article", link="https://example.com/plain")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    assert web._regenerate_from_queue(ep_id) is None


def test_redo_does_not_stack_the_source_label_on_the_title(monkeypatch):
    """A redo re-reads ep.title, which already carries the label; without the
    strip it stacked ("grc.com: grc.com: Untitled", ep. 403)."""
    from app import ingest
    from app.db import Episode

    async def fake_fetch_pdf(url):
        return "", "Deep learning content. " * 40

    async def fake_article_summary(title, body, language, link):
        return "A short spoken summary.", "notes line", {"generator": "test"}

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 12345, 60

    monkeypatch.setattr(ingest, "fetch_pdf", fake_fetch_pdf)
    monkeypatch.setattr(ingest, "article_summary", fake_article_summary)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    source = SourceDef(**{**inbox.__dict__, "allow_pdf": True,
                          "narrate_mode": "summary", "voice": ""})
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid="https://grc.com/restack.pdf",
                     title="A Paper", link="https://grc.com/restack.pdf")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, source))
    with db.session() as s:
        first = s.get(Episode, ep_id).title

    # redo: the row now holds the labelled title, exactly as api_redo re-reads it
    with db.session() as s:
        ep = s.get(Episode, ep_id)
        ep.status = "pending"
        s.add(ep)
        s.commit()
    _run(ingest.process_episode(ep_id, source))
    with db.session() as s:
        second = s.get(Episode, ep_id).title

    assert first == second
    assert second.count("grc.com") == 1
