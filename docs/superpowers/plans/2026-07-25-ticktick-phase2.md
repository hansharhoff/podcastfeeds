# TickTick Phase 2: Approval Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace TickTick auto-generation with a read-only poller feeding an admin approval queue; approved items generate episodes routed by kind (article / PDF / book brief).

**Architecture:** New `TickTickItem` table keyed on the TickTick task id (dedup; the watermark kv becomes obsolete). The 5-min poller only upserts open tasks. A new admin-page section lists queued items with Generate/Dismiss; Generate routes: article → existing `submit_url`, PDF → new pypdf extract path through `process_episode` (summary default, full-read option), book → LLM brief injected as `source_text` and narrated by the normal pipeline.

**Tech Stack:** FastAPI + SQLModel/SQLite, httpx, pypdf (new), Jinja2, pytest.

**Spec:** `docs/superpowers/specs/2026-07-25-ticktick-phase2-design.md` — read it first.

## Global Constraints

- Repo is PUBLIC (github.com/hansharhoff/podcastfeeds): never commit secrets; `config/sources.yaml`, `config/secrets.yaml`, `data/` are git-ignored — stage files explicitly, never `git add -A`.
- Gate every commit on: `.venv/bin/ruff check app/ scripts/ tests/ && .venv/bin/pytest -q` (both must pass).
- No episode is ever created without an explicit Generate click; the poller must not call `submit_url` or complete TickTick tasks.
- Iteration rules: never republish/redo existing episodes; deploying code is fine.
- Tests make no live network calls (TickTick, PDFs, LLM all faked).
- Match existing code style: module-level `log = logging.getLogger("podcastfeeds")`, comments explain *why*, tight helper functions.

---

### Task 1: `TickTickItem` model

**Files:**
- Modify: `app/db.py` (add model next to `Episode`)
- Test: `tests/test_ticktick.py` (new file)

**Interfaces:**
- Produces: `db.TickTickItem` with fields exactly as below — later tasks import it as `from .db import TickTickItem`.

- [ ] **Step 1: Write the failing test**

```python
"""TickTick phase 2: queue model, kind detection, poller, generation routing."""
from app import db
from app.db import TickTickItem


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: FAIL with `ImportError: cannot import name 'TickTickItem'`

- [ ] **Step 3: Write minimal implementation**

In `app/db.py`, after the `Episode` class (keep `KV` last):

```python
class TickTickItem(SQLModel, table=True):
    """A task seen on a watched TickTick list, awaiting Hans' generate/dismiss
    call in the admin queue. task_id is the dedup key — it replaces the old
    watermark, so the pre-existing backlog imports on the first poll."""
    id: int | None = Field(default=None, primary_key=True)
    task_id: str = Field(index=True, unique=True)  # TickTick task id
    project: str = ""  # list name, e.g. "Z Reading"
    title: str = ""
    notes: str = ""  # task content + desc, concatenated
    url: str = ""  # first URL found in title/notes; "" for book references
    kind: str = "article"  # article|pdf|book (heuristic)
    task_created: datetime | None = None  # TickTick createdTime
    first_seen: datetime = Field(default_factory=utcnow)
    status: str = Field(default="queued", index=True)  # queued|generated|dismissed
    episode_id: int | None = None  # set once generated
    last_error: str = ""  # last generate failure, shown inline in the queue
```

Note: `url` is `str = ""` not `str | None` — matches the codebase's empty-string convention (`Episode.link`).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: PASS (SQLModel `create_all` creates new tables automatically; the additive `_migrate` only concerns episode columns and needs no change).

- [ ] **Step 5: Ruff + full suite, then commit**

Run: `.venv/bin/ruff check app/ scripts/ tests/ && .venv/bin/pytest -q`

```bash
git add app/db.py tests/test_ticktick.py
git commit -m "TickTick phase 2: TickTickItem queue model"
```

---

### Task 2: Kind detection + PDF URL normalization

**Files:**
- Modify: `app/ticktick.py`
- Test: `tests/test_ticktick.py`

**Interfaces:**
- Produces: `detect_kind(url: str) -> str` (accepts `""`; returns `"article" | "pdf" | "book"`) and `pdf_url(url: str) -> str` in `app/ticktick.py`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ticktick.py`:

```python
from app.ticktick import detect_kind, pdf_url


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: FAIL with `ImportError: cannot import name 'detect_kind'`

- [ ] **Step 3: Implement**

In `app/ticktick.py` (add `from urllib.parse import urlparse` to the imports):

```python
def detect_kind(url: str) -> str:
    """Queue-item kind heuristic (spec §1): no URL means a book reference;
    .pdf paths and arxiv links are papers; everything else is an article."""
    if not url:
        return "book"
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if parsed.path.lower().endswith(".pdf") or host == "arxiv.org":
        return "pdf"
    return "article"


