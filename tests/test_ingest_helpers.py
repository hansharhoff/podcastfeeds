import asyncio
import json
import os
import time

import pytest

from app import db
from app.config import MEDIA_DIR, SourceDef, load_config
from app.db import Episode
from app.ingest import (
    _attr,
    _entry_audio,
    _entry_guid,
    _norm_title,
    _substack_fetch_url,
    cleanup_orphaned_media,
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


# ── process_episode: link-forwarding social posts (X/Twitter etc.) get the
#    outbound article narrated, not the post itself (ep. 253 feedback).
#    Async-test convention mirrored from test_substack.py/test_ticktick.py:
#    no pytest-asyncio/anyio plugin, so async tests run via a local _run().

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_process_episode_follows_outbound_link_from_social_post(monkeypatch):
    from app import ingest
    from app.db import Episode

    post_url = "https://x.com/demishassabis/status/2076957440109625718"
    article_url = "https://deepmind.google/blog/a-framework-for-frontier-ai/"
    post_html = (
        '<html><body><a href="https://x.com/demishassabis">profile</a>'
        f'<p>Excited to share our new framework.</p>'
        f'<a href="{article_url}">deepmind.google/blog/a-framewo…</a>'
        "</body></html>"
    )

    async def fake_fetch_html(url):
        if url == post_url:
            return post_html
        if url == article_url:
            return "<html><body>real article page</body></html>"
        raise AssertionError(f"unexpected fetch: {url}")

    def fake_extract_segments(html_text, url=""):
        # Title comes from extract_segments in process_episode; segments stay
        # empty either way, forcing the plain-narration branch.
        title = "A Framework for Frontier AI" if url == article_url else ""
        return title, []

    def fake_extract_article(html_text, url=""):
        if url == post_url:
            return "", "Excited to share our new framework."
        if url == article_url:
            return "A Framework for Frontier AI", "Full essay body text. " * 40
        return "", ""

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 4321, 90

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(ingest, "extract_segments", fake_extract_segments)
    monkeypatch.setattr(ingest, "extract_article", fake_extract_article)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid=post_url, title="A tweet", link=post_url)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    assert "A Framework for Frontier AI" in done.title
    prov = json.loads(done.provenance)
    assert prov["followed_link"] == article_url
    assert prov["link_source"] == post_url
    assert prov["link"] == article_url  # show notes point at the real article
    assert prov["link_follow"] == "followed"


def test_process_episode_falls_back_to_post_text_when_no_outbound_link(monkeypatch):
    """No outbound link on the post -> narrate the post's own text, same as
    today's floor (ep. 253's buggy-but-not-broken behavior)."""
    from app import ingest
    from app.db import Episode

    post_url = "https://x.com/someone/status/999"
    post_html = '<html><body><a href="https://x.com/someone">profile</a></body></html>'

    async def fake_fetch_html(url):
        assert url == post_url
        return post_html

    def fake_extract_segments(html_text, url=""):
        return "", []

    def fake_extract_article(html_text, url=""):
        return "", "Just a short tweet with no link, but padded past the fetch floor. " * 4

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 111, 10

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(ingest, "extract_segments", fake_extract_segments)
    monkeypatch.setattr(ingest, "extract_article", fake_extract_article)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid=post_url, title="A tweet", link=post_url)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    prov = json.loads(done.provenance)
    assert "followed_link" not in prov
    assert prov["link"] == post_url
    assert prov["link_follow"] == "skipped: no outbound link found"


