# Audio courses and audiobooks: design

Date: 2026-09-29. Status: approved by Hans in brainstorming, awaiting spec review.

## TL;DR

- From any Claude Code session, Hans asks for an audio course on a topic. A new
  `audio-course` skill asks a few teach-tech style questions, researches, and
  shows an outline. On approval it writes one spoken script per part and POSTs
  them to a new `/{token}/api/course` endpoint.
- Pointed at a public book (e.g. Shape Up), the skill offers an **audiobook**
  instead: the author's text, one episode per chapter, nothing rewritten.
- The server narrates through the existing pipeline into a new `learning` feed
  that is **kept out of `all.xml`**. After 60 days the MP3s are deleted but the
  scripts and show notes are kept (status `archived`, re-narratable via redo).
- Not decided here: the skill's exact prompt wording. That is settled during
  implementation by listening to real output.

## Problem

teach-tech is interactive: it stops so the user can run commands and paste
code. Hans listens to podcasts while commuting, hands-free. He wants to request
a tutorial on any topic and have it land in his podcast app as a **standalone
passive course**: concepts, mental models and vocabulary taught by listening
alone, with code parked in show notes for later.

## Decisions taken with Hans

- **Role:** standalone passive course. No exercises, no "pause and type this".
- **Size:** the LLM decides. A narrow topic gets one episode, a broad one a
  series. Caps: 8 parts for a course, 60 for an audiobook.
- **Intake:** only from a Claude Code session (no UI box, phone shortcut or
  TickTick list).
- **Split of work (approach A):** the session writes, the server narrates. The
  interactive and research-heavy work happens where it is cheap to steer and
  has full personal context. The LLM shim is not involved in writing scripts.
- **Approval gate:** nothing is submitted until Hans approves the outline (or
  chapter list) in the session. This is consistent with the standing rule that
  nothing publishes without explicit approval.
- **Separate feed:** course episodes never appear in `all.xml`.
- **Retention:** keep the text, drop the audio.
- **Books:** a public book gets an audiobook offer, one chapter per episode.

## 1. Server

### 1.1 `learning` source and feed separation

- `SourceDef` gains `in_all_feed: bool = True`.
- `load_config` auto-adds a `learning` source (type `learning`, name
  "Learning", `in_all_feed=False`) when `sources.yaml` does not define one,
  mirroring how `inbox` is auto-added. A `sources.yaml` entry can override it,
  e.g. to pin a fixed teacher voice.
- `feedgen.build_feed(source=None)` excludes episodes whose source has
  `in_all_feed=False`. `learning.xml` works like any per-source feed.
- The scheduler does not poll `learning`; it has no upstream.

### 1.2 `POST /{token}/api/course`

Request body (JSON):

```json
{
  "course_id": "rust-ownership-2026-09-29",
  "title": "Rust ownership, for a Python developer",
  "kind": "course",
  "language": "en",
  "episodes": [
    {"part": 1, "title": "Why ownership exists",
     "script": "...", "show_notes_md": "..."},
    {"part": 2, "title": "Borrowing",
     "url": "https://example.com/book/chapter-2"},
    {"part": 3, "title": "Lifetimes",
     "script": "...", "verbatim": true}
  ]
}
```

- `kind` is `course` or `audiobook`; it only selects the cap and intro wording.
- Each episode has exactly one of `script` (text to narrate) or `url` (a page
  to fetch and narrate through the normal article pipeline). `verbatim: true`
  on a script skips the LLM scrub. `show_notes_md` is optional.
- Validation: `course_id` matches `[a-z0-9-]{1,80}`; parts are 1..N with no
  gaps or duplicates; N is at most 8 (course) or 60 (audiobook); each script is
  at most 60000 chars (the same bound already used for `source_text`); `url`
  must be http(s). Anything invalid is a 400 with the reason; nothing is
  created.
- Each part becomes an `Episode` with `source_slug="learning"`,
  `guid="{course_id}:{part}"`, `link=url or ""`, `source_text=script or ""`.
  The unique `(source_slug, guid)` index makes a repeated POST a no-op for
  parts that already exist.
- Course metadata the renderer needs (course title, part number, part count,
  kind, verbatim flag, show notes) goes into the episode's `provenance` JSON
  under a `course` key. No new columns.
- Response: `{"episode_ids": [...]}` in part order.

### 1.3 Rendering

- Parts render **sequentially in part order** in one background task, not one
  task per part. A failed part is marked `error` (existing retry button
  applies) and rendering continues with the next part.
- `published_at` is set to the request time plus `part` seconds, so podcast
  apps sort the parts in order regardless of which finishes first.