def pdf_url(url: str) -> str:
    """The direct-download URL for a kind=pdf item: arxiv abstract pages
    map to their PDF; anything else is assumed to already be the PDF."""
    parsed = urlparse(url)
    if parsed.netloc.lower().removeprefix("www.") == "arxiv.org" and "/abs/" in parsed.path:
        return url.replace("/abs/", "/pdf/")
    return url
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: PASS

- [ ] **Step 5: Ruff + full suite, then commit**

```bash
git add app/ticktick.py tests/test_ticktick.py
git commit -m "TickTick phase 2: kind heuristic + arxiv pdf_url helper"
```

---

### Task 3: Read-only poller — upsert + guarded auto-dismiss

**Files:**
- Modify: `app/ticktick.py` (rewrite `poll_ticktick`; delete the watermark logic; keep `_parse_dt`, `_load`, `URL_RE`, `API`, `TOKENS_FILE`)
- Test: `tests/test_ticktick.py`

**Interfaces:**
- Consumes: `TickTickItem` (Task 1), `detect_kind` (Task 2).
- Produces: `poll_ticktick() -> int` (count of newly queued items) — signature unchanged, so `app/scheduler.py` needs no edit.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ticktick.py`. The fake mirrors how `poll_ticktick` uses httpx: `AsyncClient(...)` as async context manager, `.get()` returning an object with `.status_code`, `.json()`, `.raise_for_status()`.

```python
import json

import pytest
from sqlmodel import select

from app import ticktick
from app.db import TickTickItem


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


@pytest.mark.anyio
async def test_poll_upserts_open_tasks_and_dedupes(monkeypatch):
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
    assert await ticktick.poll_ticktick() == 2  # t3 is completed -> ignored
    assert await ticktick.poll_ticktick() == 0  # second poll: task_id dedup
    with db.session() as s:
        rows = {r.task_id: r for r in s.exec(select(TickTickItem)).all()}
    assert rows["t1"].kind == "article" and rows["t1"].url == "https://example.com/a"
    assert rows["t2"].kind == "book" and rows["t2"].url == ""
    assert rows["t2"].notes == "Caro biography"
    assert all(r.status == "queued" for r in rows.values())


@pytest.mark.anyio
async def test_poll_never_writes_to_ticktick(monkeypatch):
    """Read-only contract: the poller must never POST (no task completion)."""
    _write_conf()
    _clear_queue()
    client = _FakeClient([{"id": "p1", "name": "Z Reading"}], {"p1": []})
    client.post = None  # any attempted POST raises TypeError
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", client)
    await ticktick.poll_ticktick()  # must not raise


@pytest.mark.anyio
async def test_gone_task_auto_dismissed_only_on_clean_poll(monkeypatch):
    _write_conf()
    _clear_queue()
    projects = [{"id": "p1", "name": "Z Reading"}]
    t1 = {"id": "t1", "title": "https://example.com/a", "status": 0,
          "createdTime": "2026-07-20T10:00:00.000+0000"}
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", _FakeClient(projects, {"p1": [t1]}))
    await ticktick.poll_ticktick()
    # A failing list fetch must NOT dismiss anything (API blip guard, spec §1).
    monkeypatch.setattr(ticktick.httpx, "AsyncClient",
                        _FakeClient(projects, {"p1": []}, data_status=500))
    await ticktick.poll_ticktick()
    with db.session() as s:
        assert s.exec(select(TickTickItem)).first().status == "queued"
    # A clean poll where the task is gone (Hans completed it in TickTick) dismisses.
    monkeypatch.setattr(ticktick.httpx, "AsyncClient", _FakeClient(projects, {"p1": []}))
    await ticktick.poll_ticktick()
    with db.session() as s:
        assert s.exec(select(TickTickItem)).first().status == "dismissed"