def test_process_episode_falls_back_when_target_fetch_fails(monkeypatch):
    """Outbound link found but the target 404s / times out -> keep the post's
    own extraction, never a hard error (the brief's explicit floor)."""
    from app import ingest
    from app.db import Episode

    post_url = "https://x.com/someone/status/1000"
    article_url = "https://example.com/dead-link"
    post_html = (
        '<html><body><a href="https://x.com/someone">profile</a>'
        f'<a href="{article_url}">a broken link</a></body></html>'
    )

    async def fake_fetch_html(url):
        if url == post_url:
            return post_html
        raise RuntimeError("simulated network failure")

    def fake_extract_segments(html_text, url=""):
        return "", []

    def fake_extract_article(html_text, url=""):
        assert url == post_url
        return "", "Sharing a link that turns out to be dead, padded past the floor. " * 4

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 222, 10

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(ingest, "extract_segments", fake_extract_segments)
    monkeypatch.setattr(ingest, "extract_article", fake_extract_article)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid=post_url, title="A tweet", link=post_url)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"  # never a hard error
    prov = json.loads(done.provenance)
    assert "followed_link" not in prov
    assert prov["link"] == post_url
    assert prov["link_follow"] == "skipped: fetch failed"


# ── live-test findings (real fetch of the ep. 253 post): the outbound link
#    is a t.co short link that resolves BACK to an x.com-native article —
#    must be skipped (same platform) before any full fetch, and a JS-shell
#    body must never replace the post's own extraction even if it's longer
#    than a bare length floor. ──

def test_process_episode_skips_same_platform_after_resolving_short_link(monkeypatch):
    from app import ingest
    from app.db import Episode

    post_url = "https://x.com/demishassabis/status/2076957440109625719"
    short_link = "https://t.co/PTeDiv1b6L"
    resolved_article_url = "https://x.com/i/article/2076946210397552640"
    post_html = (
        '<html><head>'
        f'<meta property="og:description" content="{short_link}"/>'
        '</head><body>'
        '<a href="https://x.com/demishassabis">profile</a>'
        "</body></html>"
    )

    async def fake_fetch_html(url):
        if url == post_url:
            return post_html
        raise AssertionError(f"unexpected fetch: {url} — resolved same-platform "
                              "target must never be fetched in full")

    async def fake_resolve_short_link(url):
        assert url == short_link
        return resolved_article_url

    def fake_extract_segments(html_text, url=""):
        return "", []

    def fake_extract_article(html_text, url=""):
        assert url == post_url
        return "", "The tweet's own text, padded past the extraction floor here. " * 4

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 333, 10

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(ingest, "resolve_short_link", fake_resolve_short_link)
    monkeypatch.setattr(ingest, "extract_segments", fake_extract_segments)
    monkeypatch.setattr(ingest, "extract_article", fake_extract_article)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid=post_url, title="A tweet", link=post_url)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    prov = json.loads(done.provenance)
    assert "followed_link" not in prov  # never followed — post extraction preserved
    assert prov["link"] == post_url
    assert prov["link_follow"] == "skipped: same-platform article"


def test_process_episode_rejects_js_shell_target_even_though_longer(monkeypatch):
    from app import ingest
    from app.db import Episode

    post_url = "https://x.com/demishassabis/status/1"
    article_url = "https://x-native-article-mirror.example/2076946210397552640"
    post_html = (
        '<html><body><a href="https://x.com/demishassabis">profile</a>'
        f'<a href="{article_url}">the article</a></body></html>'
    )
    # Verbatim boilerplate from the live target fetch (a JS-only render).
    js_shell_body = ("We've detected that JavaScript is disabled in this "
                      "browser. Please enable JavaScript or switch to a "
                      "supported browser to continue using this site. " * 3)

    async def fake_fetch_html(url):
        if url == post_url:
            return post_html
        if url == article_url:
            return "<html><body>js shell</body></html>"
        raise AssertionError(f"unexpected fetch: {url}")

    def fake_extract_segments(html_text, url=""):
        return "", []

    # Well past the pipeline's own 40-char "no content" floor, but shorter
    # than js_shell_body — so a bare length comparison alone would follow
    # the link; only the boilerplate check catches it.
    post_body = "Sharing our thinking on this, worth a read if you have time. " * 2

    def fake_extract_article(html_text, url=""):
        if url == post_url:
            return "", post_body
        if url == article_url:
            return "A Framework for Frontier AI", js_shell_body  # > 200 chars, > post body
        return "", ""

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 444, 10

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(ingest, "extract_segments", fake_extract_segments)
    monkeypatch.setattr(ingest, "extract_article", fake_extract_article)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid=post_url, title="A tweet", link=post_url)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    prov = json.loads(done.provenance)
    assert "followed_link" not in prov  # boilerplate rejected despite being longer
    assert prov["link"] == post_url
    assert prov["link_follow"] == "skipped: js-shell"


