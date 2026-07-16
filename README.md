# podcastfeeds

Self-hosted podcast feeds from things you read: RSS/Substack sources, narrated
news digests (DR.dk etc.), and arbitrary articles shared from your phone — all
turned into MP3s with free neural TTS (edge-tts) and served as private RSS
feeds over Tailscale.

## Architecture

```
config/sources.yaml ──► scheduler (APScheduler)
                          │  rss:    each new entry → one episode
                          │  digest: entries since last run → one narrated episode
                          │  inbox:  URLs you share from the phone
                          ▼
              trafilatura extract → edge-tts (chunked) → ffmpeg concat → mp3 + ID3
                          ▼
        SQLite (data/podcastfeeds.db) + MP3s (data/media/)
                          ▼
     FastAPI: /{token}/feeds/{slug}.xml · /{token}/media/… · /{token}/ (UI)
                          ▼
   docker compose (port 8080) → Windows `tailscale serve` → tailnet-only HTTPS
```

Everything sits behind a secret token in the URL path (`data/token.txt`,
override with `TOKEN` env). Substack posts that already ship an audio version
are passed through untouched (the feed points at the original enclosure).

## Running

```bash
docker compose up -d --build     # from this directory (WSL2)
```

`.env` holds `BASE_URL` (the public hostname written into feed enclosure URLs)
and optionally `TOKEN`. ElevenLabs (optional, per-source upgrade) is configured
with `ELEVENLABS_API_KEY` and `ELEVENLABS_CHAR_BUDGET` (a hard monthly cap).

Expose on the tailnet from **Windows** (PowerShell or via WSL):

```bash
"/mnt/c/Program Files/Tailscale/tailscale.exe" serve --bg 8080
```

That serves `https://YOUR-MACHINE.YOUR-TAILNET.ts.net` → `localhost:8080`,
reachable only from your tailnet. For cloud podcast apps (Pocket Casts), the
feed must be publicly reachable — enable Funnel:

```bash
"/mnt/c/Program Files/Tailscale/tailscale.exe" funnel --bg 8080   # public HTTPS
# turn public access off again (tailnet-only serve keeps working):
"/mnt/c/Program Files/Tailscale/tailscale.exe" funnel --https=443 off
```

With Funnel on, the secret URL token is the only access control — rotate it by
setting `TOKEN` in `.env`, `docker compose up -d`, and resubscribing.

Management UI / feed URLs: `https://YOUR-MACHINE.YOUR-TAILNET.ts.net/<token>/`

## Phone setup (AntennaPod)

1. Install AntennaPod (F-Droid / Play Store); make sure Tailscale is active.
2. Add podcast by RSS URL: paste a feed URL from the UI page — either
   `…/feeds/all.xml` (everything) or per-source `…/feeds/dr-nyheder.xml` etc.
3. Settings → Automation: enable auto-download + "add new episodes to queue"
   for the queue behaviour you want (e.g. newest first).

