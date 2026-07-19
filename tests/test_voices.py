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
