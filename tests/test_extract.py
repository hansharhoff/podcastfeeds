from app.extract import (
    detect_language,
    is_link_forwarding_post,
    is_paywalled,
    is_short_link,
    looks_like_boilerplate,
    mark_dialogue,
    mark_qa,
    outbound_link,
    strip_html,
)


def test_strip_html_removes_tags_and_unescapes():
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"
    assert strip_html("Tom &amp; Jerry") == "Tom & Jerry"


def test_strip_html_block_tags_become_newlines():
    # </p> becomes a newline; the following <p> becomes a leading space.
    assert strip_html("<p>one</p><p>two</p>") == "one\n two"


def test_strip_html_drops_script_and_style():
    assert strip_html("<style>.a{color:red}</style>Keep") == "Keep"
    assert strip_html("<script>evil()</script>Keep") == "Keep"


def test_is_paywalled_direct_marker():
    assert is_paywalled("This post is for paid subscribers.") is True


def test_is_paywalled_short_body_with_marker_in_html():
    assert is_paywalled("short stub", html="<div>subscribe to keep reading</div>") is True


def test_is_paywalled_normal_article():
    assert is_paywalled("A perfectly ordinary article body with no paywall.") is False


def test_detect_language_english():
    assert detect_language("the quick brown fox jumps over the lazy dog again") == "en"


def test_detect_language_danish_via_special_chars():
    assert detect_language("æøå æøå æøå en to tre fire fem") == "da"


def test_detect_language_empty_defaults_english():
    assert detect_language("") == "en"


def _text(s):
    return {"type": "text", "text": s}


def test_mark_dialogue_converts_interview():
    segs = [
        _text("Alice: Hello there."),
        _text("Bob: Hi Alice."),
        _text("Alice: How are you?"),
        _text("Bob: Doing great."),
    ]
    out = mark_dialogue(segs)
    assert out[0] == {"type": "dialogue", "speaker": "Alice", "text": "Hello there."}
    assert out[1] == {"type": "dialogue", "speaker": "Bob", "text": "Hi Alice."}
    assert all(s["type"] == "dialogue" for s in out)


def test_mark_dialogue_leaves_non_interview_untouched():
    # Only one speaker appears twice -> not an interview; returned unchanged.
    segs = [_text("She asks: what now?"), _text("A plain paragraph."), _text("Another one.")]
    assert mark_dialogue(segs) is segs


def test_mark_qa_tags_mailbag_questions():
    segs = [
        _text("Why are Coloradans overrepresented in the comments?"),
        _text("I don't know, but several of the team are from Colorado."),
        _text("Is the AI backlash uniquely anti-tech?"),
        _text("A little of both, honestly."),
        _text("What should Democrats actually do about it?"),
        _text("Focus on abundance and permitting reform."),
    ]
    out = mark_qa(segs)
    assert [s["type"] for s in out] == [
        "question", "text", "question", "text", "question", "text"
    ]
    assert out[0]["text"].endswith("?")


def test_mark_qa_leaves_normal_article_untouched():
    segs = [
        _text("A paragraph of ordinary prose."),
        _text("Is this one rhetorical question enough?"),
        _text("It elaborates on the point at length."),
        _text("More prose here."),
        _text("Even more prose."),
        _text("A concluding thought."),
    ]
    assert mark_qa(segs) is segs  # only one question -> unchanged


def test_mark_qa_ignores_overlong_questions():
    long_q = "This rambles on and on " * 40 + "?"
    segs = [_text(long_q), _text("answer"),
            _text(long_q), _text("answer"),
            _text(long_q), _text("answer")]
    assert mark_qa(segs) is segs  # questions too long to be reader questions


def test_mark_dialogue_continuation_inherits_speaker():
    segs = [
        _text("Alice: First line."),
        _text("A continuation with no label."),
        _text("Bob: Reply one."),
        _text("Alice: Second."),
        _text("Bob: Reply two."),
    ]
    out = mark_dialogue(segs)
    assert out[1] == {"type": "dialogue", "speaker": "Alice", "text": "A continuation with no label."}


# ── per-publication sessions: a host-specific cookie key must win over the
#    generic substack.com one (noahpinion sub lives on another account) ────

def test_cookie_for_prefers_most_specific_domain(monkeypatch):
    from app import extract
    monkeypatch.setattr("app.config.load_cookies", lambda: {
        "substack.com": "substack.sid=MAIN",
        "noahpinion.substack.com": "substack.sid=GMAIL",
    })
    assert extract._cookie_for("https://noahpinion.substack.com/api/v1/posts/x") == "substack.sid=GMAIL"
    assert extract._cookie_for("https://matthewyglesias.substack.com/feed") == "substack.sid=MAIN"