Share an article from the phone: open the UI page and paste a URL, or use the
[HTTP Shortcuts](https://http-shortcuts.rmy.ch/) app with a share-menu shortcut:
`POST https://YOUR-MACHINE.YOUR-TAILNET.ts.net/<token>/api/submit` with a form
field `url={{clipboard/share text}}` — then it shows up in the Inbox feed a
minute later.

## Configuring sources

Copy `config/sources.yaml.example` to `config/sources.yaml` (git-ignored, so
your subscription list stays local) and edit it (see comments inside), then
`docker compose restart`. If no `sources.yaml` exists the app falls back to the
example. Source types: `rss`, `digest`, `breaking`, `inbox`.
Per-source: `voice` (any `edge-tts --list-voices` name), `language` (`da`/`en`/
`auto`), `poll_minutes`, `schedule` (cron, digests), `urls` (digests can
aggregate several feeds), `title_filter` (regex, e.g. release posts only),
`narrate_mode: summary` (LLM overview instead of full read — for release
notes/changelogs), `prefer_existing_audio`, `digest_max_items`.

## Episode features

- **Voice roster** — every source/blogger without a fixed `voice:` gets one
  assigned from a pool on first episode and keeps it forever (stored in the DB,
  visible on the admin page). Fixed voices: general news `en-GB-RyanNeural`,
  Home Assistant `en-GB-ThomasNeural`.
- **Quotes** — blockquotes in articles are read by a second, per-source roster
  voice.
- **Images** — announced in the narration, embedded in HTML show notes, and
  attached as **ID3 chapter art** (Pocket Casts shows the figure while that
  part plays; the chapter list jumps between figures).
- **Episode artwork** — the post's most prominent picture: the author's lead
  image (og:image / Substack cover) if present, else the largest body image by
  pixel area (re-fetched at cover resolution). Embedded in the MP3 and per-item
  `itunes:image`; the generated gradient cover is the last-resort fallback.
- **Intros/outros** — every article episode opens "Title. From Source, date."
  and signs off pointing at the show notes; episodes carry embedded cover art
  and `itunes:episode` numbers.
- **Digests** — LLM-rephrased (overview first, then perspective and
  significance) via the **LLM shim**: `scripts/llm_shim.py` wraps the local
  `claude` CLI as HTTP on port 8765, installed as a systemd user service
  (`systemctl --user status podcastfeeds-llm-shim`). The container reaches it
  as `http://host.docker.internal:8765`. If it's down, digests fall back to
  headline+lead extraction and article summaries to lead paragraphs.

## Intake & special sources

- **AI Release Watch** (`llm_filter`) — rss sources can carry an `llm_filter`
  criteria paragraph; every new entry is LLM-classified and only matches become
  episodes (rejects are stored as status `skipped` so they aren't re-judged).
  Used to surface major AI-lab releases within ~20 minutes, alongside the daily
  digest.
- **TickTick** — add a URL to a watched list and it becomes an inbox episode
  (task auto-completed). Watched lists are set in `data/ticktick.json`
  (`"lists": ["Z Reading", "Z Listening"]`). A watermark means only tasks
  created *after* setup are processed — the existing backlog is never touched.
  One-time setup: register an app at https://developer.ticktick.com/manage with
  redirect URI `http://127.0.0.1:8993/callback`, then
  `.venv/bin/python scripts/ticktick_auth.py CLIENT_ID CLIENT_SECRET`.
  Note: WSL2 doesn't always forward Windows `localhost:8993` to the callback
  server, so the browser redirect may fail to load — just copy the `code=...`
  value from the redirected URL and exchange it manually. Poller runs every 5 min.
- **Paid posts (Substack)** — copy `config/secrets.yaml.example` to
  `config/secrets.yaml` and add your `substack.sid` cookie per publication
  domain; the fetcher then reads paid posts as you. Cookies eventually expire
  (~6-12 months): the symptom is paywalled-preview episodes; the fix is
  re-pasting a fresh cookie.

## Reboot resilience

- Docker (native, in WSL) and the container (`restart: unless-stopped`) start
  with the distro; the LLM shim is a systemd user service and lingering is
  enabled for your login user.
- Windows starts the WSL distro at logon via a hidden launcher in the user
  Startup folder (`…/Start Menu/Programs/Startup/podcastfeeds-start-wsl.vbs`,
  no admin needed) — booting the distro starts systemd → docker → the
  container. (`schtasks /SC ONLOGON` is the elevated alternative if preferred.)
- Tailscale serve/funnel config persists on its own.

## Operational notes

- Episodes older than `retention_days` (default 60) are deleted nightly.
- WSL2/Docker must be running for the feed to be reachable — enable Docker
  Desktop autostart, or move the stack to the Synology (same compose file;
  change `BASE_URL`, run `tailscale serve` there).
- Logs: `docker compose logs -f`. Manual poll: button in the UI or
  `POST /{token}/api/poll`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.main     # serves on :8080, data/ + config/ in repo
```