- `process_episode` detects `provenance.course` and:
  - uses a course intro: "Rust ownership, part 2 of 4: Borrowing." for
    courses, "Shape Up, chapter 3: Set Boundaries." for audiobooks (Danish
    equivalents when `language` is `da`);
  - uses a course outro that points at the show notes only when
    `show_notes_md` is present;
  - builds show notes from `show_notes_md` (rendered to HTML) instead of the
    article HTML, for script parts;
  - skips the LLM scrub when `verbatim` is set.
- URL parts otherwise go through the unchanged article path (extraction,
  image announcements and chapter art, quote voice).

### 1.4 Retention

- The nightly cleanup treats `learning` differently: for episodes older than
  `retention_days` it deletes the MP3, clears `audio_file`, `audio_bytes` and
  `audio_seconds`, and sets `status="archived"`. The row, `source_text`,
  `script`, `description` and `provenance` are kept.
- `archived` episodes are not in any feed (feeds only list `ready`) but are
  shown on the admin page. The existing redo button re-narrates them from
  `source_text` (URL parts refetch).
- All other sources keep today's full delete.
- `cleanup_orphaned_media` must not treat an archived row's (now empty)
  `audio_file` specially; it already only cares about files on disk.

## 2. The `audio-course` skill

Lives in the hansharhoff-skills marketplace next to teach-tech. Separate from
teach-tech because teach-tech's loop is built around stopping for user action.

### 2.1 Flow

1. **Classify the request.** A topic goes down the course path. A URL or title
   of a book with a free official online edition gets a choice: audiobook
   (verbatim, chapter per episode) or course about it.
2. **Clarify (course path).** Two or three multiple-choice questions, as in
   teach-tech: why Hans wants it, which aspects, what he already knows. It
   looks at his usual stack to choose analogies.
3. **Research.** Web search for current best practice and correct facts.
4. **Outline and stop.**
   - Course: part titles, one line per part on what Hans will understand
     afterwards, language, estimated minutes per part (15 to 20).
   - Audiobook: the chapter list, with front matter and appendices marked in
     or out, and whether each chapter will go as a URL or as verbatim text.
   - The skill waits for an explicit yes. Edits loop back to the outline.
5. **Write (course path).** One script per part, written for the ear:
   flowing spoken prose, no markdown, lists or tables; code described in
   words, never read character by character; a short recap of the previous
   part at the start and a summary at the end; analogies to Hans's stack.
   Code snippets and source links go in `show_notes_md`.
6. **Audiobook path.** Prefer a per-chapter URL when the book has a page per
   chapter. If the book is one page, split it by chapter heading and send each
   chapter as `script` with `verbatim: true`.
7. **Submit and confirm.** POST to `/api/course`, then poll the first part's
   status until `ready` (or `error`) and report. It does not wait for the
   remaining parts.

### 2.2 Config and failure handling

- Reads `PODCASTFEEDS_URL` and `PODCASTFEEDS_TOKEN` from the environment. The
  token never appears in the skill files (the marketplace repo may be public).
- If the POST fails, the full request JSON is saved to a local file and the
  skill prints a one-line `curl` that resubmits it. Finished scripts are never
  lost to a down container.

## 3. Privacy note

Audiobooks narrate someone else's book into a feed. That is acceptable only
while the feed is private. The deployment is tailnet-only today; if Funnel is
ever enabled, the `learning` feed would be publicly fetchable by anyone with
the token URL, which is worth remembering before turning Funnel on.

## 4. Errors

| Situation | Behaviour |
|---|---|
| Invalid request (cap, gaps, bad URL, oversize script) | 400 with reason, nothing created |
| Repeated POST of the same course | existing parts untouched, missing parts created |
| A chapter URL fails to fetch | that part is `error` with retry; later parts still render |
| Container down at submit | skill saves JSON locally and prints resubmit command |
| TTS or pipeline failure on one part | existing `error` handling; rendering moves on |

## 5. Testing

Pytest on the host (`.venv/bin/python -m pytest`), written test-first:

- `/api/course`: creates rows in part order with increasing `published_at`;
  rejects over-cap, gapped and duplicate-part requests with no rows created;
  a repeated POST is idempotent; a partially existing course fills only the
  missing parts; requires exactly one of `script` or `url`.
- Feeds: `all.xml` excludes `learning` episodes; `learning.xml` includes them;
  other sources are unaffected.
- Retention: `learning` episodes past the cutoff become `archived` with the MP3
  deleted and text kept, and drop out of `learning.xml`; other sources are still
  fully deleted.
- Rendering: the course and audiobook intros in `da` and `en`; verbatim
  scripts bypass the scrub; show notes come from `show_notes_md`.

Manual acceptance, by listening before calling it done:

- One short course end to end from a Claude Code session.
- Shape Up chapters 1 and 2 as an audiobook.

## Out of scope

- Other intake paths (UI box, phone shortcut, TickTick).
- On-demand continuation ("go deeper on part 3").
- Recap episodes after interactive teach-tech sessions.
- Any change to the LLM shim.
