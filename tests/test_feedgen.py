from datetime import UTC, datetime

from app.feedgen import _duration, _rfc2822


def test_duration_formats_hms():
    assert _duration(3661) == "01:01:01"
    assert _duration(0) == "00:00:00"
    assert _duration(59) == "00:00:59"
    assert _duration(3600) == "01:00:00"


def test_rfc2822_aware_datetime():
    got = _rfc2822(datetime(2026, 7, 17, 12, 0, 0, tzinfo=UTC))
    assert "17 Jul 2026 12:00:00" in got
    assert got.endswith("+0000")


def test_rfc2822_naive_datetime_treated_as_utc():
    got = _rfc2822(datetime(2026, 7, 17, 12, 0, 0))
    assert "17 Jul 2026 12:00:00 +0000" in got


def test_rfc2822_none_returns_a_string():
    assert isinstance(_rfc2822(None), str) and _rfc2822(None)


def _parse(item_xml: str):
    """<item> alone uses the itunes: prefix declared on the <rss> root."""
    from xml.etree import ElementTree

    return ElementTree.fromstring(
        '<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">'
        f"{item_xml}</rss>"
    ).find("item")


def _ep(**kw):
    from app.db import Episode

    base = {"id": 7, "source_slug": "s", "guid": "g", "title": "T",
            "link": "https://e.com/a", "audio_file": "a.mp3", "audio_bytes": 10,
            "audio_seconds": 60, "description": "d", "image_url": None}
    return Episode(**{**base, **kw})


def test_a_quote_in_an_image_url_cannot_break_the_feed():
    """escape() leaves '"' alone, so a quote in an og:image URL closed the
    attribute and made the whole feed invalid XML — the podcast app then shows
    zero episodes for that source, not one bad item."""
    from app.feedgen import _item_xml

    xml = _item_xml(_ep(image_url='https://x.test/a".jpg?q="1'), "https://b", "tok", "N")
    node = _parse(xml)  # raises if the attribute broke out
    img = node.find("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")
    assert img.get("href") == 'https://x.test/a".jpg?q="1'


def test_a_quote_in_a_passthrough_enclosure_url_cannot_break_the_feed():
    from app.feedgen import _item_xml

    xml = _item_xml(_ep(audio_file=None, audio_url='https://x.test/b".mp3'),
                    "https://b", "tok", "N")
    node = _parse(xml)
    assert node.find("enclosure").get("url") == 'https://x.test/b".mp3'


def _ready(slug: str, guid: str):
    from app import db
    from app.db import Episode, utcnow

    with db.session() as s:
        s.add(Episode(source_slug=slug, guid=guid, title=f"{slug} {guid}",
                      status="ready", audio_file="a.mp3", audio_bytes=1,
                      published_at=utcnow()))
        s.commit()


def test_learning_source_is_auto_added_and_kept_out_of_all():
    from app.config import load_config

    learning = next(s for s in load_config().sources if s.type == "learning")
    assert learning.slug == "learning"
    assert learning.in_all_feed is False


def test_learning_episodes_stay_out_of_the_combined_feed():
    """Courses are listened to on purpose, in order; mixed into all.xml they
    would bury the news and scatter a series across the day's queue."""
    from app.config import load_config
    from app.feedgen import build_feed

    _ready("learning", "feedtest-course:1")
    _ready("inbox", "https://feedtest.example/a")
    config = load_config()
    combined = build_feed(config, "https://b", "tok")
    assert "feedtest-course:1" not in combined
    assert "feedtest.example/a" in combined
    learning = next(s for s in config.sources if s.slug == "learning")
    assert "feedtest-course:1" in build_feed(config, "https://b", "tok", source=learning)