def test_cookie_for_specific_key_wins_regardless_of_dict_order(monkeypatch):
    from app import extract
    monkeypatch.setattr("app.config.load_cookies", lambda: {
        "noahpinion.substack.com": "substack.sid=GMAIL",
        "substack.com": "substack.sid=MAIN",
    })
    assert extract._cookie_for("https://noahpinion.substack.com/") == "substack.sid=GMAIL"
    assert extract._cookie_for("https://substack.com/") == "substack.sid=MAIN"


# ── nested lists must not narrate passages twice (ep 236 feedback,
#    2026-07-25): child.iter("li") visited nested <li> AND the outer <li>'s
#    text_content() already contained them, so every nested item was read
#    twice. Zvi's nested-comment style hit this hard. ──

def test_segments_from_clean_html_nested_lists_no_duplication():
    from app.extract import segments_from_clean_html
    html = (
        "<ol>"
        "<li><p>Catastrophic misuse</p>"
        "<ol><li><p>Boaz thinks cyber is defense dominant.</p></li>"
        "<li><p>For CBRN defenders need an edge.</p></li></ol></li>"
        "<li><p>Second top-level point</p></li>"
        "</ol>"
    )
    _, segments = segments_from_clean_html(html)
    texts = [s["text"] for s in segments if s["type"] == "text"]
    joined = " ".join(texts)
    assert joined.count("Boaz thinks cyber is defense dominant.") == 1
    assert joined.count("For CBRN defenders need an edge.") == 1
    assert joined.count("Catastrophic misuse") == 1
    assert joined.count("Second top-level point") == 1
    # nested items still present as their own segments, in reading order
    assert texts.index("Catastrophic misuse") < texts.index(
        "Boaz thinks cyber is defense dominant.")


def test_segments_from_clean_html_flat_list_unchanged():
    from app.extract import segments_from_clean_html
    html = "<ul><li>alpha one</li><li>beta two</li></ul>"
    _, segments = segments_from_clean_html(html)
    assert [s["text"] for s in segments] == ["alpha one", "beta two"]


# ── images inside list items (ep 380 feedback, 2026-07-31: "Missing a lot of
#    screenshot and other contents here"). The ep-236 nested-list fix above
#    walked each <li> for TEXT only, so Gary Marcus's listicle — where every
#    point is a sentence followed by a screenshot — narrated 0 of its 5
#    images and left dangling colons behind. ──

def test_segments_from_clean_html_keeps_images_inside_list_items():
    from app.extract import segments_from_clean_html
    html = (
        "<ol>"
        "<li><p>Summary that appeared on X:</p>"
        '<div class="captioned-image-container"><figure>'
        '<a href="https://substackcdn.com/image/fetch/shot.png"></a>'
        "<figcaption>The thread</figcaption></figure></div></li>"
        "<li><p>Second point</p></li>"
        "</ol>"
    )
    _, segments = segments_from_clean_html(html)
    kinds = [s["type"] for s in segments]
    assert kinds == ["text", "image", "text"], kinds
    assert segments[1]["src"] == "https://substackcdn.com/image/fetch/shot.png"
    assert segments[1]["caption"] == "The thread"


def test_segments_from_clean_html_keeps_images_in_nested_list_items():
    from app.extract import segments_from_clean_html
    html = (
        "<ul><li><p>Outer</p>"
        "<ul><li><p>Inner</p>"
        '<figure><img src="https://cdn.example/inner.jpg"></figure>'
        "</li></ul></li></ul>"
    )
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text", "text", "image"]
    assert segments[2]["src"] == "https://cdn.example/inner.jpg"


def test_segments_from_clean_html_keeps_image_wrapped_in_a_div_inside_a_list_item():
    # Not every publication puts the figure directly in the <li>; a wrapper
    # div would otherwise be flattened to text and the image lost.
    from app.extract import segments_from_clean_html
    html = (
        "<ol><li><p>The point</p>"
        '<div class="wrapper"><div><figure>'
        '<img src="https://cdn.example/deep.jpg"></figure></div></div>'
        "</li></ol>"
    )
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text", "image"]
    assert segments[1]["src"] == "https://cdn.example/deep.jpg"


def test_list_item_paragraph_with_inline_image_keeps_both():
    from app.extract import segments_from_clean_html
    html = (
        "<ul><li><p>Look at this "
        '<img src="https://cdn.example/inline.jpg"></p></li></ul>'
    )
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text", "image"]
    assert segments[0]["text"] == "Look at this"
    assert segments[1]["src"] == "https://cdn.example/inline.jpg"


