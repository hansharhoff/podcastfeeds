"""DB-backed: runs against the throwaway data dir set up in conftest.py."""
from app.voices import CURATED, assign_voice, get_roster, reset_roster


def test_curated_key_returns_researched_voice_and_persists():
    voice = assign_voice("noahpinion", "en")
    assert voice == CURATED["noahpinion"]
    # Persisted: a second call returns the same voice from the roster.
    assert assign_voice("noahpinion", "en") == voice
    assert get_roster().get("noahpinion") == voice


def test_uncurated_key_is_stable_across_calls():
    first = assign_voice("some_unknown_blog", "en")
    assert isinstance(first, str) and first
    assert assign_voice("some_unknown_blog", "en") == first


def test_distinct_uncurated_keys_get_voices():
    a = assign_voice("blog_alpha", "en")
    b = assign_voice("blog_beta", "en")
    assert isinstance(a, str) and isinstance(b, str)


def test_reset_roster_clears_all_assignments():
    # Defined last: reset wipes the shared session roster.
    assign_voice("temp_blog_reset", "en")
    assert get_roster()  # non-empty before reset
    cleared = reset_roster()
    assert cleared >= 1
    assert get_roster() == {}


# ── gender/accent matching for named speakers (ep. 447 feedback) ──────────
# Jerusalem Demsas drew a male New Zealand voice and Matt Yglesias a female
# Indian one, because named guests fell through to the contrast pool.

def test_match_voice_respects_gender():
    from app.voices import VOICE_CATALOG, match_voice
    assert VOICE_CATALOG[match_voice("f", "US", "en")][0] == "f"
    assert VOICE_CATALOG[match_voice("m", "US", "en")][0] == "m"


def test_match_voice_prefers_the_speakers_own_accent():
    from app.voices import VOICE_CATALOG, match_voice
    for accent in ("US", "GB", "AU", "IN", "CA", "IE"):
        for gender in ("m", "f"):
            got = match_voice(gender, accent, "en")
            assert VOICE_CATALOG[got] == (gender, accent), (gender, accent, got)


def test_match_voice_keeps_gender_when_the_accent_is_unavailable():
    from app.voices import VOICE_CATALOG, match_voice
    # no Ukrainian/German English voice exists; gender must survive the fallback
    got = match_voice("m", "DK", "en")
    assert got is not None
    assert VOICE_CATALOG[got][0] == "m"
    assert got.startswith("en-")


def test_match_voice_returns_none_for_unknown_gender():
    from app.voices import match_voice
    assert match_voice("unknown", "US", "en") is None
    assert match_voice("", "US", "en") is None


def test_match_voice_avoids_voices_already_in_the_episode():
    from app.voices import match_voice
    first = match_voice("f", "US", "en")
    second = match_voice("f", "US", "en", frozenset({first}))
    assert second != first


def test_match_voice_never_hands_out_the_child_voice():
    from app.voices import ACCENTS, match_voice
    picks = {match_voice(g, a, "en") for g in ("m", "f") for a in ACCENTS}
    assert "en-US-AnaNeural" not in picks


def test_every_pool_and_curated_voice_is_in_the_catalog():
    """The catalog is what the matcher reasons over; a voice missing from it
    would be invisible to gender matching."""
    from app.voices import CURATED, VOICE_CATALOG, VOICE_POOLS
    for voice in set(CURATED.values()) | {v for p in VOICE_POOLS.values() for v in p}:
        assert voice in VOICE_CATALOG, voice


def _run(coro):
    import asyncio
    return asyncio.get_event_loop().run_until_complete(coro)


def test_speaker_profile_caches_and_only_asks_once(monkeypatch):
    from app import summarize, voices

    calls = []

    async def fake_llm(prompt, *a, **kw):
        calls.append(prompt)
        return '{"person": true, "gender": "f", "accent": "US"}'

    monkeypatch.setattr(summarize, "llm", fake_llm)
    assert _run(voices.speaker_profile("Jerusalem Demsas")) == ("f", "US")
    assert _run(voices.speaker_profile("Jerusalem Demsas")) == ("f", "US")
    assert len(calls) == 1


def test_speaker_profile_treats_non_people_as_unknown(monkeypatch):
    from app import summarize, voices

    async def fake_llm(prompt, *a, **kw):
        return '{"person": false, "gender": "m", "accent": "US"}'

    monkeypatch.setattr(summarize, "llm", fake_llm)
    assert _run(voices.speaker_profile("Person 1")) == ("unknown", "")


def test_speaker_profile_survives_a_dead_llm(monkeypatch):
    from app import summarize, voices

    async def boom(prompt, *a, **kw):
        raise RuntimeError("no LLM backend available")

    monkeypatch.setattr(summarize, "llm", boom)
    assert _run(voices.speaker_profile("Someone Unreachable")) == ("unknown", "")


def test_warm_speaker_voices_assigns_a_matching_voice(monkeypatch):
    from app import summarize, voices

    async def fake_llm(prompt, *a, **kw):
        if "Jerusalem Demsas" in prompt:
            return '{"person": true, "gender": "f", "accent": "US"}'
        return '{"person": true, "gender": "m", "accent": "US"}'

    monkeypatch.setattr(summarize, "llm", fake_llm)
    _run(voices.warm_speaker_voices(
        {"speaker:jd-test": "Jerusalem Demsas", "speaker:my-test": "Matthew Yglesias"},
        "en",
    ))
    roster = voices.get_roster()
    assert voices.VOICE_CATALOG[roster["speaker:jd-test"]] == ("f", "US")
    assert voices.VOICE_CATALOG[roster["speaker:my-test"]] == ("m", "US")


def test_warm_speaker_voices_leaves_an_established_speaker_alone(monkeypatch):
    from app import db, summarize, voices

    with db.session() as s:
        db.kv_set(s, "voice:speaker:established", "en-US-JennyNeural")

    async def fake_llm(prompt, *a, **kw):
        return '{"person": true, "gender": "m", "accent": "GB"}'

    monkeypatch.setattr(summarize, "llm", fake_llm)
    _run(voices.warm_speaker_voices({"speaker:established": "Someone"}, "en"))
    assert voices.get_roster()["speaker:established"] == "en-US-JennyNeural"


# ── the image describer is the app's own voice, not the publication's ─────

def test_describer_voice_is_the_same_for_every_source():
    from app.voices import describer_voice
    assert describer_voice("en") == describer_voice("en")
    assert describer_voice("da") == describer_voice("da")
    # unknown languages still get a voice rather than blowing up
    assert describer_voice("de") == describer_voice("en")


def test_describer_voice_is_language_appropriate():
    from app.voices import VOICE_CATALOG, describer_voice
    assert describer_voice("en").startswith("en-")
    assert describer_voice("da").startswith("da-")
    assert describer_voice("en") in VOICE_CATALOG


def test_english_describer_is_reserved_from_the_contrast_pool():
    """Otherwise a source's quote or question voice could be handed the
    describer's voice and the two would collide inside one episode."""
    from app.voices import VOICE_POOLS, describer_voice
    assert describer_voice("en") not in VOICE_POOLS["en"]


def test_describer_differs_from_the_view_from_denmark_closer():
    """Both are app-owned segments; hearing them in one voice would merge two
    different kinds of interjection."""
    from app.voices import CURATED, describer_voice
    assert describer_voice("en") != CURATED.get("danish-perspective:en", "en-US-AriaNeural")