def test_process_episode_rejects_target_not_longer_than_post(monkeypatch):
    """A followed target must be a real improvement — shorter-or-equal never
    replaces the post's own extraction, even if both are substantial."""
    from app import ingest
    from app.db import Episode

    post_url = "https://x.com/demishassabis/status/2"
    article_url = "https://example.com/short-note"
    post_html = (
        '<html><body><a href="https://x.com/demishassabis">profile</a>'
        f'<a href="{article_url}">a short note</a></body></html>'
    )
    post_body = "The post's own substantial text, well past any length floor. " * 4
    target_body = "A shorter target page. " * 2  # real content, but not longer than post_body

    async def fake_fetch_html(url):
        if url == post_url:
            return post_html
        if url == article_url:
            return "<html><body>short target</body></html>"
        raise AssertionError(f"unexpected fetch: {url}")

    def fake_extract_segments(html_text, url=""):
        return "", []

    def fake_extract_article(html_text, url=""):
        if url == post_url:
            return "", post_body
        if url == article_url:
            return "A short note", target_body
        return "", ""

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 555, 10

    monkeypatch.setattr(ingest, "fetch_html", fake_fetch_html)
    monkeypatch.setattr(ingest, "extract_segments", fake_extract_segments)
    monkeypatch.setattr(ingest, "extract_article", fake_extract_article)
    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid=post_url, title="A tweet", link=post_url)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    assert done.status == "ready"
    prov = json.loads(done.provenance)
    assert "followed_link" not in prov
    assert prov["link"] == post_url
    assert prov["link_follow"] == "skipped: not longer than post"


# ── the no-link fallback fed raw HTML to the TTS (ep 330/332, 2026-07-31):
#    a redo of a queue-generated episode found no link, fell back to the
#    longest of source_text/description, and narrated the show-notes markup
#    verbatim — "<p><strong>Source: Inbox – shared articles</strong></p>". ──

def test_no_link_fallback_strips_html_before_narrating(monkeypatch):
    from app import ingest
    from app.db import Episode

    captured = {}

    async def fake_synthesize(script, **kwargs):
        captured["script"] = script
        return "out.mp3", 4321, 90

    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(
            source_slug=inbox.slug, guid="ticktick:42", title="ffmpeg", link="",
            description="<p><strong>Source: Inbox</strong></p>",
            source_text="<p>A brief about the thing. " + "More prose. " * 30 + "</p>",
        )
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    script = captured.get("script", "")
    assert "<p>" not in script and "<strong>" not in script, script[:200]
    assert "A brief about the thing." in script


# ── long articles are narrated WHOLE (ep 239, 2026-07-31: "it is missing a
#    bunch of stuff"). The old 40000 default silently dropped 40-66% of ACX
#    reviews, Zvi roundups and Slow Boring essays — on free edge-tts voices,
#    so it saved nothing. max_chars is opt-in now; when a source does set it,
#    the cut is recorded instead of silent. ──

def test_long_article_is_narrated_in_full_by_default(monkeypatch):
    from app import ingest
    from app.db import Episode

    captured = {}

    async def fake_synthesize(script, **kwargs):
        captured["script"] = script
        return "out.mp3", 4321, 90

    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    assert inbox.max_chars is None, "default must be uncapped"
    long_body = "This is a sentence of the article. " * 3000  # ~105k chars
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid="trunc-1", title="A very long read",
                     link="", source_text=long_body)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    prov = json.loads(done.provenance)
    assert "truncated_chars" not in prov, prov
    # the whole body reached the TTS, not the first 40k
    assert len(captured["script"]) > 100_000, len(captured["script"])


