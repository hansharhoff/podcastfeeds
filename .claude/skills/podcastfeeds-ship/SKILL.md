---
name: podcastfeeds-ship
description: Ship a change to the running podcastfeeds instance - lint, test, commit, push, watch CI (exit-code-safe), rebuild the container, and verify health. Use whenever code/config changes are ready to deploy, or when the user says "ship it", "deploy", or "push and rebuild".
---

# podcastfeeds-ship

The full deploy dance for this repo, in order. Don't skip steps; each one has
caught a real failure at least once.

## 1. Lint + test (gate everything on this)

```bash
.venv/bin/ruff check app/ scripts/ tests/ && .venv/bin/pytest -q
```

Both must pass before committing. Bare `pytest` works (pythonpath is set in
pyproject.toml), but use the venv binaries explicitly.

## 2. Commit + push

Stage only the files you changed (never `git add -A` — `config/sources.yaml`,
`config/secrets.yaml` and `data/` are git-ignored but be deliberate anyway;
this repo is PUBLIC). Commit with a body explaining the why, then
`git push origin main`.

## 3. Watch CI — exit-code-safe

**Never pipe `gh run watch` into anything** — the pipe swallows its exit code
and once produced a false "CI PASSED" (2026-07-21). Use exactly this shape,
in the background while you continue:

```bash
sleep 10; RUN_ID=$(gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
if gh run watch "$RUN_ID" --exit-status >/dev/null 2>&1; then echo "CI PASSED"; else echo "CI FAILED"; fi
```

On failure: `gh run view "$RUN_ID" --log-failed | tail -40`.

## 4. Rebuild + restart the container

Code is baked into the image (only `data/` and `config/` are volume-mounted),
so every app-code change needs a rebuild:

```bash
docker compose up -d --build
```

Config-only changes (sources.yaml, secrets.yaml) are hot-visible but
sources.yaml is only *read* at scheduler startup — restart for source changes,
no restart for cookie changes.

## 5. Verify health

```bash
until docker ps --filter name=podcastfeeds --format '{{.Status}}' | grep -q healthy; do sleep 2; done
docker logs podcastfeeds --since 2m 2>&1 | grep -iE "error|traceback|warning" | head
```

The boot paid-access check runs at startup; the admin page should show
"Paid access ✓" (token is in `data/token.txt`; the app serves on :8080).

## 6. Verifying regenerated episodes (when the change affects the pipeline)

- Requeue via API: `POST http://localhost:8080/{token}/api/redo/{id}` (303 = queued).
- Judge the result by `script` length and `provenance` keys (`preview`,
  `fetch_issue`) — **`source_text` is NEVER rewritten on redo**, don't use it.
- Episodes render asynchronously (LLM + TTS, minutes) — poll the DB in a
  background until-loop, don't sleep-wait inline.

## Iteration rules (non-negotiable)

Never republish/redo existing episodes without explicit approval from Hans —
deploying code is fine, regenerating published audio is not (UI redo button
counts as approval).
