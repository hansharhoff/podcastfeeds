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
- [x] Unlabelled reader mailbags (ep. 232) read Q & A in one voice, so you couldn't
      tell them apart. `extract.mark_qa` now detects unlabelled Q->A posts (density
      gated to avoid false positives on essays) and tags question paragraphs; they're
      read in a distinct roster voice with an "A reader asks:" cue and their own
      chapter, answers stay in the main voice. Style chosen by Hans. Covered by tests.

## Reliability (from ep. 243 feedback, 2026-07-21)
- [x] Paid Substack posts silently published as previews since ~2026-07-17: the
      substack.com session cookie stopped granting access, and API previews
      carry no paywall CTA so `is_paywalled` missed them (`accessible` wrongly
      True). Fixed: `substack.post_from_api` now compares delivered words
      against the API's full-post `wordcount` (<70% ⇒ truncated). Covered by
      tests. Affected ready episodes: 75, 241, 243, 248, 262, 267 — republish
      only on explicit approval.
- [x] When a subscriber cookie IS configured and a paid post still comes back
      truncated, the episode now says "there was a problem getting the full
      version" (intro + outro + show-notes banner + `fetch_issue` provenance,
      skip-error variant too) instead of the misleading "requires a paid
      subscription" wording.
- [x] noahpinion source pointed at the custom-domain feed
      (www.noahpinion.blog/feed), which bypasses the authenticated Substack
      API path entirely. Switched to noahpinion.substack.com/feed in local
      sources.yaml (feed GUIDs identical, no re-generation).
- [x] Refresh the substack.com session cookie in config/secrets.yaml — done
      2026-07-23. Post-mortem correction to the entry above: only pobrien (and
      the pre-Jul-21 noahpinion custom-domain bypass) were cookie/fetch
      victims. Slow Boring / Silver Bulletin are free signups — eps 75, 241,
      248, 267 were legitimate previews, not fetch failures. Entitlement is
      now explicit (`paid: true` per source); the probe
      (scripts/check_substack_access.py) sentinels only verified-active subs.
- [x] noahpinion paid access — RESOLVED 2026-07-24, and the "another account"
      theory above was wrong: the sub IS on the same account, but it's billed
      via the reader app, which the publication-subdomain API doesn't honor
      ({sub}.substack.com/api/v1/subscription → 404, truncated body) while the
      substack.com host does. Root-cause fix 2026-07-25: `fetch_post`
      consults substack.com/api/v1/posts/by-id/{id} for EVERY paid post
      (routing by `audience`, not the truncation heuristic — which a missing
      `wordcount` could fool into skipping the honoring host) and keeps the
      fuller body; the subdomain result stands when by-id is not fuller
      (per-publication sessions). Covered by tests; ep 243 regenerated in
      full.

## Reliability (from ep. 236 feedback, 2026-07-25)
- [x] Nested lists narrated twice: `segments_from_clean_html` used
      `child.iter("li")`, so an outer `<li>`'s text_content() (which already
      contains its nested list) AND each nested `<li>` were both emitted —
      Zvi's nested-comment style repeated dozens of passages (ep 236). Now
      each `<li>` contributes only its own text and nested lists recurse.
      Covered by tests; backtested against the live post (37 repeated
      chunks → 1, which is genuine prose repetition).

## Reliability (from the ep. 253 link-following review, 2026-07-28)
- [x] A link followed OUT of a social post into Substack took the generic
      `fetch_html` path, so a paid essay pointed at by a tweet came back as its
      free preview. `substack_ref_from_url` judges the target by URL alone
      (the source feed is no help for a followed link) and the cookie-aware
      `fetch_post` runs first when it matches, falling back to the generic
      fetch otherwise. Custom-domain publications (slowboring.com) are
      indistinguishable by URL and deliberately not matched. Covered by tests.

## Reliability (from ep. 380 feedback, 2026-07-31)
- [x] Images inside list items were silently dropped — a REGRESSION from the
      ep-236 fix directly above: `_walk_list` walked each `<li>` for text only
      and never consulted `is_image_block`. Gary Marcus's listicle format (a
      sentence, then a screenshot, seven times over) narrated 0 of its 5
      images and left dangling colons behind ("spotted by :"). Each `<li>` now
      emits its own text, then its image/embed/nested-list blocks in document
      order. Covered by tests; backtested against the live post (8 text-only
      segments → 8 text + 6 image + 2 quote).
