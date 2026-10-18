"""Outcome monitoring: each check, the bookkeeping, and the feed alert."""
import json
from datetime import timedelta

import pytest

from app import db, monitor
from app.db import KV, Episode


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """Each test starts from an empty episode table and no monitor state."""
    from sqlmodel import delete

    with db.session() as s:
        s.exec(delete(Episode))
        s.exec(delete(KV).where(KV.key.like("monitor:%")))
        s.commit()
    monitor.LAST.update({"checked_at": "", "problems": []})
    yield
    monitor.LAST.update({"checked_at": "", "problems": []})


def _ep(status, hours_ago=1, error="", slug="inbox", rendered_hours_ago=None):
    now = monitor._now()
    prov = {}
    if rendered_hours_ago is not None:
        prov["generated_at"] = (now - timedelta(hours=rendered_hours_ago)).isoformat()
    with db.session() as s:
        ep = Episode(source_slug=slug, guid=f"g{monitor._now().timestamp()}{status}{hours_ago}{error}",
                     title="t", status=status, error=error,
                     created_at=now - timedelta(hours=hours_ago),
                     provenance=json.dumps(prov))
        s.add(ep)
        s.commit()


def _age(component, hours, ok=True, fails=0, detail=""):
    """Write monitor state as if the last success/failure was `hours` ago."""
    when = (monitor._now() - timedelta(hours=hours)).isoformat()
    state = {"fails_in_row": fails, "detail": detail}
    state["last_ok" if ok else "last_fail"] = when
    with db.session() as s:
        db.kv_set(s, f"monitor:{component}", json.dumps(state))
        s.commit()


# ── record ──────────────────────────────────────────────────────────────────

def test_record_counts_failures_and_resets_on_success():
    monitor.record("x", False, "boom")
    monitor.record("x", False, "boom again")
    state = monitor._state("x")
    assert state["fails_in_row"] == 2 and state["detail"] == "boom again"
    monitor.record("x", True)
    state = monitor._state("x")
    assert state["fails_in_row"] == 0 and state["last_ok"]


def test_record_never_raises(monkeypatch):
    def broken():
        raise RuntimeError("db gone")

    monkeypatch.setattr(db, "session", broken)
    monitor.record("x", True)  # must not raise


# ── episode errors (the October edge-tts break) ─────────────────────────────

TTS_403 = ("403, message='Invalid response status', url='wss://speech.platform.bing.com/"
           "consumer/speech/synthesize?ConnectionId=abc123'")


def test_a_wave_of_errors_is_a_problem():
    for i in range(4):
        _ep("error", error=TTS_403.replace("abc123", f"id{i}"))
    _ep("ready")
    [p] = monitor.check_episode_errors()
    assert p["key"] == "episode-errors"
    assert "4 of 5 episodes failed" in p["summary"]
    assert "(4)" in p["detail"] and "<url>" in p["detail"]  # grouped despite differing ids


def test_occasional_errors_among_successes_are_not():
    for _ in range(3):
        _ep("error", error="boom")
    for _ in range(4):
        _ep("ready")
    assert monitor.check_episode_errors() == []


def test_old_errors_are_not_counted():
    for _ in range(5):
        _ep("error", hours_ago=72, error="boom")
    assert monitor.check_episode_errors() == []


# ── nothing new ─────────────────────────────────────────────────────────────

def test_nothing_new_fires_after_36_hours():
    _ep("ready", hours_ago=100, rendered_hours_ago=40)
    [p] = monitor.check_nothing_new()
    assert p["key"] == "nothing-new" and "40 hours" in p["summary"]


def test_a_recently_rendered_old_episode_counts_as_new():
    # A retried episode keeps its old created_at; the render time is what matters.
    _ep("ready", hours_ago=100, rendered_hours_ago=2)
    assert monitor.check_nothing_new() == []


def test_learning_episodes_do_not_hide_a_dead_main_feed():
    _ep("ready", slug="inbox", rendered_hours_ago=50)
    _ep("ready", slug="learning", rendered_hours_ago=1)
    assert monitor.check_nothing_new()


def test_nothing_new_is_quiet_on_a_fresh_install():
    assert monitor.check_nothing_new() == []


# ── polls ───────────────────────────────────────────────────────────────────

def _rss_source(monkeypatch, poll_minutes=180):
    from app.config import AppConfig, SourceDef

    src = SourceDef(slug="mon-src", name="Mon Source", type="rss",
                    url="https://x.example/feed", poll_minutes=poll_minutes)
    monkeypatch.setattr(monitor, "load_config", lambda: AppConfig(sources=[src]))
    return src


def test_a_feed_failing_for_a_day_is_a_problem(monkeypatch):
    _rss_source(monkeypatch)
    _age("poll:mon-src", hours=30, ok=True, fails=9, detail="HTTP 404")
    [p] = monitor.check_polls()
    assert p["key"] == "poll:mon-src" and "Mon Source" in p["summary"]
    assert p["detail"] == "HTTP 404"


def test_a_brief_feed_blip_is_not(monkeypatch):
    _rss_source(monkeypatch)
    _age("poll:mon-src", hours=5, ok=True, fails=2)
    assert monitor.check_polls() == []


def test_a_recovered_feed_is_not(monkeypatch):
    _rss_source(monkeypatch)
    _age("poll:mon-src", hours=30, ok=True, fails=0)
    assert monitor.check_polls() == []


def test_slow_polling_sources_get_more_slack(monkeypatch):
    _rss_source(monkeypatch, poll_minutes=720)  # 6 intervals = 72h
    _age("poll:mon-src", hours=30, ok=True, fails=2)
    assert monitor.check_polls() == []


# ── LLM, TickTick, podcast app, disk ────────────────────────────────────────