```

Check `pyproject.toml` for the async test plugin already in use (`test_substack.py` has async tests — copy its marker/fixture convention exactly; if it uses `pytest.mark.asyncio` or an `anyio_backend` fixture, mirror that instead of the `anyio` marker above).

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: the three new tests FAIL (poll still submits URLs / uses watermark).

- [ ] **Step 3: Rewrite `poll_ticktick`**

Replace the body of `poll_ticktick` in `app/ticktick.py` (delete the watermark block, the `submit_url` call, the task-complete POST, and the now-unused `from .ingest import submit_url` import; update the module docstring to say items queue for admin approval instead of auto-generating):

```python
async def poll_ticktick() -> int:
    """Upsert open tasks from the watched lists into the admin approval queue.
    Read-only: never completes tasks, never generates episodes (spec:
    docs/superpowers/specs/2026-07-25-ticktick-phase2-design.md). Returns the
    number of newly queued items."""
    conf = _load()
    if not conf or not conf.get("access_token"):
        return 0
    wanted = conf.get("lists") or [conf.get("list") or "Podcast"]
    wanted_lc = {str(n).lower() for n in wanted}
    headers = {"Authorization": f"Bearer {conf['access_token']}"}
    new_items = 0
    open_ids: set[str] = set()
    all_lists_ok = True
    try:
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.get(f"{API}/project")
            if resp.status_code == 401:
                log.warning("ticktick: access token expired/invalid — rerun scripts/ticktick_auth.py")
                return 0
            resp.raise_for_status()
            projects = [p for p in resp.json() if p.get("name", "").lower() in wanted_lc]
            if not projects:
                log.warning("ticktick: none of the lists %r found", wanted)
                return 0
            from . import db
            with db.session() as s:
                for project in projects:
                    resp = await client.get(f"{API}/project/{project['id']}/data")
                    if resp.status_code != 200:
                        all_lists_ok = False
                        continue
                    for task in resp.json().get("tasks") or []:
                        if task.get("status"):  # completed in TickTick
                            continue
                        open_ids.add(task["id"])
                        new_items += _upsert(s, project.get("name", ""), task)
                # Only a poll that saw every watched list may conclude a queued
                # item's task is gone — an API blip must not wipe the queue.
                if all_lists_ok:
                    _auto_dismiss(s, open_ids)
    except Exception:
        log.exception("ticktick poll failed")
    return new_items


def _upsert(s, project_name: str, task: dict) -> int:
    """Queue an unseen open task; task_id dedup makes re-polls no-ops.
    Returns 1 if a new item was queued."""
    from sqlmodel import select

    from .db import TickTickItem

    task_id = task["id"]
    if s.exec(select(TickTickItem).where(TickTickItem.task_id == task_id)).first():
        return 0
    title = (task.get("title") or "").strip()
    notes = " ".join(p for p in (task.get("content"), task.get("desc")) if p).strip()
    match = URL_RE.search(f"{title} {notes}")
    url = match.group(0).rstrip(").,]") if match else ""
    item = TickTickItem(
        task_id=task_id, project=project_name, title=title or url or "Untitled",
        notes=notes, url=url, kind=detect_kind(url),
        task_created=_parse_dt(task.get("createdTime") or ""),
    )
    s.add(item)
    s.commit()
    log.info("ticktick: queued [%s] %s (from %s)", item.kind, item.title[:60], project_name)
    return 1


def _auto_dismiss(s, open_ids: set[str]) -> None:
    """Hans completed/deleted a still-queued task in TickTick — mirror that
    here so the queue can be cleaned from either side."""
    from sqlmodel import select

    from .db import TickTickItem

    for item in s.exec(select(TickTickItem).where(TickTickItem.status == "queued")).all():
        if item.task_id not in open_ids:
            item.status = "dismissed"
            s.add(item)
            log.info("ticktick: auto-dismissed %s (task gone from TickTick)", item.title[:60])
    s.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: PASS

- [ ] **Step 5: Ruff + full suite, then commit**

```bash
git add app/ticktick.py tests/test_ticktick.py
git commit -m "TickTick phase 2: read-only poller queues items (watermark removed)"
```

---

### Task 4: PDF text extraction (`pypdf`)

**Files:**
- Modify: `app/extract.py`, `requirements.txt`
- Test: `tests/test_extract.py`

**Interfaces:**
- Produces: `pdf_text(data: bytes) -> str` (pure) and `fetch_pdf_text(url: str) -> str` (async download + extract) in `app/extract.py`.

- [ ] **Step 1: Add the dependency**

Append `pypdf` to `requirements.txt` (alphabetical position if the file is sorted), then:

Run: `.venv/bin/pip install pypdf`

- [ ] **Step 2: Write the failing test**

Append to `tests/test_extract.py`. The helper assembles a fully valid one-page PDF with a correct xref table, so the test exercises real pypdf parsing with no fixture file and no network:

