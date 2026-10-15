# Getting started

## TL;DR

- You need Docker, a free Tailscale account and a podcast app. Claude Code is
  optional and only improves digests and summaries.
- Clone, copy the example config, run `docker compose up -d --build`, open
  `http://localhost:8080/<token>/` (the token is in `data/token.txt`).
- Make it reachable from your phone with `tailscale serve`, or `tailscale funnel`
  if your podcast app fetches feeds from its own servers (Pocket Casts, Overcast).
- On a Mac, keep it from sleeping, or the feed goes offline with it.

This guide is for running your own copy. Each install has its own sources,
episodes and secret token; nothing is shared with anyone else's.

## What you need

| | macOS | Linux | Windows |
|---|---|---|---|
| Docker | [Docker Desktop](https://www.docker.com/products/docker-desktop/) or [OrbStack](https://orbstack.dev/) | Docker Engine with the compose plugin | Docker inside WSL2 |
| Tailscale | [Tailscale app](https://tailscale.com/download/mac) | `tailscale` package | Tailscale for Windows |
| Optional: LLM features | [Claude Code](https://claude.com/claude-code), logged in, plus Python 3.12 | same | same, inside WSL2 |

Without Claude Code everything still works: articles are read in full, digests
fall back to headlines plus lead paragraphs, and images are announced without a
description.

## 1. Get the code and configure it

```bash
git clone https://github.com/hansharhoff/podcastfeeds.git
cd podcastfeeds
mkdir -p data
cp config/sources.yaml.example config/sources.yaml
```

Edit `config/sources.yaml` to list the feeds you want. The example is the
author's own setup (mostly Danish news and English Substacks), so replace its
sources with yours. The comments at the top of the file explain every field.
Leave `voice` empty to have a voice picked and remembered per source.

Schedules (`schedule:` on digests, the nightly cleanup) run on Copenhagen time.

**Linux and WSL2 only:** the container writes to `./data` as a non-root user
that must own it. Tell the build your IDs once:

```bash
printf 'APP_UID=%s\nAPP_GID=%s\n' "$(id -u)" "$(id -g)" >> .env
```

On macOS skip this; Docker Desktop and OrbStack handle file ownership themselves.

## 2. Start it

```bash
docker compose up -d --build
curl -s localhost:8080/healthz          # {"ok":true}
cat data/token.txt                      # your secret token
```

Open `http://localhost:8080/<token>/` in a browser. That is the admin page: it
lists episodes, has the feed URLs, and a box to paste any article URL. The first
poll runs within a few minutes; the **Poll all sources now** button starts one immediately.

The token in the URL is the only thing protecting the admin page and the feeds.
Don't post your feed URL anywhere public. To change the token, put `TOKEN=...`
in `.env`, run `docker compose up -d`, and resubscribe in your podcast app.

## 3. Reach it from your phone

Install Tailscale on the computer and on your phone, signed in to the same
account. On macOS, if `tailscale` isn't on your PATH, use the CLI inside the app:

```bash
alias tailscale=/Applications/Tailscale.app/Contents/MacOS/Tailscale
```

Then pick one:

- **Private (tailnet only).** Works with podcast apps that download feeds on the
  phone itself, such as AntennaPod, while Tailscale is on.

  ```bash
  tailscale serve --bg 8080
  ```

- **Public (Funnel).** Needed for apps that fetch feeds from their own servers,
  such as Pocket Casts and Overcast. The token stays the only protection.

  ```bash
  tailscale funnel --bg 8080
  ```

  The first time, Tailscale may print a link to enable Funnel for your tailnet.

Either command prints your address, `https://<machine>.<tailnet>.ts.net`. Put it
in `.env` so feeds link their audio files there rather than to `localhost`:

```bash
echo 'BASE_URL=https://<machine>.<tailnet>.ts.net' >> .env
docker compose up -d
```

## 4. Subscribe

In your podcast app, add a podcast by URL:

- `https://<machine>.<tailnet>.ts.net/<token>/feeds/all.xml` for everything, or
- `.../feeds/<source-slug>.xml` for one source (the admin page lists them all).

To send an article from your phone, paste its URL on the admin page. On Android,
the [HTTP Shortcuts](https://http-shortcuts.rmy.ch/) app can add a share-menu
entry that POSTs `url=<shared text>` to `.../<token>/api/submit`. On iOS, a
Shortcuts action "Get contents of URL" with method POST does the same.

## 5. Optional: LLM features through Claude Code

The container has no Claude login of its own. A small shim on the host wraps
your logged-in `claude` CLI and the container calls it on port 8765.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
claude -p "say ok"                       # check the CLI is logged in
.venv/bin/python scripts/llm_shim.py     # leave running; Ctrl-C to stop
```

It only answers requests from the machine itself and from Docker, never from
your network or tailnet. To keep it running across reboots:

**macOS (launchd).** Save as `~/Library/LaunchAgents/com.podcastfeeds.llm-shim.plist`,
replacing `/Users/YOU/podcastfeeds` with your clone's path:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.podcastfeeds.llm-shim</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/podcastfeeds/.venv/bin/python</string>
    <string>/Users/YOU/podcastfeeds/scripts/llm_shim.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.podcastfeeds.llm-shim.plist
```

**Linux and WSL2 (systemd user service).** Save as
`~/.config/systemd/user/podcastfeeds-llm-shim.service`, with your paths:

```ini
[Unit]
Description=podcastfeeds LLM shim (claude CLI wrapper)
After=network.target

[Service]
ExecStart=/home/YOU/podcastfeeds/.venv/bin/python /home/YOU/podcastfeeds/scripts/llm_shim.py
Restart=on-failure
Environment=PATH=/home/YOU/.local/bin:/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now podcastfeeds-llm-shim
loginctl enable-linger "$USER"           # keep it running when logged out
```

The shim logs to `data/llm_shim.log`. Each call uses your Claude plan's usage.

## Keeping it running on a Mac

The feed is only reachable while the Mac is awake and Docker is running. New
episodes are made when it wakes up, but your podcast app can't fetch anything
while it sleeps.

- Docker Desktop: Settings, General, **Start Docker Desktop when you sign in**.
  OrbStack starts at login by default. The container restarts with Docker.
- Tailscale: enable **Launch at login**. `serve` and `funnel` settings persist.
- Sleep: on a desktop Mac, System Settings, Energy, turn on **Prevent automatic
  sleeping when the display is off**. On a laptop on power,
  `sudo pmset -c sleep 0` does the same.

## Optional extras

- **Paid Substack posts:** copy `config/secrets.yaml.example` to
  `config/secrets.yaml` and add your `substack.sid` cookie. Without it, paid
  posts become their free preview.
- **ElevenLabs voices:** set `ELEVENLABS_API_KEY` in `.env`; it is opt-in per
  source and capped by `ELEVENLABS_CHAR_BUDGET` characters a month.
- **Audio courses:** the `audio-course` Claude Code skill writes a course on any
  topic and sends it to the Learning feed. See the main README.

Parts of the main README describe the author's own setup and can be ignored:
the TickTick integration, the private-leak git hooks, the cookie-health cron and
the Windows startup launcher.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Container restarts, `PermissionError` under `/data` in `docker compose logs` | Linux: `APP_UID`/`APP_GID` not set, or `data/` was created by Docker as root. `sudo chown -R "$(id -u):$(id -g)" data config`, then `docker compose up -d --build`. |
| Feed loads but episodes won't play on the phone | `BASE_URL` is missing or wrong, so audio links point at `localhost`. |
| Pocket Casts or Overcast can't find the feed | Funnel is off; those apps fetch from their own servers. |
| Digests are just headlines, no images described | The LLM shim isn't running or can't be reached. Check `data/llm_shim.log`: a 403 means the container's requests arrive from an address the shim doesn't accept as local. |
| A source never produces episodes | Check its row on the admin page and `docker compose logs -f`; some sites block automated fetches. |
