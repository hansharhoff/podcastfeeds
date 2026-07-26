from app.extract import (
    detect_language,
    is_paywalled,
    mark_dialogue,
    mark_qa,
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