def test_source_that_opts_into_a_cap_still_truncates_and_records_it(monkeypatch):
    from dataclasses import replace

    from app import ingest
    from app.db import Episode

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 4321, 90

    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    capped = replace(next(s for s in config.sources if s.type == "inbox"),
                     max_chars=5000)
    long_body = "This is a sentence of the article. " * 3000
    with db.session() as s:
        ep = Episode(source_slug=capped.slug, guid="trunc-3", title="A capped read",
                     link="", source_text=long_body)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, capped))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    prov = json.loads(done.provenance)
    assert prov["body_chars"] == 5000
    assert prov["truncated_chars"] == len(long_body.strip()) - 5000


def test_short_article_records_no_truncation(monkeypatch):
    from app import ingest
    from app.db import Episode

    async def fake_synthesize(script, **kwargs):
        return "out.mp3", 4321, 90

    monkeypatch.setattr(ingest, "synthesize", fake_synthesize)

    config = load_config()
    inbox = next(s for s in config.sources if s.type == "inbox")
    with db.session() as s:
        ep = Episode(source_slug=inbox.slug, guid="trunc-2", title="A short read",
                     link="", source_text="Just a paragraph of prose. " * 40)
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    _run(ingest.process_episode(ep_id, inbox))

    with db.session() as s:
        done = s.get(Episode, ep_id)
    prov = json.loads(done.provenance)
    assert "truncated_chars" not in prov


# ── a multi-feed source logged only "a feed failed" with feedparser's whole
#    result dict, so finding which of ai-releases' three feeds was serving
#    malformed XML meant re-parsing all of them by hand (2026-08-01). ──

def test_feed_error_reports_the_exception_not_the_dict():
    from app.ingest import _feed_error

    class _Parsed(dict):
        pass

    bozo = _Parsed({"bozo": True, "entries": [], "feed": {},
                    "bozo_exception": ValueError("mismatched tag")})
    assert _feed_error(bozo) == "ValueError: mismatched tag"


def test_feed_error_handles_a_raised_exception():
    from app.ingest import _feed_error
    assert _feed_error(TimeoutError("timed out")) == "TimeoutError: timed out"


def test_feed_error_falls_back_when_there_is_no_exception():
    from app.ingest import _feed_error
    assert _feed_error({"bozo": False, "entries": []}) == "no entries returned"


def _submitted_source(monkeypatch, url: str, **kwargs):
    """Run submit_url with the async spawn stubbed out, returning the SourceDef
    it would have processed the episode with."""
    import app.ingest as ingest

    seen = {}

    async def noop(ep_id, source):
        return None

    def recording(ep_id, source):
        seen["source"] = source
        return noop(ep_id, source)

    monkeypatch.setattr(ingest, "process_episode", recording)
    monkeypatch.setattr(ingest, "spawn", lambda coro: coro.close())
    _run(ingest.submit_url(url, **kwargs))
    return seen["source"]


def test_submit_url_allows_pdfs_from_a_deliberate_share(monkeypatch):
    """A hand-shared URL is as deliberate as a TickTick generate click, which
    already sets allow_pdf. Without it a shared PDF is skipped (ep. 403)."""
    source = _submitted_source(monkeypatch, "https://example.com/paper.pdf",
                               title="Paper")
    assert source.allow_pdf is True
    assert source.type == "inbox"


def test_submit_url_language_override_still_applies(monkeypatch):
    source = _submitted_source(monkeypatch, "https://example.com/dansk",
                               language="da")
    assert source.language == "da"
    assert source.voice == ""
    assert source.allow_pdf is True