def test_list_item_image_does_not_duplicate_its_caption_as_text():
    # The figcaption belongs to the image segment; it must not also be swept
    # into the <li>'s own text (that would narrate it twice).
    from app.extract import segments_from_clean_html
    html = (
        "<ol><li><p>The point</p>"
        '<figure><img src="https://cdn.example/a.jpg">'
        "<figcaption>Caption text</figcaption></figure></li></ol>"
    )
    _, segments = segments_from_clean_html(html)
    texts = [s["text"] for s in segments if s["type"] == "text"]
    assert texts == ["The point"]


# ── Substack tweet embeds (same ep 380 feedback). A quoted tweet is rendered
#    as <div class="twitter-embed" data-attrs="{json}"> with NO text content —
#    the tweet body lives only in the JSON. The DOM walk descended into it and
#    emitted nothing, so "spotted by :" trailed off into silence. ──

_TWEET_ATTRS = (
    '{"url":"https://x.com/ns123abc/status/2082922547406848279",'
    '"full_text":"WE&#39;RE CLOSE TO AGI. GIVE ME 500 BILLIONS.",'
    '"username":"ns123abc","name":"NIK",'
    '"photos":[{"img_url":"https://pbs.substack.com/media/HOgHBJa.jpg"}]}'
)


def test_tweet_embed_becomes_a_quote_segment():
    from app.extract import segments_from_clean_html
    html = f'<div class="twitter-embed" data-attrs=\'{_TWEET_ATTRS}\'></div>'
    _, segments = segments_from_clean_html(html)
    quotes = [s for s in segments if s["type"] == "quote"]
    assert len(quotes) == 1
    assert "WE'RE CLOSE TO AGI" in quotes[0]["text"]
    assert "NIK" in quotes[0]["text"]


def test_tweet_embed_photo_becomes_an_image_segment():
    from app.extract import segments_from_clean_html
    html = f'<div class="twitter-embed" data-attrs=\'{_TWEET_ATTRS}\'></div>'
    _, segments = segments_from_clean_html(html)
    images = [s for s in segments if s["type"] == "image"]
    assert [i["src"] for i in images] == [
        "https://pbs.substack.com/media/HOgHBJa.jpg"]


def test_tweet_embed_inside_list_item_is_kept():
    from app.extract import segments_from_clean_html
    html = (
        "<ol><li><p>spotted by:</p>"
        f'<div class="twitter-embed" data-attrs=\'{_TWEET_ATTRS}\'></div>'
        "</li></ol>"
    )
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text", "quote", "image"]


def test_tweet_greentext_markers_become_sentences():
    # ">" line markers would be read aloud as "greater than" (or dropped,
    # merging six shouted lines into one run-on). Each line is a sentence.
    from app.extract import _speakable_tweet
    assert _speakable_tweet(">CLOSE TO AGI \n>GIVE ME 500 BILLIONS\n>I PROMISE!") == (
        "CLOSE TO AGI. GIVE ME 500 BILLIONS. I PROMISE!")


def test_tweet_text_drops_bare_urls():
    from app.extract import segments_from_clean_html
    attrs = '{"full_text":"read this https://t.co/abc123 now","name":"Ann"}'
    html_ = f'<div class="twitter-embed" data-attrs=\'{attrs}\'></div>'
    _, segments = segments_from_clean_html(html_)
    assert segments[0]["text"] == "Ann on X: read this now."


def test_textless_tweet_embed_is_skipped():
    from app.extract import segments_from_clean_html
    html = '<div class="twitter-embed" data-attrs=\'{"username":"x"}\'></div>'
    _, segments = segments_from_clean_html(html)
    assert segments == []


def test_malformed_embed_attrs_do_not_crash():
    from app.extract import segments_from_clean_html
    html = '<div class="twitter-embed" data-attrs="not json {{"></div><p>after</p>'
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text"]


def test_subscribe_widget_embed_is_not_narrated():
    from app.extract import segments_from_clean_html
    html = (
        '<div class="subscribe-widget" data-attrs=\'{"text":"Subscribe now"}\'>'
        "</div><p>real body</p>"
    )
    _, segments = segments_from_clean_html(html)
    assert [s["text"] for s in segments] == ["real body"]


# ── link-forwarding social posts (X/Bluesky/Mastodon/Threads): the post is a
#    pointer, not the payload — follow it to the article it links to (ep. 253
#    feedback: Demis Hassabis's tweet pointing at his essay got narrated as a
#    2439-char tweet-page excerpt instead of the essay). ──

