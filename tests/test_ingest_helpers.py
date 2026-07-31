import asyncio
import json

from app import db
from app.config import SourceDef, load_config
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