def test_requeue_approves_pdfs_for_the_episode_it_requeues(monkeypatch):
    """Unskip/redo is an explicit per-episode approval, so a PDF episode must
    not be skipped straight back by the allow_pdf guard (ep. 403)."""
    from app import db as _db
    from app import web

    seen = {}

    def _noop():
        async def inner():
            return None
        return inner()

    monkeypatch.setattr(web, "spawn", lambda coro: coro.close())
    monkeypatch.setattr(web, "process_episode",
                        lambda ep_id, source: seen.update(source=source) or _noop())

    with _db.session() as s:
        ep = _db.Episode(source_slug="ai-releases", guid="g-pdf-requeue",
                         title="A paper", link="https://example.com/paper.pdf",
                         status="skipped", error="PDF source — not narratable")
        s.add(ep)
        s.commit()
        s.refresh(ep)
        ep_id = ep.id

    web._requeue(ep_id)
    assert seen["source"].allow_pdf is True
    with _db.session() as s:
        assert s.get(_db.Episode, ep_id).status == "pending"


def test_record_available_filters_before_taking_the_newest_few():
    """Slicing first meant a source whose feed is mostly non-matching (ACX open
    threads) surfaced only the matches inside the first `keep` entries."""
    from app.config import SourceDef
    from app.ingest import _record_available

    src = SourceDef(slug="acx", name="ACX", type="rss", url="u",
                    title_filter="^Links")
    recent = [{"title": f"Open Thread {i}", "id": f"o{i}"} for i in range(9)]
    recent += [{"title": "Links For August", "id": "L1"}]
    made = _record_available(src, recent, keep=3)
    assert made == 1, "the one matching entry must be reachable past 9 non-matches"


def test_record_available_still_caps_at_keep():
    from app.config import SourceDef
    from app.ingest import _record_available

    src = SourceDef(slug="capped", name="C", type="rss", url="u")
    recent = [{"title": f"Post {i}", "id": f"p{i}"} for i in range(10)]
    assert _record_available(src, recent, keep=4) == 4


def _flight_page(rows: list[str]) -> str:
    """A Next.js App Router page: Flight rows escaped inside __next_f pushes."""
    import json as _json

    pushes = "".join(
        f"<script>self.__next_f.push([1,{_json.dumps(r)}])</script>" for r in rows
    )
    return f"<html><body>{pushes}</body></html>"


def test_breaking_articles_are_read_from_app_router_flight_rows():
    """DR's front page moved off __NEXT_DATA__ around 2026-08-09; the poll then
    found nothing for a week while warning once per poll and looking healthy."""
    from app.ingest import _collect_breaking, _next_flight_rows

    row = (
        '8:{"items":[{"title":"Stort udslip i Kattegat",'
        '"summary":"Beredskabet er kaldt ud.",'
        '"urlPathId":"/nyheder/indland/stort-udslip",'
        '"publications":[{"breaking":true,"live":false}]},'
        '{"title":"Roligt vejr i vente","summary":"",'
        '"urlPathId":"/nyheder/vejret/roligt",'
        '"publications":[{"breaking":false,"live":false}]}]}'
    )
    page = _flight_page(['2:I[9766,[],""]\n', row + "\n"])
    got = _collect_breaking(_next_flight_rows(page))
    assert [a["urlPathId"] for a in got] == ["/nyheder/indland/stort-udslip"]
    assert got[0]["summary"] == "Beredskabet er kaldt ud."


def test_flight_rows_survive_module_references_and_junk():
    from app.ingest import _next_flight_rows

    page = _flight_page(['1:"$Sreact.fragment"\n3:I[57150,[],""]\nnot-a-row\n4:{"a":1}\n'])
    assert {"a": 1} in _next_flight_rows(page)


def test_a_page_with_neither_shape_yields_no_rows():
    from app.ingest import _next_flight_rows

    assert _next_flight_rows("<html><body>nothing here</body></html>") == []


# ── Image captions ───────────────────────────────────────────────────────
# The article's caption used to be a fallback that the describer read only when
# vision produced no description — so with vision working, which is nearly
# always, the caption was never heard at all.

