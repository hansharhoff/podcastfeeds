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
