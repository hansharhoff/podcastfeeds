"""Outcome monitoring: notice when the service is quietly failing.

The October 2026 edge-tts break failed every episode for four days and nothing
said so; the July cookie expiry and a 19 hour LLM shim outage went the same
way. Each check here looks at an outcome (did episodes get made, did polls
succeed, did the podcast app fetch) rather than at one known cause, so a new
kind of breakage still shows up as one of these.

Problems surface in three places: a WARNING in the log, a banner on the admin
page, and an alert item at the top of the combined feed, because the podcast
app is the one screen Hans looks at every day.

Components report in through `record()`; `run_checks()` turns those reports
and the episode table into a list of open problems.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from xml.sax.saxutils import escape, quoteattr

from sqlmodel import select

from . import db
from .config import DATA_DIR, MEDIA_DIR, load_config
from .db import Episode, utcnow

log = logging.getLogger("podcastfeeds")

# Thresholds. Generous on purpose: an alert that cries wolf gets ignored.
ERROR_WINDOW = timedelta(hours=48)
MIN_ERRORS = 3                      # and at least as many errors as successes
NOTHING_NEW_AFTER = timedelta(hours=36)
POLL_STALE_MIN = timedelta(hours=24)
POLL_STALE_INTERVALS = 6            # or six missed poll intervals, if longer
LLM_DOWN_AFTER = timedelta(hours=12)
TICKTICK_DOWN_AFTER = timedelta(hours=24)
PODCAST_APP_SILENT_AFTER = timedelta(hours=24)
MIN_FREE_BYTES = 2 * 1024**3
STUCK_AFTER_HOURS = 12

ALERT_AUDIO = "monitor-alert.mp3"

# Latest result, rendered by the admin page.
LAST: dict = {"checked_at": "", "problems": []}


def _now() -> datetime:
    # SQLite hands datetimes back naive; compare everything as naive UTC.
    return utcnow().replace(tzinfo=None)


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=None) if ts else None
    except ValueError:
        return None


# ── reporting ───────────────────────────────────────────────────────────────

def record(component: str, ok: bool, detail: str = "") -> None:
    """Note that `component` just worked (ok) or failed. Never raises: a
    monitoring write must not take down the thing it monitors."""
    try:
        key = f"monitor:{component}"
        with db.session() as s:
            state = json.loads(db.kv_get(s, key) or "{}")
            now = _now().isoformat()
            if ok:
                state["last_ok"] = now
                state["fails_in_row"] = 0
            else:
                state["last_fail"] = now
                state["fails_in_row"] = int(state.get("fails_in_row", 0)) + 1
                state["detail"] = detail[:300]
            db.kv_set(s, key, json.dumps(state))
            s.commit()
    except Exception:
        log.exception("monitor.record failed for %s", component)


def _state(component: str) -> dict:
    with db.session() as s:
        return json.loads(db.kv_get(s, f"monitor:{component}") or "{}")


def _hours(delta: timedelta) -> str:
    h = int(delta.total_seconds() // 3600)
    return f"{h // 24} days" if h >= 48 else f"{h} hours"


# ── checks ──────────────────────────────────────────────────────────────────

def _error_class(error: str) -> str:
    """Group errors that differ only in URLs, ids or numbers."""
    text = re.sub(r"https?://\S+|wss?://\S+", "<url>", error or "unknown error")
    text = re.sub(r"\d+", "N", text)
    return text.strip()[:80]


def check_episode_errors() -> list[dict]:
    cutoff = _now() - ERROR_WINDOW
    with db.session() as s:
        rows = s.exec(select(Episode.status, Episode.error).where(
            Episode.created_at >= cutoff,
            Episode.status.in_(("ready", "error")),  # type: ignore[attr-defined]
        )).all()
    errors = [e for st, e in rows if st == "error"]
    ready = sum(1 for st, _ in rows if st == "ready")
    if len(errors) < MIN_ERRORS or len(errors) < ready:
        return []
    classes: dict[str, int] = {}
    for e in errors:
        classes[_error_class(e)] = classes.get(_error_class(e), 0) + 1
    top, n = max(classes.items(), key=lambda kv: kv[1])
    return [{"key": "episode-errors",
             "summary": f"{len(errors)} of {len(errors) + ready} episodes failed "
                        f"in the last 2 days",
             "detail": f"Most common ({n}): {top}"}]


def _rendered_at(ep: Episode) -> datetime | None:
    try:
        return _parse(json.loads(ep.provenance or "{}").get("generated_at", ""))
    except (ValueError, AttributeError):
        return None


def check_nothing_new() -> list[dict]:
    """The catch-all: whatever broke, no finished episode is the symptom."""
    hidden = [s.slug for s in load_config().sources if not s.in_all_feed]
    with db.session() as s:
        rows = s.exec(select(Episode).where(
            Episode.status == "ready",
            Episode.source_slug.not_in(hidden),  # type: ignore[attr-defined]
        ).order_by(Episode.id.desc()).limit(50)).all()  # type: ignore[union-attr]
        if not rows:
            return []  # a fresh install has nothing to compare against
        times = [t for t in (_rendered_at(e) or e.created_at.replace(tzinfo=None)
                             for e in rows) if t]
    newest = max(times)
    age = _now() - newest
    if age < NOTHING_NEW_AFTER:
        return []
    return [{"key": "nothing-new",
             "summary": f"No new episode in {_hours(age)}",
             "detail": "Nothing has finished since "
                       f"{newest:%d %b %H:%M} UTC. Check the admin page for errors."}]


def check_stuck() -> list[dict]:
    from .health import stuck_episodes

    stuck = stuck_episodes(STUCK_AFTER_HOURS)
    if not stuck:
        return []
    return [{"key": "stuck",
             "summary": f"{len(stuck)} episode{'s' if len(stuck) != 1 else ''} "
                        f"stuck for over {STUCK_AFTER_HOURS} hours",
             "detail": ", ".join(f"#{e['id']}" for e in stuck[:10])}]


def check_paid_access() -> list[dict]:
    from .health import LAST as paid

    broken = [r["name"] for r in paid.get("results", []) if not r.get("ok")]
    if not broken:
        return []
    return [{"key": "paid-access",
             "summary": "Paid Substack access broken: " + ", ".join(broken),
             "detail": "The substack.sid cookie in config/secrets.yaml has likely expired."}]


def check_polls() -> list[dict]:
    problems = []
    now = _now()
    for source in load_config().sources:
        if source.type != "rss":
            continue
        state = _state(f"poll:{source.slug}")
        if not state:
            continue  # never polled since monitoring started
        limit = max(POLL_STALE_MIN, timedelta(minutes=source.poll_minutes * POLL_STALE_INTERVALS))
        last_ok = _parse(state.get("last_ok", ""))
        since = last_ok or _parse(state.get("last_fail", ""))
        if since and now - since > limit and state.get("fails_in_row", 0):
            problems.append({
                "key": f"poll:{source.slug}",
                "summary": f"{source.name}: feed failing for {_hours(now - since)}",
                "detail": state.get("detail", "")})
    return problems


def _down_check(component: str, after: timedelta, key: str, label: str,
                hint: str) -> list[dict]:
    state = _state(component)
    if not state.get("fails_in_row"):
        return []
    last_ok = _parse(state.get("last_ok", ""))
    first_seen = last_ok or _parse(state.get("last_fail", ""))
    if first_seen and _now() - first_seen <= after:
        return []
    return [{"key": key,
             "summary": f"{label} failing" + (
                 f" for {_hours(_now() - last_ok)}" if last_ok else ""),
             "detail": f"{hint} Last error: {state.get('detail', '')}"}]


def check_llm() -> list[dict]:
    return _down_check(
        "llm", LLM_DOWN_AFTER, "llm", "LLM helper",
        "Digests fall back to headlines and images go undescribed. "
        "Is scripts/llm_shim.py running and is claude logged in?")


def check_ticktick() -> list[dict]:
    return _down_check(
        "ticktick", TICKTICK_DOWN_AFTER, "ticktick", "TickTick",
        "Rerun scripts/ticktick_auth.py if the token expired.")


def check_podcast_app() -> list[dict]:
    """Pocket Casts fetches through Funnel; silence means the public path is
    down (and that this very alert can't reach the app either)."""
    last = _parse(_state("pocketcasts").get("last_ok", ""))
    if not last or _now() - last <= PODCAST_APP_SILENT_AFTER:
        return []
    return [{"key": "pocketcasts",
             "summary": f"Pocket Casts hasn't fetched a feed in {_hours(_now() - last)}",
             "detail": "Tailscale Funnel may be off, or the machine unreachable."}]


def check_disk() -> list[dict]:
    try:
        free = shutil.disk_usage(DATA_DIR).free
    except OSError:
        return []
    if free >= MIN_FREE_BYTES:
        return []
    return [{"key": "disk",
             "summary": f"Low disk space: {free / 1024**3:.1f} GB free",
             "detail": f"On the volume holding {DATA_DIR}."}]


CHECKS = (check_episode_errors, check_nothing_new, check_stuck, check_paid_access,
          check_polls, check_llm, check_ticktick, check_podcast_app, check_disk)


def run_checks() -> list[dict]:
    """Run every check, remember when each problem was first seen, log changes,
    and refresh LAST. Returns the open problems."""
    problems: list[dict] = []
    for check in CHECKS:
        try:
            problems.extend(check())
        except Exception as exc:
            log.exception("monitor check %s crashed", check.__name__)
            problems.append({"key": f"check:{check.__name__}",
                             "summary": f"Health check {check.__name__} crashed",
                             "detail": str(exc)[:200]})
    now = _now().isoformat()
    with db.session() as s:
        seen = json.loads(db.kv_get(s, "monitor:open") or "{}")
        current = {p["key"]: seen.get(p["key"], now) for p in problems}
        for p in problems:
            p["since"] = current[p["key"]]
            if p["key"] not in seen:
                log.warning("HEALTH PROBLEM: %s. %s", p["summary"], p["detail"])
        for key in set(seen) - set(current):
            log.info("health problem resolved: %s", key)
        db.kv_set(s, "monitor:open", json.dumps(current))
        s.commit()
    LAST["checked_at"] = _now().strftime("%Y-%m-%d %H:%M UTC")
    LAST["problems"] = problems
    return problems


async def run_checks_job() -> None:
    run_checks()


# ── the feed alert ──────────────────────────────────────────────────────────

def _alert_audio() -> str:
    """A short tone, made with ffmpeg rather than TTS: the alert has to work
    when narration is the thing that is broken. Returns the file name, or ""
    when it can't be made."""
    path = MEDIA_DIR / ALERT_AUDIO
    if path.exists():
        return ALERT_AUDIO
    try:
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=660:duration=0.4,apad=pad_dur=0.3",
             "-f", "lavfi", "-i", "sine=frequency=880:duration=0.4",
             "-filter_complex", "[0][1]concat=n=2:v=0:a=1",
             "-codec:a", "libmp3lame", "-b:a", "64k", str(path)],
            check=True, timeout=30)
        return ALERT_AUDIO
    except Exception:
        log.exception("could not make the alert tone")
        return ""