def _image_blocks(caption, analysis, language="en"):
    from app.ingest import _build_blocks

    seg = {"type": "image", "src": "s1", "caption": caption}
    blocks, _ = _build_blocks(
        title="T", intro="Intro.", segments=[seg],
        main_voice="MAIN", quote_voice="QUOTE", describer_voice="DESC",
        language=language, max_chars=None,
        images_meta={"s1": {"analysis": analysis, "jpeg": None}},
        speaker_voice=lambda name: "SPK",
    )
    return blocks[1:]  # drop the intro block


def test_caption_is_spoken_in_the_article_voice_after_the_description():
    blocks = _image_blocks(
        "Mette Frederiksen outside Christiansborg",
        {"kind": "image", "description": "A woman speaks at a podium."},
    )
    assert [b["voice"] for b in blocks] == ["DESC", "MAIN"]
    assert blocks[0]["text"] == "There is an image here. A woman speaks at a podium."
    # The describer is the app's own narrator; the caption is the publication's
    # own words, so it is read in the voice narrating the article.
    assert blocks[1]["text"] == "Caption: Mette Frederiksen outside Christiansborg"


def test_caption_is_spoken_in_danish_too():
    blocks = _image_blocks(
        "Statsministeren på talerstolen",
        {"kind": "image", "description": "En kvinde taler."},
        language="da",
    )
    assert blocks[1]["text"] == "Billedtekst: Statsministeren på talerstolen"


def test_caption_is_not_read_twice_when_there_is_no_description():
    """It used to stand in for the description; now that it is spoken on its own
    it must not also be borrowed as the describer's line."""
    blocks = _image_blocks("A chart of model releases", {"kind": "image", "description": ""})
    describer = blocks[0]["text"]
    assert "A chart of model releases" not in describer
    assert blocks[-1]["text"] == "Caption: A chart of model releases"


def test_no_caption_block_when_the_article_gave_none():
    blocks = _image_blocks("", {"kind": "image", "description": "A photo of a robot."})
    assert [b["voice"] for b in blocks] == ["DESC"]


def test_text_screenshots_also_get_their_caption_read():
    blocks = _image_blocks(
        "The relevant paragraph",
        {"kind": "text", "description": "d", "text": "Some quoted prose."},
    )
    assert blocks[-1]["voice"] == "MAIN"
    assert blocks[-1]["text"] == "Caption: The relevant paragraph"


def test_the_caption_still_titles_the_chapter():
    blocks = _image_blocks(
        "Mette Frederiksen outside Christiansborg",
        {"kind": "image", "description": "A woman speaks at a podium."},
    )
    assert blocks[0]["chapter"]["title"] == "Mette Frederiksen outside Christiansborg"
    assert blocks[1]["chapter"] is None  # an aside must not fragment the chapter list


def test_cleanup_orphaned_media_removes_unreferenced_old_files():
    """The residue every redo leaves behind: the old audio_file is never
    unlinked when process_episode overwrites the DB row with the new one."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    kept = MEDIA_DIR / "kept.mp3"
    orphan = MEDIA_DIR / "orphan.mp3"
    kept.write_bytes(b"kept")
    orphan.write_bytes(b"orphan")
    old = time.time() - 7200
    os.utime(orphan, (old, old))

    with db.session() as s:
        s.add(Episode(source_slug="x", guid="cleanup-1", title="t", audio_file="kept.mp3"))
        s.commit()

    removed = _run(cleanup_orphaned_media(min_age_seconds=3600))

    assert removed == 1
    assert kept.exists()
    assert not orphan.exists()


def test_cleanup_orphaned_media_leaves_recently_written_files_alone():
    """A file can land on disk moments before the DB commit that references
    it; a sweep must not win that race and delete audio about to be served."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    fresh = MEDIA_DIR / "fresh.mp3"
    fresh.write_bytes(b"fresh")

    removed = _run(cleanup_orphaned_media(min_age_seconds=3600))

    assert removed == 0
    assert fresh.exists()


