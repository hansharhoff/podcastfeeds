"""TickTick phase 2: queue model, kind detection, poller, generation routing."""
from app import db
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
