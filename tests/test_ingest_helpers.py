from app.config import SourceDef
from app.ingest import (
    _attr,
    _entry_audio,
    _entry_guid,
    _norm_title,
    _substack_fetch_url,
)


def test_norm_title_strips_punctuation_and_lowercases():
    assert _norm_title("Hello, World! #1") == "helloworld1"


def test_entry_guid_prefers_id_then_link_then_title():
    assert _entry_guid({"id": "g1", "link": "L", "title": "T"}) == "g1"
    assert _entry_guid({"link": "L", "title": "T"}) == "L"
    assert _entry_guid({"title": "T"}) == "T"
    assert _entry_guid({}) == ""


def test_entry_audio_extracts_enclosure():
    entry = {"enclosures": [{"type": "audio/mpeg", "href": "http://a/x.mp3", "length": "123"}]}
    assert _entry_audio(entry) == ("http://a/x.mp3", 123)


def test_entry_audio_bad_length_is_zero():
    entry = {"enclosures": [{"type": "audio/mpeg", "href": "http://a/x.mp3", "length": "NaN"}]}
    assert _entry_audio(entry) == ("http://a/x.mp3", 0)


def test_entry_audio_none_when_no_audio():
    assert _entry_audio({"enclosures": [{"type": "image/png", "href": "x"}]}) == ("", 0)
    assert _entry_audio({}) == ("", 0)


def test_substack_fetch_url_rewrites_custom_domain_to_subdomain():
    src = SourceDef(slug="sb", name="SB", type="rss",
                    url="https://matthewyglesias.substack.com/feed")
    got = _substack_fetch_url(src, "https://www.slowboring.com/p/some-post")
    assert got == "https://matthewyglesias.substack.com/p/some-post"


def test_substack_fetch_url_leaves_non_substack_untouched():
    src = SourceDef(slug="x", name="X", type="rss", url="https://example.com/feed")
    link = "https://example.com/p/some-post"
    assert _substack_fetch_url(src, link) == link


def test_substack_fetch_url_leaves_matching_host_untouched():
    src = SourceDef(slug="sb", name="SB", type="rss",
                    url="https://acme.substack.com/feed")
    link = "https://acme.substack.com/p/post"
    assert _substack_fetch_url(src, link) == link


def test_attr_escapes_html_dangerous_chars():
    out = _attr('http://x/?a=1&b=2"><script>')
    assert "&amp;" in out and "&quot;" in out and "&lt;" in out and "&gt;" in out
    assert '"' not in out and "<" not in out