def test_is_link_forwarding_post_recognizes_known_hosts():
    assert is_link_forwarding_post("https://x.com/demishassabis/status/1") is True
    assert is_link_forwarding_post("https://twitter.com/user/status/1") is True
    assert is_link_forwarding_post("https://mobile.twitter.com/user/status/1") is True
    assert is_link_forwarding_post("https://bsky.app/profile/user.bsky.social/post/1") is True
    assert is_link_forwarding_post("https://www.threads.net/@user/post/1") is True
    assert is_link_forwarding_post("https://mastodon.social/@user/1") is True


def test_is_link_forwarding_post_ignores_ordinary_sites():
    assert is_link_forwarding_post("https://noahpinion.blog/p/some-post") is False
    assert is_link_forwarding_post("https://deepmind.google/blog/a-framework") is False


def test_outbound_link_finds_first_external_link_skipping_platform_and_cdn():
    # Shape mirrors a real X post page: a self-link (profile), a CDN image
    # (not an <a>, ignored anyway), a login nav link, then the actual outbound
    # link the poster is pointing at.
    html = (
        '<html><body>'
        '<a href="https://x.com/demishassabis">@demishassabis</a>'
        '<p>Sharing our new framework for frontier AI.</p>'
        '<a href="https://twitter.com/i/flow/login">Log in</a>'
        '<a href="https://t.co/AbCdEfGhIj">deepmind.google/blog/a-framewo…</a>'
        '</body></html>'
    )
    assert outbound_link(html, "https://x.com/demishassabis/status/1") == "https://t.co/AbCdEfGhIj"


def test_outbound_link_skips_twimg_cdn_and_other_platform_hosts():
    html = (
        '<html><body>'
        '<img src="https://pbs.twimg.com/profile_images/foo.jpg">'
        '<a href="https://pbs.twimg.com/media/bar.jpg">image</a>'
        '<a href="https://bsky.app/profile/other/post/2">another post</a>'
        '<a href="https://example.com/article">the real article</a>'
        '</body></html>'
    )
    assert outbound_link(html, "https://x.com/demishassabis/status/1") == "https://example.com/article"


def test_outbound_link_returns_empty_when_nothing_plausible():
    html = (
        '<html><body>'
        '<a href="https://x.com/demishassabis">profile</a>'
        '<a href="#reply">reply</a>'
        '<a href="mailto:someone@example.com">mail</a>'
        '</body></html>'
    )
    assert outbound_link(html, "https://x.com/demishassabis/status/1") == ""


def test_outbound_link_handles_unparsable_html():
    assert outbound_link("<<<not html", "https://x.com/a/status/1") == ""


# ── live-test findings (real fetch of the ep. 253 post, 2076957440109625718):
#    X's 40 anchors are all x.com/twitter.com nav; the actual outbound link
#    (a t.co short link) appears only in og:description-style meta content
#    and in embedded JSON — never in an <a href>. ──

def test_outbound_link_discovers_link_in_meta_description_only():
    html = (
        '<html><head>'
        '<meta property="og:description" content="https://t.co/PTeDiv1b6L" nonce="x"/>'
        '<meta name="description" content="https://t.co/PTeDiv1b6L" nonce="x"/>'
        '<meta name="twitter:description" content="https://t.co/PTeDiv1b6L" nonce="x"/>'
        '</head><body>'
        '<a href="https://x.com/demishassabis">profile</a>'
        '<a href="https://twitter.com/i/flow/login">Log in</a>'
        '</body></html>'
    )
    assert outbound_link(html, "https://x.com/demishassabis/status/1") == "https://t.co/PTeDiv1b6L"


def test_outbound_link_discovers_raw_tco_with_no_meta_or_anchor():
    html = '<html><body><script>state={"text":"https://t.co/AbC123xyz"}</script></body></html>'
    assert outbound_link(html, "https://x.com/a/status/1") == "https://t.co/AbC123xyz"


def test_is_short_link_recognizes_tco_only():
    assert is_short_link("https://t.co/AbC123") is True
    assert is_short_link("https://example.com/article") is False


def test_looks_like_boilerplate_detects_js_shell_strings():
    # Verbatim strings from the live target fetch (x.com/i/article/... behind
    # a t.co redirect): a JS-only render, no JS -> boilerplate, not content.
    assert looks_like_boilerplate(
        "We've detected that JavaScript is disabled in this browser. Please "
        "enable JavaScript or switch to a supported browser to continue."
    ) is True
    assert looks_like_boilerplate("A Framework for Frontier AI. " * 30) is False


