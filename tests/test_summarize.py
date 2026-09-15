import contextlib
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


@contextlib.contextmanager
def _stub_llm(module, fake, searches=None):
    """Stub BOTH llm() and llm_with_meta(); digest_script uses the latter and an
    unpatched test would otherwise reach the live backend."""
    async def fake_with_meta(prompt, model="", tools=None, thinking=False):
        return await fake(prompt, model, tools, thinking), {"searches": searches}

    old_llm, old_meta = module.llm, module.llm_with_meta
    module.llm, module.llm_with_meta = fake, fake_with_meta
    try:
        yield
    finally:
        module.llm, module.llm_with_meta = old_llm, old_meta


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

    with _stub_llm(summarize, fake_llm):
        asyncio.get_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "August 15th, 2026",
            [{"title": "T", "summary": "S"}], "en", window="the last 24 hours"))

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

    with _stub_llm(summarize, fake_llm):
        asyncio.get_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert "Never invent" in seen["prompt"]


# ── Attribution guard (ep. 572, 2026-08-18) ──────────────────────────────
# 168 characters of feed text became a 557-word script asserting that
# "eighty-seven percent of security professionals" see more AI-driven threats,
# sourced to "According to recent reporting". The figure may even be real —
# nothing recorded at the time could tell anyone either way.

VAGUE = [
    "According to recent reporting, eighty-seven percent of professionals agree.",
    "According to the data, adoption doubled.",
    "Reports suggest the rollout slipped.",
    "The firm reportedly reached external systems through a misconfiguration.",
    "Studies show adoption is rising.",
    "Analysts say the market will double.",
    "It has been widely reported that the launch slipped.",
    "Ifølge nylige rapporter er tallet steget.",
    "Virksomheden har angiveligt mistet data.",
    "Undersøgelser viser, at tilliden falder.",
]

# The guard must leave real attribution alone. Widening it until these trip is
# how it stops being a guard and starts being a rewrite.
NAMED = [
    "According to OpenAI, the model ships in October.",
    "According to the Financial Times, revenue doubled.",
    "Anthropic reports that usage tripled last quarter.",
    "The GitHub issue says the feature is opt-out by default.",
    "A study published by Stanford found the opposite effect.",
    "Ifølge DR er sagen afgjort.",
    "The company's own security manifesto puts the figure at twelve percent.",
    "Reuters and Bloomberg both covered the acquisition.",
]


def test_vague_attribution_is_caught():
    from app.summarize import vague_attributions

    for line in VAGUE:
        assert vague_attributions(line), f"missed vague attribution: {line}"


def test_named_attribution_is_left_alone():
    from app.summarize import vague_attributions

    for line in NAMED:
        assert not vague_attributions(line), f"false positive on: {line}"


def test_has_figures_finds_spoken_and_written_numbers():
    from app.summarize import has_figures

    assert has_figures("eighty-seven percent of security professionals")
    assert has_figures("roughly 94 percent of organizations")
    assert has_figures("a 2.5 billion dollar round")
    assert has_figures("omkring 40 procent af virksomhederne")
    assert not has_figures("The company shipped a new model this week.")


def test_digest_prompt_forbids_vague_attribution_by_name():
    """The old rule ("attribute anything you did find") was satisfied by
    "According to recent reporting". The new one names the failure."""
    import asyncio

    from app import summarize

    seen = {}

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        seen.setdefault("prompt", prompt)
        return "A spoken script. " * 40

    with _stub_llm(summarize, fake_llm):
        asyncio.new_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert "according to recent reporting" in seen["prompt"].lower()
    assert "leave the figure out" in seen["prompt"]


def test_digest_records_the_searches_it_actually_ran():
    import asyncio

    from app import summarize

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        return "A spoken script about models. " * 30

    searches = [{"tool": "WebSearch", "query": "claude code session urls",
                 "urls": ["https://github.com/anthropics/claude-code/issues/66504"]}]
    with _stub_llm(summarize, fake_llm, searches=searches):
        _, prov = asyncio.new_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert prov["searches"] == 1
    assert prov["search_queries"] == ["claude code session urls"]
    assert prov["search_urls"] == ["https://github.com/anthropics/claude-code/issues/66504"]
    assert prov["input_chars"] == 2  # "T" + "S" — the whole basis for the script


def test_digest_flags_figures_that_no_search_supports():
    """The signature of the failure: hard numbers in, nothing looked up."""
    import asyncio

    from app import summarize

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        return "Eighty-seven percent of teams now use it. " * 20

    with _stub_llm(summarize, fake_llm, searches=[]):
        _, prov = asyncio.new_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert prov["searches"] == 0
    assert prov["unsourced_figures"] is True