def alert_item_xml(base: str, token: str) -> str:
    """An <item> for the combined feed while problems are open, else "".

    Its guid changes whenever the set of problems changes, so the podcast app
    shows a fresh episode (and notifies) for each new problem, and it vanishes
    from the feed once everything is resolved."""
    from .feedgen import _rfc2822

    problems = LAST.get("problems") or []
    if not problems:
        return ""
    audio = _alert_audio()
    if not audio:
        return ""
    keys = ",".join(sorted(p["key"] for p in problems))
    newest = max(_parse(p.get("since", "")) or _now() for p in problems)
    title = ("⚠ podcastfeeds: " + problems[0]["summary"] +
             (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""))
    notes = "".join(
        f"<p><strong>{escape(p['summary'])}</strong><br/>{escape(p['detail'])}"
        f"<br/><small>since {escape(p.get('since', '')[:16].replace('T', ' '))} UTC</small></p>"
        for p in problems)
    notes += (f'<p><a href="{escape(base)}/{escape(token)}/">Open the admin page</a>. '
              "This item disappears once the problems are fixed.</p>")
    url = f"{base}/{token}/media/{audio}"
    size = (MEDIA_DIR / audio).stat().st_size
    return "\n".join([
        "    <item>",
        f"      <title>{escape(title)}</title>",
        f"      <description><![CDATA[{notes}]]></description>",
        f'      <guid isPermaLink="false">monitor-alert:{escape(keys)}:{newest:%Y%m%d%H%M}</guid>',
        f"      <pubDate>{_rfc2822(newest)}</pubDate>",
        f'      <enclosure url={quoteattr(url)} length="{size}" type="audio/mpeg"/>',
        "      <itunes:duration>00:00:01</itunes:duration>",
        "    </item>",
    ])