```python
def _mini_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF containing `text` (ASCII only)."""
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    bodies = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (
        len(bodies) + 1, xref_at)
    return bytes(out)


def test_pdf_text_extracts_page_text():
    from app.extract import pdf_text

    text = pdf_text(_mini_pdf("Attention is all you need."))
    assert "Attention is all you need." in text
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_extract.py::test_pdf_text_extracts_page_text -v`
Expected: FAIL with `ImportError: cannot import name 'pdf_text'`

- [ ] **Step 4: Implement**

In `app/extract.py` (place near `fetch_html`; reuse the same client options `fetch_html` uses — headers/UA, `follow_redirects` — copy its construction, with a longer 120s timeout since papers are big):

```python
def pdf_text(data: bytes) -> str:
    """Text content of a PDF, pages joined by blank lines. v1 of the queue's
    PDF path is text-only (spec §3) — figures/layout are not preserved."""
    import io

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


async def fetch_pdf_text(url: str) -> str:
    """Download a PDF and extract its text (queue-approved PDFs only — the
    RSS-side skip in ingest.process_episode stays)."""
    async with httpx.AsyncClient(
        timeout=120, follow_redirects=True, headers=_HEADERS
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return pdf_text(resp.content)
```

(`_HEADERS`: whatever header constant/dict `fetch_html` actually uses in this file — match it exactly; if it builds headers inline, do the same inline.)

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_extract.py::test_pdf_text_extracts_page_text -v`
Expected: PASS

- [ ] **Step 6: Ruff + full suite, then commit**

```bash
git add app/extract.py requirements.txt tests/test_extract.py
git commit -m "TickTick phase 2: pypdf text extraction for queue-approved PDFs"
```

---

### Task 5: `process_episode` PDF path behind `allow_pdf`

**Files:**
- Modify: `app/config.py` (SourceDef gains `allow_pdf: bool = False`, next to `narrate_mode` around line 63)
- Modify: `app/ingest.py:700-766` (the PDF guard and fetch block in `process_episode`)
- Test: `tests/test_ticktick.py`

**Interfaces:**
- Consumes: `fetch_pdf_text` (Task 4).
- Produces: `SourceDef.allow_pdf`; `process_episode` narrates PDFs when the source copy passed in has `allow_pdf=True` (only the queue's Generate ever sets it).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ticktick.py` (async, same marker convention as Task 3). It runs the real `process_episode` with the LLM and TTS faked out:

```python
from app.config import SourceDef, load_config


@pytest.mark.anyio
async def test_process_episode_narrates_pdf_when_allowed(monkeypatch):
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
    await ingest.process_episode(ep_id, source)

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    assert "summary" in done.provenance


@pytest.mark.anyio
async def test_process_episode_still_skips_pdf_without_allow_pdf():
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

    await ingest.process_episode(ep_id, inbox)  # inbox has allow_pdf=False

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "skipped"
    assert "PDF" in done.error
```