- [x] Substack tweet embeds were dropped entirely: `<div class="twitter-embed"
      data-attrs="{json}">` has no text content, so the DOM walk descended into
      it and emitted nothing. The tweet body lives only in that JSON. Now
      emitted as an attributed quote plus any attached photos, with `>`
      greentext markers turned into sentences and bare t.co URLs stripped
      (both read terribly aloud). Subscribe widgets and share buttons use the
      same mechanism and are deliberately NOT in `_NARRATABLE_EMBEDS`.

## Reliability (found while redoing ep. 380, 2026-07-31)
- [x] `redo`/`unskip` on a queue-generated episode did not re-run the
      kind-routed generate path. Those episodes carry no link (the body comes
      from the book brief, parked in `source_text`), so a plain requeue fell
      into `process_episode`'s no-link fallback and re-narrated the stale text
      already on the row. Consequences seen live: eps 330/332 read their old
      dud brief back, ep 337's leaked meta-text was rejected by `looks_meta`
      leaving nothing to narrate (→ skipped), and a dud generated before the
      VERDICT check existed could never pick up a not-a-book proposal. Both
      routes now check for a TickTick item and call `generate_item`. Mode is
      not recorded on the item, so a redone PDF takes the `summary` default.
- [x] The no-link fallback narrated raw HTML: eps 330/332 spoke
      "&lt;p&gt;&lt;strong&gt;Source: Inbox – shared articles&lt;/strong&gt;&lt;/p&gt;"
      aloud, because `source_text`/`description` (both of which can hold
      markup) went to the TTS untouched. Now stripped first.

## Known gaps (decided but not acted on)
- [ ] **Paid post with `wordcount` missing on BOTH hosts** — defer vs. shorter
      CTA-free body. When `wordcount` is absent everywhere, "fuller body wins"
      can prefer a full body whose embedded CTA trips `is_paywalled` (→ defer)
      over a shorter CTA-free preview that would previously have published,
      silently truncated. The 2026-07-25 fix chose defer-over-silent-truncation
      deliberately. Hans to confirm or revisit — no evidence it has bitten yet.
- [ ] **Phase 3: non-article media in the TickTick queue.** A YouTube playlist,
      a Slideshare deck and thimbleweedpark.com sit in the queue classified as
      `article` because they are non-PDF URLs. Generating them will fail
      visibly (the item stays queued with the error inline), which is the
      intended floor — but video/slide extraction was explicitly out of scope
      for v1 and this is the natural next step.

## Observability
- [x] Per-call latency + outcome logging in the LLM shim (`scripts/llm_shim.py`):
      both endpoints share `_run_cli`, which logs duration, model, prompt and
      reply sizes to stderr AND `data/llm_shim.log`. Motivated by an unclosed
      stdin that cost a flat 3s per call for weeks and was found only by
      accident (2026-07-28). The file handler matters because this host has no
      usable session D-Bus, so `journalctl --user` is not a reliable reader.
- [x] In-app paid-access health check (`app/health.py`): every 6h + at boot,
      fetch each `paid: true` source's newest paid post through the real
      fetch path; admin banner + WARNING log on failure. Survives independent
      of any external monitoring session. Covered by tests.
- [ ] Per-source counters (generated / skipped / errored) and last-poll time,
      surfaced in the admin UI, so silent failures become visible.
- [ ] Consider structured logging (JSON) for easier grepping across restarts.

## Ops / packaging
- [ ] Consider digest-pinning the Docker base image (`python:3.12-slim@sha256:…`)
      for fully reproducible builds.
- [ ] `pre-commit` config running ruff on commit.
- [x] `voices.reset_roster()` wired to an admin "↺ reset voices" button
      (`POST /api/reset-roster`, confirm-guarded); clears auto-assigned roster voices,
      leaves fixed config voices. Covered by a test.
- [ ] `summarize.spoken_date` uses the glibc-only `strftime("%-d")`; fine in the
      Linux container, but make it portable before moving the stack to a
      different host (e.g. Synology).

## Docs
- [ ] Expand the README to cover the ElevenLabs per-source upgrade + hard cap,
      the `danish_perspective` closer, the `breaking` source type, and generated
      cover art.