def test_resolve_short_link_blocks_non_public_targets_without_fetching():
    from app.extract import resolve_short_link
    # SSRF guard fires before any network attempt (loopback resolves purely
    # locally, so this assertion stays offline) — resolution returns the
    # original URL unchanged rather than fetching it.
    blocked = "http://127.0.0.1:8000/evil"
    assert _run_async(resolve_short_link(blocked)) == blocked


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


def _run_async(coro):
    """Run an async coroutine the same way as test_health.py does."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_fetch_pdf_text_blocks_non_public_addresses():
    import pytest

    from app.extract import fetch_pdf_text

    # Verify that fetch_pdf_text refuses to fetch from private addresses
    # before making any network request (SSRF guard).
    with pytest.raises(RuntimeError, match="refusing to fetch non-public address"):
        _run_async(fetch_pdf_text("http://127.0.0.1:8000/document.pdf"))


# ── footnotes (ep 310 feedback, 2026-08-01: "Try to handle footnotes
#    correctly, inline and with an announcement like: 'footnote: ...' where the
#    footnote is emphasized"). Substack puts a bare marker digit inline and the
#    bodies in a block at the very end, so the voice read "…at an astonishing
#    rate. One." and then recited orphaned sentences after the outro. ──

_FN_BODY = (
    '<p>Universities hired administrators at an astonishing rate.'
    '<a class="footnote-anchor" data-component-name="FootnoteAnchorToDOM"'
    ' id="footnote-anchor-1" href="#footnote-1" target="_self">1</a> </p>'
    '<p>But replacing churches proved harder.'
    '<a class="footnote-anchor" id="footnote-anchor-2" href="#footnote-2">2</a></p>'
    '<div class="footnote" data-component-name="FootnoteToDOM">'
    '<a id="footnote-1" href="#footnote-anchor-1" class="footnote-number">1</a>'
    '<div class="footnote-content"><p>Administrator bloat is well documented.</p>'
    "</div></div>"
    '<div class="footnote" data-component-name="FootnoteToDOM">'
    '<a id="footnote-2" href="#footnote-anchor-2" class="footnote-number">2</a>'
    '<div class="footnote-content"><p>With, of course, the notable exception '
    "of Black voters.</p></div></div>"
)


def test_footnote_body_follows_its_own_paragraph():
    from app.extract import segments_from_clean_html
    _, segments = segments_from_clean_html(_FN_BODY)
    assert [s["type"] for s in segments] == [
        "text", "footnote", "text", "footnote"], segments
    assert segments[1]["text"] == "Administrator bloat is well documented."
    assert segments[3]["text"].startswith("With, of course,")


def test_footnote_marker_digit_is_not_spoken_in_the_sentence():
    from app.extract import segments_from_clean_html
    _, segments = segments_from_clean_html(_FN_BODY)
    first = segments[0]["text"]
    assert first == "Universities hired administrators at an astonishing rate."
    assert not first.endswith("1")


def test_footnote_bodies_do_not_also_appear_as_trailing_prose():
    # The definition block used to survive as a bare text segment at the end.
    from app.extract import segments_from_clean_html
    _, segments = segments_from_clean_html(_FN_BODY)
    texts = [s["text"] for s in segments if s["type"] == "text"]
    assert not any("Administrator bloat" in t for t in texts)
    assert not any("Black voters" in t for t in texts)


def test_footnote_inside_a_list_item_is_kept():
    from app.extract import segments_from_clean_html
    html = (
        "<ul><li>A point worth qualifying"
        '<a class="footnote-anchor" href="#footnote-1">1</a></li></ul>'
        '<div class="footnote"><a class="footnote-number">1</a>'
        '<div class="footnote-content"><p>The qualification.</p></div></div>'
    )
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text", "footnote"]
    assert segments[0]["text"] == "A point worth qualifying"
    assert segments[1]["text"] == "The qualification."


def test_orphan_anchor_without_a_definition_is_still_stripped():
    from app.extract import segments_from_clean_html
    html = ('<p>A claim<a class="footnote-anchor" href="#footnote-9">9</a></p>')
    _, segments = segments_from_clean_html(html)
    assert [s["type"] for s in segments] == ["text"]
    assert segments[0]["text"] == "A claim"


def test_body_without_footnotes_is_unchanged():
    from app.extract import segments_from_clean_html
    _, segments = segments_from_clean_html("<p>Plain prose.</p><p>More prose.</p>")
    assert [s["type"] for s in segments] == ["text", "text"]
    assert [s["text"] for s in segments] == ["Plain prose.", "More prose."]