# ── Danish-perspective claim cooldown (see tests/test_summarize.py) ──────
# The ledger is global across sources and rolling, not calendar: you hear
# thezvi and garymarcus the same morning (eps. 793/800 repeated the same
# figure hours apart), and a Monday reset would let Sunday and Monday repeat.

def _dk_episode(slug, claims, age_days=0):
    from datetime import timedelta

    from app.db import utcnow
    return Episode(
        source_slug=slug, guid=f"g:{slug}:{age_days}", title="t",
        status="ready", provenance=json.dumps({"dk_claims": claims}),
        created_at=utcnow() - timedelta(days=age_days),
    )


@pytest.fixture
def dk_ledger():
    """An empty episode table: the claim ledger is a query over ALL episodes,
    so rows left behind by other tests are indistinguishable from real ones."""
    from sqlmodel import delete
    with db.session() as s:
        s.exec(delete(Episode))
        s.commit()
        yield s


def test_recent_dk_claims_spans_sources_and_drops_stale_ones(dk_ledger):
    from app.ingest import recent_dk_claims

    dk_ledger.add(_dk_episode("thezvi", ["DST | AI adoption | 42% in 2025"], age_days=0))
    dk_ledger.add(_dk_episode("garymarcus", ["Eurostat | EU average | 20%"], age_days=6))
    dk_ledger.add(_dk_episode("slowboring", ["DST | house prices | +6.8%"], age_days=9))
    dk_ledger.commit()

    claims = recent_dk_claims(dk_ledger, days=7)
    assert "DST | AI adoption | 42% in 2025" in claims
    assert "Eurostat | EU average | 20%" in claims
    assert "DST | house prices | +6.8%" not in claims  # outside the window


def test_recent_dk_claims_deduplicates_and_caps(dk_ledger):
    from app.ingest import recent_dk_claims

    for n in range(3):
        dk_ledger.add(_dk_episode(f"dup{n}", ["DST | AI adoption | 42% in 2025"]))
    dk_ledger.commit()

    claims = recent_dk_claims(dk_ledger, days=7)
    assert claims.count("DST | AI adoption | 42% in 2025") == 1
    assert recent_dk_claims(dk_ledger, days=7, limit=0) == []


def test_recent_dk_claims_excludes_the_episode_being_regenerated(dk_ledger):
    # A redo leaves the old provenance in place until process_episode rewrites
    # it at the very end, so without this the segment is forbidden from reusing
    # its OWN figures — and a redo is how Hans asks for a better take.
    from app.ingest import recent_dk_claims

    ep = _dk_episode("redone", ["DST | AI adoption | 42% in 2025"])
    dk_ledger.add(ep)
    dk_ledger.commit()
    dk_ledger.refresh(ep)

    assert recent_dk_claims(dk_ledger, exclude_id=ep.id) == []
    assert recent_dk_claims(dk_ledger) == ["DST | AI adoption | 42% in 2025"]


def test_recent_dk_claims_cap_drops_the_oldest_first(dk_ledger):
    # Inserted so that row id and age DISAGREE: the older episode gets the
    # higher id, so ordering by id would keep exactly the wrong claim.
    from app.ingest import recent_dk_claims

    dk_ledger.add(_dk_episode("newer", ["NEW | y | 2"], age_days=1))
    dk_ledger.commit()
    dk_ledger.add(_dk_episode("older", ["OLD | x | 1"], age_days=5))
    dk_ledger.commit()

    assert recent_dk_claims(dk_ledger, days=7, limit=1) == ["NEW | y | 2"]


# ── Show notes: the budget must count what it actually emits ─────────────
# `used` counted seg["text"] only, so <figure><img src="…long CDN URL…"> was
# free and an image-heavy article sailed past the budget. ep. 573 and ep. 605
# ended at exactly 25000 chars, mid-tag ("</figcapt", "</st"), which feedgen
# wraps in CDATA verbatim.

