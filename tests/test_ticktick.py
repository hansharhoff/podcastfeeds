"""TickTick phase 2: queue model, kind detection, poller, generation routing."""
import asyncio
import json

from sqlmodel import select

from app import db, ticktick
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


def test_poll_never_writes_to_ticktick(monkeypatch):
    """Read-only contract: the poller must never POST (no task completion)."""
    _write_conf()
    _clear_queue()
    client = _FakeClient([{"id": "p1", "name": "Z Reading"}], {"p1": []})
    client.post = None  # any attempted POST raises TypeError
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", client)
    _run(ticktick.poll_ticktick())  # must not raise


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
