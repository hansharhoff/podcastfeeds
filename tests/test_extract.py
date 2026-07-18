from app.extract import detect_language, is_paywalled, mark_dialogue, strip_html


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