Notes for the implementer: `fetch_pdf_text` must be imported into `ingest.py`'s namespace (`from .extract import fetch_pdf_text` alongside the other extract imports) so the monkeypatch on `ingest.fetch_pdf_text` bites. `article_summary` and `synthesize` are already names in `ingest`'s namespace (check the import block at the top of `ingest.py` and monkeypatch whatever the actual attribute names are). If `load_config()` in the test env (tmp CONFIG_DIR, falls back to `config/sources.yaml.example`) has no `inbox` source, add one to the example — the README documents inbox as a standard source type.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: the first new test FAILS (episode gets skipped, status != "ready"); the second may already pass (that's fine — it pins existing behavior).

- [ ] **Step 3: Implement**

In `app/config.py`, add to `SourceDef` right after `narrate_mode`:

```python
    allow_pdf: bool = False  # queue-approved PDFs only; RSS PDFs stay skipped
```

In `app/ingest.py`, rework the guard at the top of the `try:` in `process_episode` (currently lines 700-713) and gate the generic fetch below it:

```python
        is_pdf = bool(link) and urlparse(link).path.lower().endswith(".pdf")
        # PDFs from feeds (e.g. a research-proof link) don't narrate into a
        # useful episode — only queue-approved items (allow_pdf) take the
        # extraction path.
        if is_pdf and not source.allow_pdf:
            log.info("skipping PDF link (not narratable): %s", link)
            with db.session() as s:
                ep = s.get(Episode, ep_id)
                ep.status = "skipped"
                ep.error = "PDF source — not narratable"
                ep.provenance = json.dumps({
                    "pipeline_version": PIPELINE_VERSION, "skipped": "pdf", "link": link,
                })
                s.add(ep)
                s.commit()
            return

        body, segments, og_image = "", [], ""
        html_text = ""
        paywalled = False
        fetch_issue = False
        if is_pdf:
            body = await fetch_pdf_text(link)
            sref = None
        else:
            sref = substack_ref(source, link) if link else None
        if sref:
            ... existing substack block unchanged ...
        if not is_pdf and (not sref or (not html_text and not paywalled)):
            ... existing generic fetch block unchanged ...
```

The rest of `process_episode` needs no change: a PDF body flows into the existing `narrate_mode == "summary"` branch (or full read), `detect_language` works on the text, and the empty `segments` list keeps it off the structured path.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: PASS

- [ ] **Step 5: Ruff + full suite, then commit**

```bash
git add app/config.py app/ingest.py tests/test_ticktick.py config/sources.yaml.example
git commit -m "TickTick phase 2: PDF narration path behind SourceDef.allow_pdf"
```

(Drop `config/sources.yaml.example` from the stage list if it needed no inbox addition.)

---

### Task 6: Generation routing — `generate_item` + book briefs

**Files:**
- Modify: `app/ticktick.py`
- Test: `tests/test_ticktick.py`

**Interfaces:**
- Consumes: `submit_url` (existing), `process_episode` (existing), `pdf_url` (Task 2), `SourceDef.allow_pdf` (Task 5), `summarize.llm` (existing: `async def llm(prompt, model="", tools=None, ...)`).
- Produces: `async generate_item(item_id: int, mode: str = "summary") -> int` (returns episode id; raises `ValueError` for unknown/dismissed items) — the web route in Task 7 calls exactly this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ticktick.py`:

```python
def _queue_item(**over):
    defaults = dict(task_id=f"t-{over.get('kind','x')}-{id(over)}", project="Z Reading",
                    title="Item", notes="", url="", kind="book", status="queued")
    defaults.update(over)
    with db.session() as s:
        item = TickTickItem(**defaults)
        s.add(item)
        s.commit()
        s.refresh(item)
        return item.id


@pytest.mark.anyio
async def test_generate_article_routes_to_submit_url(monkeypatch):
    calls = {}

    async def fake_submit_url(url, title="", language="auto"):
        calls["url"], calls["title"] = url, title
        return 42

    monkeypatch.setattr(ticktick, "submit_url", fake_submit_url)
    item_id = _queue_item(kind="article", url="https://example.com/post", title="A post")
    ep_id = await ticktick.generate_item(item_id)
    assert ep_id == 42 and calls["url"] == "https://example.com/post"
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
    assert item.status == "generated" and item.episode_id == 42


@pytest.mark.anyio
async def test_generate_pdf_spawns_allow_pdf_source(monkeypatch):
    spawned = {}

    def fake_spawn(coro):
        spawned["coro"] = coro
        coro.close()  # don't actually run the pipeline in this test

    monkeypatch.setattr(ticktick, "spawn", fake_spawn)
    item_id = _queue_item(kind="pdf", url="https://arxiv.org/abs/2401.00001",
                          title="A paper", task_id="t-pdf-1")
    ep_id = await ticktick.generate_item(item_id, mode="full")
    assert "coro" in spawned
    with db.session() as s:
        from app.db import Episode
        ep = s.get(Episode, ep_id)
        item = s.get(TickTickItem, item_id)
    assert ep.link == "https://arxiv.org/pdf/2401.00001"  # abs -> pdf
    assert ep.status == "pending"
    assert item.status == "generated" and item.episode_id == ep_id


@pytest.mark.anyio
async def test_book_brief_renders_via_source_text(monkeypatch):
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
    await ticktick._render_book_brief(ep_id, item_id, inbox,
                                      "The Power Broker", "Caro")
    with db.session() as s:
        ep = s.get(Episode, ep_id)
    assert "brief about The Power Broker" in ep.source_text
    assert processed["ep_id"] == ep_id


@pytest.mark.anyio
async def test_book_brief_failure_requeues_item(monkeypatch):
    from app import summarize
    from app.db import Episode

    async def boom(prompt, **kwargs):
        raise RuntimeError("shim down")

    monkeypatch.setattr(summarize, "llm", boom)
    item_id = _queue_item(kind="book", title="Some Book", task_id="t-book-2")
    config = load_config()
    inbox = next(s_ for s_ in config.sources if s_.type == "inbox")
    ep_id = ticktick._create_episode(inbox.slug, "ticktick:t-book-2", "Some Book", "")
    await ticktick._render_book_brief(ep_id, item_id, inbox, "Some Book", "")
    with db.session() as s:
        ep = s.get(Episode, ep_id)
        item = s.get(TickTickItem, item_id)
    assert ep.status == "error"
    assert item.status == "queued" and "shim down" in item.last_error


@pytest.mark.anyio
async def test_generate_dismissed_item_refused():
    item_id = _queue_item(kind="article", url="https://example.com/z",
                          status="dismissed", task_id="t-dis-1")
    with pytest.raises(ValueError):
        await ticktick.generate_item(item_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'generate_item'`

- [ ] **Step 3: Implement**

In `app/ticktick.py` add imports: `from sqlmodel import select`, `from .config import SourceDef, load_config`, `from .db import Episode, TickTickItem`, `from .ingest import submit_url`, `from .tasks import spawn` (module-level; `ruff` will flag circular-import problems — if `ingest` imports fail at module load because `ticktick` is imported by `scheduler` first, move the `ingest` import inside the functions, matching how `poll_ticktick` already imports `db` lazily). Then:

```python
async def generate_item(item_id: int, mode: str = "summary") -> int:
    """Hans clicked Generate: create the episode for a queued item, routed by
    kind (spec §2). The click IS the approval — nothing calls this
    automatically. Returns the episode id."""
    from . import db as _db

    with _db.session() as s:
        item = s.get(TickTickItem, item_id)
        if not item or item.status == "dismissed":
            raise ValueError(f"no queued ticktick item {item_id}")
        kind, url, title, notes, task_id = (
            item.kind, item.url, item.title, item.notes, item.task_id)
    config = load_config()
    inbox = next(src for src in config.sources if src.type == "inbox")
    if kind == "article":
        ep_id = await submit_url(url, title=title)
    elif kind == "pdf":
        ep_id = _create_episode(inbox.slug, url, title, pdf_url(url))
        source = SourceDef(**{**inbox.__dict__, "allow_pdf": True, "voice": "",
                              "narrate_mode": "full" if mode == "full" else "summary"})
        from .ingest import process_episode
        spawn(process_episode(ep_id, source))
    else:  # book — brief is generated first, then narrated as source_text
        ep_id = _create_episode(inbox.slug, f"ticktick:{task_id}", title, "")
        spawn(_render_book_brief(ep_id, item_id, inbox, title, notes))
    with _db.session() as s:
        item = s.get(TickTickItem, item_id)
        item.status = "generated"
        item.episode_id = ep_id
        item.last_error = ""
        s.add(item)
        s.commit()
    log.info("ticktick: generated item %s [%s] -> episode %s", item_id, kind, ep_id)
    return ep_id


def _create_episode(slug: str, guid: str, title: str, link: str) -> int:
    """Insert-or-reset an episode row (mirrors submit_url's dedup so a
    re-Generate after an error retries instead of duplicating)."""
    from . import db as _db

    with _db.session() as s:
        existing = s.exec(select(Episode).where(
            Episode.source_slug == slug, Episode.guid == guid)).first()
        if existing:
            existing.status = "pending"
            existing.error = ""
            s.add(existing)
            s.commit()
            return existing.id
        ep = Episode(source_slug=slug, guid=guid, title=title or "Untitled", link=link)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        return ep.id


async def _render_book_brief(ep_id: int, item_id: int, inbox, title: str,
                             notes: str) -> None:
    """LLM book brief (spec §4): generate the spoken text, park it in
    source_text, and let the normal pipeline narrate it (an episode with no
    link falls back to source_text as its body). On failure the queue item
    goes back to 'queued' with the error inline — nothing vanishes silently."""
    from . import db as _db
    from .ingest import process_episode
    from .summarize import llm

    prompt = (
        "You are preparing a short podcast brief about a book someone added to "
        "their reading list. Write 400-600 words of flowing spoken prose (no "
        "headings, no lists, no markdown) covering: what the book is and who "
        "wrote it, its core argument or story, its reception and context, and "
        "why it might be worth reading. Open with a sentence making clear this "
        "is a brief ABOUT the book, not the book itself. If you are not "
        "confident you know this book, say so honestly and keep it short.\n\n"
        f"Book reference: {title}" + (f"\nNotes: {notes}" if notes else "")
    )
    try:
        brief = await llm(prompt)
        with _db.session() as s:
            ep = s.get(Episode, ep_id)
            ep.source_text = brief
            s.add(ep)
            s.commit()
        await process_episode(ep_id, inbox)
    except Exception as exc:
        log.exception("book brief failed for queue item %s", item_id)
        with _db.session() as s:
            ep = s.get(Episode, ep_id)
            ep.status = "error"
            ep.error = f"book brief failed: {exc}"[:300]
            s.add(ep)
            item = s.get(TickTickItem, item_id)
            item.status = "queued"
            item.last_error = str(exc)[:300]
            item.episode_id = None
            s.add(item)
            s.commit()
```

Monkeypatch note for Step 1's tests: `_render_book_brief` imports `llm`/`process_episode` lazily at call time via `from .summarize import llm`, so patch `app.summarize.llm` and `app.ingest.process_episode` (the source modules), as the tests above do. `generate_item` references `submit_url`/`spawn` as module attributes of `ticktick`, so those are patched on `app.ticktick` — keep those imports module-level for exactly this reason.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ticktick.py -v`
Expected: PASS

- [ ] **Step 5: Ruff + full suite, then commit**

```bash
git add app/ticktick.py tests/test_ticktick.py
git commit -m "TickTick phase 2: kind-routed generate_item + LLM book briefs"
```

---

### Task 7: Admin queue — routes + template

**Files:**
- Modify: `app/web.py` (two routes + queue context in `index`)
- Modify: `app/templates/index.html` (queue section between "Needs your decision" and "Recent episodes", i.e. after the `{% if decisions %}` block ending near line 145)

**Interfaces:**
- Consumes: `generate_item` (Task 6), `TickTickItem` (Task 1).

- [ ] **Step 1: Add routes**

In `app/web.py` (place near `api_dismiss`; `RedirectResponse`, `HTTPException`, `db` already imported):

```python
@app.post("/{token}/api/ticktick/generate/{item_id}")
async def api_ticktick_generate(token: str, item_id: int, request: Request):
    """Generate the episode for a queued TickTick item (the click is the
    approval; mode applies to PDFs: summary|full)."""
    _check(token)
    form = await request.form()
    mode = (form.get("mode") or "summary").strip()
    from .ticktick import generate_item
    try:
        ep_id = await generate_item(item_id, mode=mode)
    except ValueError:
        raise HTTPException(status_code=404) from None
    log.info("ticktick queue: item %s -> episode %s (mode=%s)", item_id, ep_id, mode)
    return RedirectResponse(url=f"/{token}/", status_code=303)


@app.post("/{token}/api/ticktick/dismiss/{item_id}")
async def api_ticktick_dismiss(token: str, item_id: int):
    _check(token)
    from .db import TickTickItem
    with db.session() as s:
        item = s.get(TickTickItem, item_id)
        if not item:
            raise HTTPException(status_code=404)
        item.status = "dismissed"
        s.add(item)
        s.commit()
    return RedirectResponse(url=f"/{token}/", status_code=303)
```

- [ ] **Step 2: Add queue context to `index`**

In the `index` route, after the `decisions` list is built:

```python
    # TickTick queue: queued items, plus generated ones whose episode errored
    # (those stay visible with the error inline — spec §5).
    from .db import TickTickItem
    with db.session() as s:
        q_rows = s.exec(
            select(TickTickItem)
            .where(TickTickItem.status != "dismissed")
            .order_by(TickTickItem.task_created.desc())  # type: ignore[union-attr]
        ).all()
        q_eps = {r.episode_id: s.get(Episode, r.episode_id)
                 for r in q_rows if r.episode_id}
    queue = []
    for it in q_rows:
        ep = q_eps.get(it.episode_id)
        if it.status == "generated" and not (ep and ep.status == "error"):
            continue  # healthy episode — it lives in the episode list now
        queue.append({"item": it,
                      "error": (ep.error if ep and ep.status == "error"
                                else it.last_error)})
```

and add `"queue": queue,` to the `TemplateResponse` context dict.

- [ ] **Step 3: Add the template section**

In `app/templates/index.html`, directly after the `{% if decisions %}...{% endif %}` block. Match the file's existing markup style (look at how the decisions block renders episode rows, buttons, and `muted` small-text and mirror it — this is a style baseline, adapt tags/classes to what's actually there):

```html
{% if queue %}
<h2>📥 TickTick queue</h2>
<ul>
  {% for row in queue %}{% set it = row.item %}
  <li>
    <strong>[{{ it.kind }}]</strong>
    {% if it.url %}<a href="{{ it.url }}" target="_blank" rel="noopener">{{ it.title|truncate(70) }}</a>{% else %}{{ it.title|truncate(70) }}{% endif %}
    <small class="muted">{{ it.project }}{% if it.task_created %} · {{ it.task_created.strftime('%Y-%m-%d') }}{% endif %}</small>
    {% if it.notes %}<br><small class="muted">{{ it.notes|truncate(120) }}</small>{% endif %}
    {% if row.error %}<br><small style="color:#c0392b;">⚠ {{ row.error|truncate(160) }}</small>{% endif %}
    <form method="post" action="/{{ token }}/api/ticktick/generate/{{ it.id }}" style="display:inline">
      {% if it.kind == 'pdf' %}
        <button name="mode" value="summary">Generate → summary</button>
        <button name="mode" value="full">Generate → full read</button>
      {% elif it.kind == 'book' %}
        <button>Generate brief</button>
      {% else %}
        <button>Generate</button>
      {% endif %}
    </form>
    <form method="post" action="/{{ token }}/api/ticktick/dismiss/{{ it.id }}" style="display:inline">
      <button>Dismiss</button>
    </form>
  </li>
  {% endfor %}
</ul>
{% endif %}
```

(Check how the existing forms in the template build their `action` URLs — if they prefix with `{{ base }}`, do the same.)

- [ ] **Step 4: Verify the app boots and the section renders**

```bash
.venv/bin/python -c "from app.web import app; print('imports ok')"
.venv/bin/ruff check app/ scripts/ tests/ && .venv/bin/pytest -q
```

Expected: imports ok, all checks pass, full suite green. (Full HTTP-level route tests are deliberately out of scope — the token/config bootstrap doesn't fit the unit-test env; the deploy-time click-through in Task 8 covers the wiring.)

- [ ] **Step 5: Commit**

```bash
git add app/web.py app/templates/index.html
git commit -m "TickTick phase 2: admin queue section + generate/dismiss routes"
```

---

### Task 8: Docs, deploy, live verification

**Files:**
- Modify: `README.md` (the TickTick bullet under "Intake & special sources", lines ~118-127)

- [ ] **Step 1: Update the README TickTick bullet**

Rewrite the existing bullet to describe phase 2 (keep the auth/setup sentences as-is):

```markdown
- **TickTick** — tasks on watched lists land in an admin **approval queue**
  (nothing generates on its own; the integration is read-only and never
  completes your tasks — the whole backlog is imported). Each queued item is
  classified article / PDF / book: articles become inbox episodes, PDFs are
  text-extracted and summarized (or read in full — per-item choice), and
  title-only book references get an LLM "book brief" episode. Completing a
  task in TickTick auto-dismisses its queue item. Watched lists are set in
  `data/ticktick.json` (`"lists": ["Z Reading", "Z Listening"]`).
  One-time setup: register an app at https://developer.ticktick.com/manage with
  redirect URI `http://127.0.0.1:8993/callback`, then
  `.venv/bin/python scripts/ticktick_auth.py CLIENT_ID CLIENT_SECRET`.
  Note: WSL2 doesn't always forward Windows `localhost:8993` to the callback
  server, so the browser redirect may fail to load — just copy the `code=...`
  value from the redirected URL and exchange it manually. Poller runs every 5 min.
```

- [ ] **Step 2: Full gate**

Run: `.venv/bin/ruff check app/ scripts/ tests/ && .venv/bin/pytest -q`
Expected: all pass.

- [ ] **Step 3: Commit and deploy via the ship skill**

Use the `podcastfeeds-ship` skill (lint/test already done): commit `README.md`, push, watch CI exit-code-safe, `docker compose up -d --build`, verify healthy + no errors in logs.

- [ ] **Step 4: Live verification (with Hans)**

- Within 5 minutes of deploy the poller should import the backlog: check `docker compose logs` for `ticktick: queued [...]` lines and the admin page for the new section.
- Hans clicks Generate on one item of his choosing (his click = approval; do NOT trigger generation any other way).
- Verify the TickTick task was NOT completed and the episode appears.

- [ ] **Step 5: Update memory**

Update the `hans-audio-preferences` memory ("TickTick intake is phase 2" → phase 2 shipped: approval queue, read-only) and the `podcastfeeds-features` memory (add the queue + book briefs + PDF path).

---

## Self-review notes (done at plan time)

- Spec coverage: §1 poller/model → Tasks 1-3; §2 UI/routing → Tasks 6-7; §3 PDF → Tasks 4-5; §4 book brief → Task 6; §5 errors/testing → Tasks 3, 6 (failure test), 7 (error rows); backlog import → falls out of Task 3 (no watermark); "never auto-complete" → Task 3 read-only test.
- Async-test convention (Task 3 note) applies to every async test in Tasks 5-6 too: copy whatever `tests/test_substack.py` actually uses.
- `_clear_queue()` (Task 3) should be called at the top of any test that asserts on queue contents — tests share one throwaway DB.
