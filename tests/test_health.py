"""In-app paid-access health check (durable replacement for external probes).

Every paid: true substack source gets its newest only_paid post fetched
through the real fetch_post path on a schedule; failures surface as an admin
banner + WARNING log instead of depending on any external session.
"""
import asyncio

from app.health import LAST, check_paid_access, newest_paid_slug


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_newest_paid_slug_picks_first_paid_item():
    items = [
        {"audience": "everyone", "slug": "free-live-video"},
        {"audience": "only_paid", "slug": "the-paid-one"},
        {"audience": "only_paid", "slug": "older-paid"},
    ]
    assert newest_paid_slug(items) == "the-paid-one"


def test_newest_paid_slug_handles_empty_and_missing_fields():
    assert newest_paid_slug([]) is None
    assert newest_paid_slug(None) is None
    assert newest_paid_slug([{"audience": "only_paid"}, {"slug": "s"}]) is None


def test_check_paid_access_probes_only_paid_substack_sources(monkeypatch):
    from types import SimpleNamespace

    from app import health

    sources = [
        SimpleNamespace(slug="pobrien", name="Phillips's", type="rss",
                        url="https://phillipspobrien.substack.com/feed", paid=True),
        SimpleNamespace(slug="slowboring", name="Slow Boring", type="rss",
                        url="https://matthewyglesias.substack.com/feed", paid=False),
        SimpleNamespace(slug="dr", name="DR", type="breaking",
                        url="https://www.dr.dk/", paid=False),
    ]
    monkeypatch.setattr(health, "load_config",
                        lambda: SimpleNamespace(sources=sources))

    async def fake_api(url, cookie_url=None):
        assert "phillipspobrien" in url  # non-paid sources must not be probed
        return [{"audience": "everyone", "slug": "free"},
                {"audience": "only_paid", "slug": "paid-post"}]

    async def fake_fetch(sub, slug):
        assert (sub, slug) == ("phillipspobrien", "paid-post")
        return {"accessible": True, "delivered_words": 1400, "wordcount": 1390}

    monkeypatch.setattr(health, "_api_json", fake_api)
    monkeypatch.setattr(health, "fetch_post", fake_fetch)

    snapshot = _run(check_paid_access())
    assert snapshot is LAST
    assert snapshot["checked_at"]
    assert snapshot["results"] == [
        {"slug": "pobrien", "name": "Phillips's", "post": "paid-post",
         "ok": True, "delivered": 1400, "wordcount": 1390},
    ]


def test_check_paid_access_flags_broken_access(monkeypatch):
    from types import SimpleNamespace

    from app import health

    sources = [SimpleNamespace(slug="noahpinion", name="Noahpinion", type="rss",
                               url="https://noahpinion.substack.com/feed", paid=True)]
    monkeypatch.setattr(health, "load_config",
                        lambda: SimpleNamespace(sources=sources))

    async def fake_api(url, cookie_url=None):
        return [{"audience": "only_paid", "slug": "p"}]

    async def fake_fetch(sub, slug):
        return {"accessible": False, "delivered_words": 60, "wordcount": 1300}

    monkeypatch.setattr(health, "_api_json", fake_api)
    monkeypatch.setattr(health, "fetch_post", fake_fetch)

    snapshot = _run(check_paid_access())
    assert snapshot["results"][0]["ok"] is False


# ── stuck-pending watchdog: episodes sitting in pending/processing for >12h
#    (the ep-312 defer-loop class) must surface, not rot silently. ──

def test_stuck_episodes_flags_old_pending_and_processing():
    from datetime import timedelta

    from app import db
    from app.db import Episode, utcnow
    from app.health import stuck_episodes

    with db.session() as s:
        s.add(Episode(source_slug="a", guid="g-old-pending", title="Old pending",
                      status="pending", created_at=utcnow() - timedelta(hours=13)))
        s.add(Episode(source_slug="a", guid="g-old-processing", title="Old processing",
                      status="processing", created_at=utcnow() - timedelta(hours=13)))
        s.add(Episode(source_slug="a", guid="g-fresh", title="Fresh pending",
                      status="pending", created_at=utcnow() - timedelta(hours=1)))
        s.add(Episode(source_slug="a", guid="g-ready", title="Old ready",
                      status="ready", created_at=utcnow() - timedelta(hours=48)))
        s.commit()

    stuck = stuck_episodes(max_age_hours=12)
    titles = sorted(e["title"] for e in stuck)
    assert titles == ["Old pending", "Old processing"]
    assert all(set(e) >= {"id", "title", "status", "source_slug", "age_hours"}
               for e in stuck)
