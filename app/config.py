"""Settings from environment + source definitions from config/sources.yaml."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("podcastfeeds")

ROOT = Path(os.environ.get("PODCASTFEEDS_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", ROOT / "config"))
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "podcastfeeds.db"

TOKEN_FILE = DATA_DIR / "token.txt"


def get_token() -> str:
    """Secret URL token. Set TOKEN env var, or one is generated and persisted."""
    env = os.environ.get("TOKEN")
    if env:
        return env.strip()
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    return token


# Bump whenever script-generation logic changes — recorded in every episode's
# provenance so feedback ("episode X sounded wrong") maps to the code that made it.
PIPELINE_VERSION = "2026.07.16-15-review-hardening"

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")  # e.g. https://YOUR-MACHINE.YOUR-TAILNET.ts.net
PORT = int(os.environ.get("PORT", "8080"))


@dataclass
class SourceDef:
    slug: str
    name: str
    type: str  # rss | digest | breaking | inbox
    url: str = ""
    urls: list[str] = field(default_factory=list)  # digest: aggregate several feeds
    voice: str = ""  # fixed voice; empty -> persistent per-source/blogger roster
    language: str = "auto"  # da | en | auto
    poll_minutes: int = 30
    max_items_per_poll: int = 3
    schedule: str = "30 6 * * *"  # cron, digest sources only
    prefer_existing_audio: bool = True
    max_chars: int = 40000  # safety cap on TTS input length
    description: str = ""
    enabled: bool = True
    digest_max_items: int = 15
    title_filter: str = ""  # regex; rss entries whose title doesn't match are skipped
    llm_filter: str = ""  # criteria prose; entries are LLM-classified, non-matches skipped
    narrate_mode: str = "full"  # full: read the article | summary: LLM overview + show notes
    danish_perspective: bool = False  # append an Opus-written "view from Denmark" segment
    keep_available: int = 10  # record the latest N posts as skipped rows (browsable/unskippable)
    el_voice: str = ""  # ElevenLabs voice_id for this source's main voice (when EL enabled)

    def feed_urls(self) -> list[str]:
        return self.urls or ([self.url] if self.url else [])


@dataclass
class AppConfig:
    author: str = "podcastfeeds"
    retention_days: int = 60
    sources: list[SourceDef] = field(default_factory=list)
    # Maps a speaker name as it appears in screenshots (lowercase) to a roster
    # key, so e.g. a tweet by the blog's author is read in the blog's voice.
    speaker_aliases: dict[str, str] = field(default_factory=dict)


def load_config() -> AppConfig:
    # Real config is git-ignored (it lists your private subscriptions); fall back
    # to the tracked example so a fresh clone still boots.
    path = CONFIG_DIR / "sources.yaml"
    if not path.exists():
        path = CONFIG_DIR / "sources.yaml.example"
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    defaults = raw.get("defaults", {}) or {}
    meta = raw.get("meta", {}) or {}

    known = {f.name for f in SourceDef.__dataclass_fields__.values()}
    sources: list[SourceDef] = []
    for item in raw.get("sources", []) or []:
        merged = {**defaults, **item}
        merged = {k: v for k, v in merged.items() if k in known}
        sources.append(SourceDef(**merged))

    if not any(s.type == "inbox" for s in sources):
        sources.append(
            SourceDef(
                slug="inbox",
                name=meta.get("inbox_name", "Inbox"),
                type="inbox",
                description="Articles shared from your phone",
                voice=defaults.get("voice", ""),
            )
        )

    return AppConfig(
        author=meta.get("author", "podcastfeeds"),
        retention_days=int(defaults.get("retention_days", 60)),
        sources=[s for s in sources if s.enabled],
        speaker_aliases={
            str(k).lower(): str(v)
            for k, v in (meta.get("speaker_aliases", {}) or {}).items()
        },
    )


def load_cookies() -> dict[str, str]:
    """Per-domain Cookie headers from config/secrets.yaml (git-ignored) —
    used to fetch paid posts (e.g. Substack subscriptions) as the logged-in user.

    secrets.yaml:
      cookies:
        "www.noahpinion.blog": "substack.sid=..."
    """
    path = CONFIG_DIR / "secrets.yaml"
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        return {str(k).lower(): str(v) for k, v in (raw.get("cookies", {}) or {}).items()}
    except (yaml.YAMLError, OSError) as exc:
        log.warning("config/secrets.yaml unreadable (paid posts will hit paywalls): %s", exc)
        return {}


def pick_voice(source: SourceDef, language_hint: str = "", roster_key: str = "") -> str:
    """Fixed voice from config wins; otherwise the persistent roster assigns
    (and remembers) a voice for this use case."""
    if source.voice:
        return source.voice
    lang = language_hint or source.language
    if lang not in ("da", "en"):
        lang = "en"
    from .voices import assign_voice  # local import: voices imports db only

    return assign_voice(roster_key or source.slug, lang)