def test_llm_down_for_half_a_day_is_a_problem():
    _age("llm", hours=13, ok=True, fails=5, detail="ConnectError")
    [p] = monitor.check_llm()
    assert p["key"] == "llm" and "ConnectError" in p["detail"]


def test_llm_brief_failure_is_not():
    _age("llm", hours=2, ok=True, fails=1)
    assert monitor.check_llm() == []


def test_llm_never_worked_and_failing_for_long_is_a_problem():
    _age("llm", hours=20, ok=False, fails=40)
    assert monitor.check_llm()


def test_ticktick_expired_token_is_a_problem():
    _age("ticktick", hours=30, ok=True, fails=300, detail="access token expired or invalid (401)")
    [p] = monitor.check_ticktick()
    assert "401" in p["detail"]


def test_podcast_app_silence_is_a_problem():
    _age("pocketcasts", hours=30)
    [p] = monitor.check_podcast_app()
    assert "Pocket Casts" in p["summary"]
    _age("pocketcasts", hours=3)
    assert monitor.check_podcast_app() == []


def test_podcast_app_never_seen_is_not_a_problem():
    assert monitor.check_podcast_app() == []


def test_low_disk_is_a_problem(monkeypatch):
    import collections

    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(monitor.shutil, "disk_usage", lambda p: usage(10, 9, 1024**3))
    [p] = monitor.check_disk()
    assert "1.0 GB free" in p["summary"]


# ── run_checks bookkeeping ──────────────────────────────────────────────────

def test_run_checks_keeps_first_seen_and_clears_resolved(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return [{"key": "k", "summary": "s", "detail": "d"}] if calls["n"] < 3 else []

    monkeypatch.setattr(monitor, "CHECKS", (flaky,))
    first = monitor.run_checks()[0]["since"]
    assert monitor.run_checks()[0]["since"] == first
    assert monitor.run_checks() == [] and monitor.LAST["problems"] == []
    with db.session() as s:
        assert json.loads(db.kv_get(s, "monitor:open")) == {}


def test_a_crashing_check_is_itself_reported(monkeypatch):
    def broken():
        raise ValueError("bad")

    monkeypatch.setattr(monitor, "CHECKS", (broken,))
    [p] = monitor.run_checks()
    assert p["key"] == "check:broken"


# ── feed alert ──────────────────────────────────────────────────────────────

def _problem(key="episode-errors"):
    monitor.LAST["problems"] = [{"key": key, "summary": "4 of 5 episodes failed",
                                 "detail": "Most common: <boom> & co",
                                 "since": monitor._now().isoformat()}]


def test_alert_item_tops_the_combined_feed_only():
    from xml.etree import ElementTree

    from app.config import load_config
    from app.feedgen import build_feed

    _ep("ready", slug="inbox")
    _problem()
    config = load_config()
    xml = build_feed(config, "https://b", "tok")
    items = ElementTree.fromstring(xml).find("channel").findall("item")
    assert items[0].findtext("title").startswith("⚠ podcastfeeds: 4 of 5 episodes failed")
    assert items[0].findtext("guid").startswith("monitor-alert:episode-errors:")
    assert items[0].find("enclosure").get("url") == f"https://b/tok/media/{monitor.ALERT_AUDIO}"
    inbox = next(s for s in config.sources if s.slug == "inbox")
    assert "monitor-alert" not in build_feed(config, "https://b", "tok", source=inbox)


def test_alert_guid_changes_with_the_set_of_problems():
    _problem("a")
    one = monitor.alert_item_xml("https://b", "tok")
    _problem("b")
    assert monitor.alert_item_xml("https://b", "tok") != one


def test_no_problems_no_alert():
    assert monitor.alert_item_xml("https://b", "tok") == ""


def test_alert_tone_needs_no_tts(monkeypatch):
    """The alert must work when narration is what broke."""
    from app import tts

    def no_tts(*a, **k):
        raise AssertionError("alert used TTS")

    monkeypatch.setattr(tts, "synthesize", no_tts)
    (monitor.MEDIA_DIR / monitor.ALERT_AUDIO).unlink(missing_ok=True)
    _problem()
    assert monitor.ALERT_AUDIO in monitor.alert_item_xml("https://b", "tok")
    assert (monitor.MEDIA_DIR / monitor.ALERT_AUDIO).stat().st_size > 1000


# ── wiring ──────────────────────────────────────────────────────────────────

def test_rss_poll_reports_its_outcome(monkeypatch):
    from app import ingest
    from app.config import SourceDef

    async def dead(url):
        return Exception("HTTP 404")

    monkeypatch.setattr(ingest, "_parse_feed", dead)
    src = SourceDef(slug="mon-poll", name="P", type="rss", url="https://x.example/feed")
    import asyncio
    asyncio.new_event_loop().run_until_complete(ingest.poll_rss_source(src))
    state = monitor._state("poll:mon-poll")
    assert state["fails_in_row"] == 1 and "HTTP 404" in state["detail"]


def test_pocket_casts_fetch_is_noted():
    from fastapi.testclient import TestClient

    from app import web
    from app.config import get_token

    TestClient(web.app).get(f"/{get_token()}/feeds/all.xml",
                            headers={"User-Agent": "PocketCasts/1.0 (Pocket Casts Feed Parser)"})
    assert monitor._state("pocketcasts").get("last_ok")


def test_media_sweep_keeps_the_alert_tone():
    import asyncio
    import os

    from app import ingest

    _problem()
    monitor.alert_item_xml("https://b", "tok")
    path = monitor.MEDIA_DIR / monitor.ALERT_AUDIO
    os.utime(path, (0, 0))  # make it look ancient
    asyncio.new_event_loop().run_until_complete(ingest.cleanup_orphaned_media())
    assert path.exists()