def test_unknown_search_count_is_not_reported_as_zero():
    """The CLI fallback cannot observe tool use. None means unknown; recording
    it as 0 would frame every fallback digest as unresearched."""
    import asyncio

    from app import summarize

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        return "Eighty-seven percent of teams now use it. " * 20

    with _stub_llm(summarize, fake_llm, searches=None):
        _, prov = asyncio.new_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert prov["searches"] is None
    assert "unsourced_figures" not in prov


def test_vague_attribution_triggers_a_repair_pass():
    import asyncio

    from app import summarize

    calls = []

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        calls.append(prompt)
        if "credits claims to nobody checkable" in prompt:
            return "The Verge reports that eighty-seven percent of teams use it. " * 20
        if "final editor" in prompt:  # scrub pass: echo back what it was handed
            return prompt.split("Script:\n", 1)[1]
        return "According to recent reporting, eighty-seven percent of teams use it. " * 20

    with _stub_llm(summarize, fake_llm, searches=[]):
        script, prov = asyncio.new_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert any("credits claims to nobody checkable" in c for c in calls), "no repair pass ran"
    assert "According to recent reporting" not in script
    assert prov["vague_attribution"] == 0


def test_a_repair_that_guts_the_script_is_discarded():
    """Deleting an unattributable claim is right; deleting the episode is not."""
    import asyncio

    from app import summarize

    original = "According to recent reporting, teams use it. " * 20

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        if "credits claims to nobody checkable" in prompt:
            return "Teams use it."
        if "final editor" in prompt:  # scrub pass: echo back what it was handed
            return prompt.split("Script:\n", 1)[1]
        return original

    with _stub_llm(summarize, fake_llm, searches=[]):
        script, prov = asyncio.new_event_loop().run_until_complete(summarize.digest_script(
            "AI Announcements", "d", [{"title": "T", "summary": "S"}], "en"))

    assert "According to recent reporting" in script
    assert prov["vague_attribution"] > 0  # kept, and recorded as still offending


def test_vision_prompt_asks_for_names_but_forbids_guessing():
    from app.summarize import VISION_PROMPT

    prompt = VISION_PROMPT.format(lang_name="English")
    assert "IDENTIFY WHAT YOU CAN" in prompt
    assert "never infer a name" in prompt
    assert "wrong name" in prompt


# ── Danish-perspective claim cooldown ────────────────────────────────────
# Danmarks Statistik's AI-adoption figure ("15% in 2023 -> 28% -> 42% in 2025")
# was recited in 14 of 45 days of episodes, twice on some days. The segment now
# reports the claims it made so the next week's segments can be told to avoid
# them.

_DK_SEGMENT = (
    "And now, the view from Denmark. " + "This is a real Danish segment. " * 20
)


def test_split_claims_separates_trailer_from_spoken_text():
    from app.summarize import split_claims

    text, claims = split_claims(
        "Spoken words.\n---CLAIMS---\n"
        "Danmarks Statistik | Danish firms using AI | 42% in 2025\n"
        "- Eurostat | EU average firm AI use | 20% in 2025\n"
    )
    assert text == "Spoken words."
    assert claims == [
        "Danmarks Statistik | Danish firms using AI | 42% in 2025",
        "Eurostat | EU average firm AI use | 20% in 2025",
    ]


def test_split_claims_tolerates_a_missing_trailer():
    from app.summarize import split_claims

    assert split_claims("Just the segment.") == ("Just the segment.", [])


def test_danish_perspective_records_claims_and_never_narrates_them(monkeypatch):
    import asyncio

    from app import summarize

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        if "final editor" in prompt:  # scrub pass: echo back what it was handed
            return prompt.split("Script:\n", 1)[1]
        return (_DK_SEGMENT + "\n---CLAIMS---\n"
                "Danmarks Statistik | Danish firms using AI | 42% in 2025")

    with _stub_llm(summarize, fake_llm):
        segment, prov = asyncio.new_event_loop().run_until_complete(
            summarize.danish_perspective("T", "body", "en"))

    assert "---CLAIMS---" not in segment
    assert "Danmarks Statistik" not in segment
    assert prov["dk_claims"] == [
        "Danmarks Statistik | Danish firms using AI | 42% in 2025"]


def test_danish_perspective_forbids_last_weeks_claims_in_the_prompt():
    import asyncio

    from app import summarize

    seen = {}

    async def fake_llm(prompt, model="", tools=None, thinking=False):
        if "final editor" in prompt:
            return prompt.split("Script:\n", 1)[1]
        seen["prompt"] = prompt
        return _DK_SEGMENT

    with _stub_llm(summarize, fake_llm):
        asyncio.new_event_loop().run_until_complete(summarize.danish_perspective(
            "T", "body", "en",
            recent_claims=["Danmarks Statistik | Danish firms using AI | 42% in 2025"]))

    assert "Danmarks Statistik | Danish firms using AI | 42% in 2025" in seen["prompt"]
    assert "Do not restate" in seen["prompt"]
