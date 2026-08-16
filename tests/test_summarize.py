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


def test_looks_meta_detects_harness_self_talk():
    """Ep. 337: the shim's `claude` CLI loaded Hans' personal skills and
    narrated commentary about the brainstorming skill instead of a book brief."""
    assert looks_meta(
        "I've loaded the brainstorming skill, but I want to flag something: this "
        "skill is designed for designing software projects and features, and its "
        "full process (through design doc, user review, then invoking "
        "writing-plans for an implementation plan) assumes a codebase."
    ) is True


def test_looks_meta_passes_prose_mentioning_skills_and_tools():
    """The guard is phrase-level: ordinary articles talk about skills and tools."""
    assert looks_meta(
        "Reading is a skill that compounds, and the best tool for building it is "
        "a habit. Doerr argues that measurement is what turns intent into action."
    ) is False


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


def test_scrub_light_keeps_the_approximation_tilde():
    """Stripping every ~ (added 2026-08-08 for stray strikethrough markers)
    turned rounded figures into exact claims: "~$5B" was read as "$5B"."""
    from app.summarize import scrub_light

    assert scrub_light("roughly ~50 mio. and ~$5B") == "roughly ~50 mio. and ~$5B"


def test_scrub_light_still_strips_strikethrough_markers():
    from app.summarize import scrub_light

    assert scrub_light("it was ~~cancelled~~ postponed") == "it was cancelled postponed"


def test_digest_prompt_states_the_period_it_covers():
    """The prompt said nothing about cadence, so a daily digest opened "This
    week, we're looking at..." and signed off "That's this week's digest"
    (ep. 443 feedback: "it is not ai announcements for the week, rather the day")."""
    import asyncio

    from app import summarize

    seen = {}  # scrub_script calls llm too; keep the first (digest) call

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        seen.setdefault("prompt", prompt)
        seen.setdefault("tools", tools)
        return "A spoken script. " * 40

    old = summarize.llm
    summarize.llm = fake_llm
    try:
        asyncio.get_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "August 15th, 2026",
            [{"title": "T", "summary": "S"}], "en", window="the last 24 hours"))
    finally:
        summarize.llm = old

    assert "the last 24 hours" in seen["prompt"]
    assert "never describe it as any other span of time" in seen["prompt"]
    assert seen["tools"] == ["WebSearch"], "research is what earns the length"


def test_digest_window_is_measured_not_guessed():
    from datetime import UTC, datetime, timedelta

    from app.ingest import _digest_window

    now = datetime(2026, 8, 15, 5, 0, tzinfo=UTC)
    assert _digest_window(now - timedelta(hours=24), now, "en") == "the last 24 hours"
    assert _digest_window(now - timedelta(days=7), now, "en") == "the last 7 days"
    assert _digest_window(now - timedelta(hours=24), now, "da") == "de seneste 24 timer"


def test_digest_window_never_returns_zero_hours():
    """A digest rebuilt moments after the last one must still name a period."""
    from datetime import UTC, datetime

    from app.ingest import _digest_window

    now = datetime(2026, 8, 15, 5, 0, tzinfo=UTC)
    assert _digest_window(now, now, "en") == "the last 1 hours"


def test_digest_prompt_forbids_inventing_specifics():
    """Asked for a word count off a two-line summary, a dry run manufactured
    "after watching thousands of Claude Code sessions" — reporting-shaped
    detail that was in neither the item nor any search result."""
    import asyncio

    from app import summarize

    seen = {}

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        seen.setdefault("prompt", prompt)
        return "A spoken script. " * 40

    old = summarize.llm
    summarize.llm = fake_llm
    try:
        asyncio.get_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))
    finally:
        summarize.llm = old

    assert "Never invent" in seen["prompt"]
