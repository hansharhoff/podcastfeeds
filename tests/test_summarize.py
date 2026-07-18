from datetime import datetime

from app.summarize import (
    is_cruft_line,
    looks_meta,
    scrub_light,
    scrub_regex,
    spoken_date,
)


def test_spoken_date_english():
    assert spoken_date(datetime(2026, 7, 17), "en") == "July 17, 2026"


def test_spoken_date_danish():
    assert spoken_date(datetime(2026, 7, 17), "da") == "17. juli 2026"


def test_scrub_light_unwraps_markdown_link():
    assert scrub_light("Check [this link](https://example.com/very/long/path) out") == \
        "Check this link out"


def test_scrub_light_bare_url_becomes_domain():
    assert scrub_light("See https://example.com/x/y/z now") == "See example.com now"


def test_scrub_light_strips_footnote_markers():
    assert scrub_light("A claim.[1] More text.[12]") == "A claim. More text."


def test_scrub_light_removes_markdown_emphasis():
    assert scrub_light("**bold** and _italic_ and `code`") == "bold and italic and code"


def test_is_cruft_line_matches_cta():
    assert is_cruft_line("Subscribe now") is True
    assert is_cruft_line("Share this post") is True


def test_is_cruft_line_word_boundary():
    # "share" must be a whole word — "Shareholders" is real content.
    assert is_cruft_line("Shareholders gained ground today") is False


def test_is_cruft_line_long_paragraph_kept():
    para = "You should subscribe to more newsletters, " + ("and read widely. " * 15)
    assert len(para) >= 200
    assert is_cruft_line(para) is False


def test_scrub_regex_strips_preamble_and_trailer():
    raw = "Here is a spoken digest script:\n\nReal content line.\n\nI hope this helps!"
    assert scrub_regex(raw) == "Real content line."


def test_looks_meta_detects_commentary():
    assert looks_meta("The text you provided is not actually a script.") is True


def test_looks_meta_passes_real_script():
    assert looks_meta("Today the market rallied on strong earnings across tech.") is False
