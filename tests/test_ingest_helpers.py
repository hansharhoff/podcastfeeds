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

# ── preview messaging: subscribed-but-truncated must say "fetch problem",
#    not "requires a paid subscription" (ep. 243 feedback) ────────────────

def test_episode_intro_preview_plain():
    from app.ingest import _episode_intro
    text = _episode_intro("T", "Src", "en", preview=True)
    assert "free preview of a paid post" in text


def test_episode_intro_preview_fetch_issue_en():
    from app.ingest import _episode_intro
    text = _episode_intro("T", "Src", "en", preview=True, fetch_issue=True)
    assert "problem getting the full version" in text
    assert "free preview of a paid post" not in text


def test_episode_intro_preview_fetch_issue_da():
    from app.ingest import _episode_intro
    text = _episode_intro("T", "Src", "da", preview=True, fetch_issue=True)
    assert "problem med at hente den fulde version" in text


def test_preview_outro_fetch_issue_en():
    from app.ingest import _preview_outro
    plain = _preview_outro("en")
    issue = _preview_outro("en", fetch_issue=True)
    assert "requires a paid subscription" in plain
    assert "could not be fetched" in issue
    assert "requires a paid subscription" not in issue


def test_preview_outro_fetch_issue_da():
    from app.ingest import _preview_outro
    issue = _preview_outro("da", fetch_issue=True)
    assert "kunne ikke hentes" in issue


# ── paywall action: paid posts DEFER (stay pending) while the subscriber
#    session is broken, instead of publishing previews (Hans, 2026-07-23) ──

def test_paywall_action_defers_on_fetch_issue_regardless_of_length():
    from app.ingest import _paywall_action
    assert _paywall_action(fetch_issue=True, body_chars=10_000) == "defer"
    assert _paywall_action(fetch_issue=True, body_chars=100) == "defer"


def test_paywall_action_substantial_preview_without_fetch_issue():
    from app.ingest import _paywall_action
    assert _paywall_action(fetch_issue=False, body_chars=600) == "preview"


def test_paywall_action_thin_preview_without_fetch_issue_skips():
    from app.ingest import _paywall_action
    assert _paywall_action(fetch_issue=False, body_chars=599) == "skip"
