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
