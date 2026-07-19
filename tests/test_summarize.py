from datetime import datetime

from app.summarize import (
    has_markdown_table,
    is_cruft_line,
    linearize_markdown_tables,
    looks_meta,
    scrub_light,
    scrub_regex,
    spoken_date,
)

# The exact table that ep. 232 read aloud verbatim (pipes and all).
EP232_TABLE = (
    "Category | Subgroup | Mostly a good thing - people could pursue what matters to them"
    " | Mostly a bad thing - people need jobs to have purpose and dignity | Not sure\n"
    "---|---|---|---|---\n"
    " | Overall | 20.5% | 66.8% | 12.7%"
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


def test_has_markdown_table_detects_pipe_table():
    assert has_markdown_table(EP232_TABLE) is True
    assert has_markdown_table("Just a normal sentence with no table.") is False
    assert has_markdown_table("A sentence | with one pipe but no separator row.") is False


def test_linearize_table_removes_all_markdown_syntax():
    out = linearize_markdown_tables(EP232_TABLE)
    assert "|" not in out
    assert "---" not in out


def test_linearize_table_conveys_figures_as_prose():
    out = linearize_markdown_tables(EP232_TABLE)
    assert "Subgroup: Overall" in out
    assert "Not sure: 12.7%" in out
    assert "66.8%" in out
    # header text is paired with its cell value
    assert "Mostly a good thing - people could pursue what matters to them: 20.5%" in out


def test_linearize_leaves_plain_prose_untouched():
    prose = "This is a paragraph.\nWith two lines and no table at all."
    assert linearize_markdown_tables(prose) == prose


def test_linearize_preserves_surrounding_prose():
    text = "Here are the results:\n" + EP232_TABLE + "\nThat is the full picture."
    out = linearize_markdown_tables(text)
    assert out.startswith("Here are the results:")
    assert out.rstrip().endswith("That is the full picture.")
    assert "|" not in out


def test_scrub_light_linearizes_tables():
    # scrub_light is the choke point every spoken block passes through.
    out = scrub_light(EP232_TABLE)
    assert "|" not in out and "---" not in out
    assert "12.7%" in out
