# TODO / future work

Tracked improvements that are out of scope for a quick cleanup. Roughly ordered
by value-for-effort. Nothing here is a known correctness bug in the shipped
path; these are hardening, testability, and maintainability items.

## Testing & CI (highest value)
- [x] `pytest` + `tests/` covering the pure helpers (`extract`, `summarize`,
      `ingest` helpers, `feedgen`) + a DB-backed `voices.assign_voice` test
      against a throwaway data dir (`tests/conftest.py`). 40 tests.
- [~] GitHub Actions workflow authored (`.github/workflows/ci.yml`: `ruff check`
      + `pytest` + `docker build`; dev tools pinned in `requirements-dev.txt`).
      NOT yet pushed — the `gh` OAuth token lacks the `workflow` scope. To enable:
      `gh auth refresh -s workflow` then `git add .github && git commit && git push`.
- [ ] Integration test for `process_episode` narration-path selection
      (verbatim-short / summary / structured / plain) with a stubbed fetch + TTS.
- [ ] Lock transitive dependencies (`uv pip compile` / `pip-compile` →
      `requirements.lock`). Direct deps are pinned in `requirements.txt`.
- [ ] Optionally add `ruff format` enforcement (currently lint-only; the
      codebase is hand-formatted and not `ruff format`-clean).

## Refactors (reduce risk of the fragile pipeline)
- [ ] Break up `ingest.process_episode` (~320 lines). Extract
      `_fetch_body_and_segments`, `_resolve_voice`, and a single
      `_synthesize_tagged(...)` wrapper (the `async with _tts_lock: synthesize(...)`
      block is duplicated across all four narration branches).
- [ ] Extract `_episode_by_guid(session, slug, guid)` — the same
      `select(Episode).where(source_slug==, guid==).first()` query appears 5× in
      `ingest.py`.
- [ ] Replace the episode-status magic strings
      (`pending`/`processing`/`ready`/`error`/`skipped`) with a `StrEnum` or
      module constants in `db.py`; a typo currently fails silently.
- [ ] Split `poll_rss_source` into a small `Watermark` helper + a
      `should_generate(entry)` predicate so the "never backfill" logic is testable
      in isolation.
- [ ] Promote load-bearing pipeline thresholds to named constants (preview floor
      600, fallback-body 200/40, summary-vs-verbatim 400, structured-segment 200,
      image cap 8, paywall floor 600, min image px 200, Danish-ratio 0.08, the
      <90s "needs decision" cutoff). They currently live as inline literals.

## HTTP & resources
- [ ] Introduce one lifespan-managed `httpx.AsyncClient` (connection pooling +
      keep-alive) instead of constructing a throwaway client per call in
      `extract`, `substack`, `summarize`, `elevenlabs`, `ticktick`. Consolidate
      the scattered timeout literals (15/30/45/180/300/600) into named constants
      and add a small shared retry policy.

## ElevenLabs spend cap
- [ ] The monthly cap is enforced per-episode; two episodes rendering
      concurrently can each pass the check and slightly overshoot the *local*
      budget. Real spend is still bounded by ElevenLabs' own reported quota (which
      the check also honors), so this is a soft local overshoot, not a runaway.
      Serialize the budget check/spend (reuse `_tts_lock`, or an EL-specific lock)
      to make the local cap exact.

## Reliability / narration quality (from ep. 232 feedback, 2026-07-19)
- [x] edge-tts throttling (`NoAudioReceived`): `tts._synth_chunk` already retried
      3× (~9s) but sustained throttling needs more — deepened to 6 attempts with
      backoff (max 20s) and a warning log so throttling is visible.
- [x] Vision describer emitted markdown TABLES read aloud verbatim (ep. 232 block
      5). Fixed two ways: (1) `summarize.linearize_markdown_tables` rewrites any
      pipe table into spoken "Header: value; …" prose, wired into `scrub_light` so
      every spoken block is protected and text-screenshot tables are labelled
      "There is a table here."; (2) `VISION_PROMPT` now forbids markdown/pipes and
      routes data tables to kind "image" with a prose takeaway. Covered by tests.

## Observability
- [ ] Per-source counters (generated / skipped / errored) and last-poll time,
      surfaced in the admin UI, so silent failures become visible.
- [ ] Consider structured logging (JSON) for easier grepping across restarts.

## Ops / packaging
- [ ] Consider digest-pinning the Docker base image (`python:3.12-slim@sha256:…`)
      for fully reproducible builds.
- [ ] `pre-commit` config running ruff on commit.
- [ ] Wire `voices.reset_roster()` to an admin endpoint (currently unreachable) or
      remove it.
- [ ] `summarize.spoken_date` uses the glibc-only `strftime("%-d")`; fine in the
      Linux container, but make it portable before moving the stack to a
      different host (e.g. Synology).

## Docs
- [ ] Expand the README to cover the ElevenLabs per-source upgrade + hard cap,
      the `danish_perspective` closer, the `breaking` source type, and generated
      cover art.
