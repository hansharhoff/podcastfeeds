# TickTick intake, phase 2: approval queue — design

Date: 2026-07-25. Status: approved by Hans (this session).

## Problem

Phase 1 (`app/ticktick.py`) auto-generates an inbox episode from any URL found
in a new task on the watched TickTick lists and auto-completes the task. Hans
wants the default inverted: nothing generates on its own; items land in an
admin-page queue and he clicks the ones to generate. The lists mostly hold
PDFs/academic papers, book references, and articles — only the last of which
the current pipeline handles.

Decisions taken with Hans:

- **Never** auto-complete TickTick tasks — the integration is read-only.
- Import the existing backlog into the queue (safe now that generation is
  manual).
- Book references (often title-only, no URL) generate an **LLM book brief**.
- PDFs default to **summary-mode** narration with a per-item **full read**
  option at generate time.

## 1. Data model + poller

New table `TickTickItem`:

| column | notes |
|---|---|
| `task_id` | TickTick task id, unique — the dedup key |
| `project` | list name (e.g. "Z Reading") |
| `title`, `notes` | task title; content/desc concatenated |
| `url` | first URL found in title/notes, nullable |
| `kind` | `article` \| `pdf` \| `book` (heuristic, below) |
| `task_created` | TickTick createdTime |
| `first_seen` | when the poller first saw it |
| `status` | `queued` \| `generated` \| `dismissed` (indexed) |
| `episode_id` | FK to the episode once generated, nullable |
| `last_error` | last generate failure, nullable — shown inline in the queue |

Poller (same 5-min cadence, rewritten `poll_ticktick`):

- Fetches open tasks from the watched lists and **upserts** by `task_id`;
  unseen tasks become `queued`. No `submit_url`, no task-complete call.
- The `ticktick_watermark` kv and its seeding logic are removed — `task_id`
  dedup replaces it, and backlog import falls out of the first poll.
- A `queued` item whose task is no longer open in TickTick (Hans completed or
  deleted it there) is auto-dismissed — the queue can be cleaned from either
  side. Auto-dismiss only runs when the poll fetched every watched list
  successfully, so an API blip can't wipe the queue. `generated`/`dismissed`
  items are never re-queued.

Kind heuristic: no URL → `book`; URL path ends `.pdf` or arxiv host → `pdf`;
otherwise `article`. Misclassifications are expected occasionally (book with a
Goodreads link reads as `article`); handled by iteration, not by v1 machinery.

## 2. Admin queue UI + generate routing

New "TickTick queue" section on the admin page listing `queued` items: kind
badge, title, list, age, notes preview, buttons. Clicking **Generate** is the
approval act (same convention as the redo button — see iteration rules).

- `article` → existing `submit_url` inbox pipeline.
- `pdf` → **Generate → summary** (default) or **Generate → full read**.
- `book` → book-brief generator.
- **Dismiss** → status `dismissed`, hidden.

The resulting episode id is stored on the item (`generated`). Failures keep
the item `queued` with the error shown inline in the queue row.

## 3. PDF path

Download the PDF, extract text with `pypdf` (new dependency). Summary mode
reuses the existing `narrate_mode: summary` machinery via the LLM shim; full
read narrates the extracted text through the normal pipeline. The RSS-side
"PDF — not narratable, skip" guard in `app/ingest.py` stays; only
queue-approved PDFs take the new path. v1 is text-only (no figures); quality
iteration happens per-paper with Hans.

## 4. Book brief

LLM-shim-generated 3–5 minute episode: what the book is, core argument,
reception/context, why it might be worth reading — the narration explicitly
frames itself as a brief, not the book. Known limitation: the shim wraps the
local `claude` CLI with no web access, so new/obscure titles may get thin
briefs; acceptable for v1.

## 5. Errors + testing

- No episode is ever created without a click; failed generates stay visible
  in the queue with the error inline.
- Tests: poller upsert/dedup/kind-detection against a faked TickTick API,
  per-kind generate routing, PDF extraction on a fixture file, book-brief
  prompt construction. No live TickTick calls in tests.

## Out of scope (v1)

- Per-item kind override UI (iterate if the heuristic annoys).
- Figure/chapter-art handling for PDFs.
- Web-augmented book briefs.
- YouTube / X-thread intake (none in Hans' lists today).

## Rejected alternatives

- **Episode rows with a `proposed` status** — books have no URL to use as
  `guid`, task notes don't fit the episode model, and every status consumer
  would need to learn to ignore `proposed`.
- **Pre-generating draft episodes, publish on approval** — spends LLM/TTS on
  items that are never approved.