def test_shownotes_budget_counts_image_markup():
    from app.ingest import _interleaved_shownotes

    images = [{"type": "image", "src": "https://cdn.example.com/" + "x" * 180,
               "caption": ""} for _ in range(50)]
    notes = _interleaved_shownotes("Src", images, "https://example.com", max_chars=2000)
    assert len(notes) < 4000, "image markup must count toward the budget"
    assert notes.endswith("<p>…</p>")


def test_shownotes_budget_counts_escaping_expansion():
    # "&" becomes "&amp;" — five emitted chars for one counted one.
    from app.ingest import _interleaved_shownotes

    segs = [{"type": "text", "text": "&" * 400} for _ in range(20)]
    notes = _interleaved_shownotes("Src", segs, "https://example.com", max_chars=2000)
    assert len(notes) < 6000


def test_shownotes_never_end_inside_a_tag():
    from app.ingest import _safe_truncate_html

    # The real ep. 573 shape: the cut lands inside a closing tag.
    html_ = "<figure><img src='x'/><figcaption>" + "word " * 200 + "</figcaption></figure>"
    for limit in range(40, len(html_), 37):
        out = _safe_truncate_html(html_, limit)
        assert out.rfind("<") <= out.rfind(">"), f"cut inside a tag at limit={limit}"


def test_safe_truncate_leaves_short_html_alone():
    from app.ingest import _safe_truncate_html

    assert _safe_truncate_html("<p>short</p>", 25000) == "<p>short</p>"


# ── Feed fetching: httpx bytes instead of feedparser's own urllib ────────
# _parse_feed_sync mutated the process-global socket.setdefaulttimeout while
# running under asyncio.to_thread inside asyncio.gather across every feed, so
# overlapping calls could interleave save/restore. Separately, parse(url) hides
# the network layer: when hnrss fails ~2 polls in 3 with a constant
# "7:2 mismatched tag", there is no way to see what actually arrived.

_RSS = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
        b"<item><title>One</title><link>http://x/1</link></item>"
        b"<item><title>Two</title><link>http://x/2</link></item></channel></rss>")


def _stub_fetch(monkeypatch, body, status=200, ctype="application/rss+xml"):
    from app import ingest

    async def fake(url):
        return body, status, ctype
    monkeypatch.setattr(ingest, "_fetch_feed_bytes", fake)


def test_parse_feed_reads_entries_from_fetched_bytes(monkeypatch):
    from app import ingest

    _stub_fetch(monkeypatch, _RSS)
    parsed = _run(ingest._parse_feed("http://example.com/feed"))
    assert [e.title for e in parsed.entries] == ["One", "Two"]
    assert parsed.bozo is False


def test_parse_feed_leaves_the_global_socket_timeout_alone(monkeypatch):
    import socket

    from app import ingest

    _stub_fetch(monkeypatch, _RSS)
    socket.setdefaulttimeout(7.5)
    try:
        _run(ingest._parse_feed("http://example.com/feed"))
        assert socket.getdefaulttimeout() == 7.5
    finally:
        socket.setdefaulttimeout(None)


def test_broken_feed_diagnostics_name_status_size_and_first_bytes(monkeypatch):
    from app import ingest

    broken = b"<rss><channel><item><title>x</title></channel></rss>"
    _stub_fetch(monkeypatch, broken, status=200, ctype="text/html")
    monkeypatch.setattr(ingest, "FEED_RETRY_SECONDS", 0)
    parsed = _run(ingest._parse_feed("http://example.com/feed"))
    diag = ingest._feed_diagnostics(broken, 200, "text/html", parsed)
    assert "status=200" in diag
    assert f"bytes={len(broken)}" in diag
    assert "text/html" in diag
    assert "<rss><channel>" in diag          # the head snippet, for eyeballing


def test_feed_diagnostics_snippet_is_bounded(monkeypatch):
    from app import ingest

    huge = b"<rss>" + b"x" * 5000
    assert len(ingest._feed_diagnostics(huge, 200, "application/xml", None)) < 600
