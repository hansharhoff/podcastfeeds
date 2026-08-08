"""TTS synthesis behaviour."""


def test_synth_chunk_retry_budget_outlasts_a_throttling_burst():
    """edge-tts answers NoAudioReceived while Microsoft is throttling. A long
    episode is hundreds of back-to-back requests, and ep. 299 (13k words, run
    straight after four other interviews) burned the whole six-attempt budget
    inside 45s — twice — losing the episode. The budget must span minutes."""
    import inspect

    from app import tts

    attempts = inspect.signature(tts._synth_chunk).parameters["attempts"].default
    backoffs = [min(5 * a, 60) for a in range(1, attempts)]
    assert attempts >= 9
    assert sum(backoffs) >= 240, f"only {sum(backoffs)}s across {attempts} attempts"


def test_synth_chunk_backoff_is_capped_so_a_dead_service_still_fails():
    """Retrying must not turn an outage into an unbounded hang."""
    import inspect

    from app import tts

    attempts = inspect.signature(tts._synth_chunk).parameters["attempts"].default
    assert sum(min(5 * a, 60) for a in range(1, attempts)) <= 900


def test_has_speech_accepts_ordinary_prose():
    from app.tts import has_speech
    assert has_speech("Hello there.")
    assert has_speech("2026 was a year.")
    assert has_speech("Bare ét ord")          # non-ASCII letters count


def test_has_speech_rejects_chunks_with_nothing_to_pronounce():
    """edge-tts answers NoAudioReceived for these and no amount of retrying
    helps — a stray '~~' cost ep. 299 three full re-renders."""
    from app.tts import has_speech
    for junk in ("~~", "", "   ", "---", "***", "  •  ", "…", "|  |  |"):
        assert not has_speech(junk), junk


def test_scrub_light_strips_strikethrough_markers():
    from app.summarize import scrub_light
    assert scrub_light("~~struck out~~ but readable") == "struck out but readable"
    assert scrub_light("~~") == ""
