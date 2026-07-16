"""ElevenLabs TTS with a hard monthly spend cap.

Opt-in PER SOURCE (toggled from the admin panel; stored in the KV table as
`el:{slug}`). When a source is enabled AND the budget allows the whole episode,
its main narration voice is rendered by ElevenLabs; otherwise it silently falls
back to edge-tts. Quotes / image describers / guests stay on edge-tts.

Hard cap: before an episode we require both
  (a) ElevenLabs' own reported remaining quota (character_limit - character_count), and
  (b) our own budget (ELEVENLABS_CHAR_BUDGET chars/month, tracked in KV),
to cover the text. Whichever is smaller binds. We never send text that would
exceed either, so spend can't run past the cap (on the free tier the 10k/mo API
limit binds; on a paid plan set ELEVENLABS_CHAR_BUDGET to your ~$100 worth).
"""
from __future__ import annotations

import logging
import os

import httpx

from . import db

log = logging.getLogger("podcastfeeds")

API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_flash_v2_5")  # ~0.5 credit/char
# Monthly character budget. Flash ≈ 0.5 credit/char, so 1,000,000 chars ≈ 500k
# credits ≈ within the $99 Pro plan. Tune per plan; the ElevenLabs-reported
# quota is also honoured, so the smaller of the two always binds.
CHAR_BUDGET = int(os.environ.get("ELEVENLABS_CHAR_BUDGET", "1000000"))
# A default premade voice id (used when a source sets no el_voice).
DEFAULT_VOICE = os.environ.get("ELEVENLABS_DEFAULT_VOICE", "pNInz6obpgDQGcFmaJgB")

API = "https://api.elevenlabs.io/v1"


def configured() -> bool:
    return bool(API_KEY)


def _month_key() -> str:
    from .db import utcnow
    return f"el_used:{utcnow():%Y-%m}"


def used_this_month() -> int:
    with db.session() as s:
        return int(db.kv_get(s, _month_key(), "0") or "0")


def _record(chars: int) -> None:
    with db.session() as s:
        cur = int(db.kv_get(s, _month_key(), "0") or "0")
        db.kv_set(s, _month_key(), str(cur + chars))


async def api_remaining() -> int | None:
    """ElevenLabs-reported remaining chars this cycle, or None if unavailable.
    Async so it never blocks the event loop."""
    if not API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{API}/user/subscription", headers={"xi-api-key": API_KEY})
            r.raise_for_status()
            d = r.json()
        return max(0, int(d.get("character_limit", 0)) - int(d.get("character_count", 0)))
    except Exception as exc:
        log.warning("elevenlabs subscription check failed: %s", exc)
        return None


async def budget_remaining() -> int:
    """Chars we may still send this month: min(our budget, EL-reported quota)."""
    ours = max(0, CHAR_BUDGET - used_this_month())
    api = await api_remaining()
    return min(ours, api) if api is not None else ours


def el_enabled(slug: str) -> bool:
    with db.session() as s:
        return db.kv_get(s, f"el:{slug}") == "1"


def set_enabled(slug: str, on: bool) -> None:
    with db.session() as s:
        db.kv_set(s, f"el:{slug}", "1" if on else "0")


async def synth(text: str, voice_id: str) -> bytes:
    """Render text to mp3 bytes via ElevenLabs and record the character spend.
    Raises on any error (caller falls back to edge-tts)."""
    voice_id = voice_id or DEFAULT_VOICE
    url = f"{API}/text-to-speech/{voice_id}"
    payload = {"text": text, "model_id": MODEL,
               "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}}
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            url, headers={"xi-api-key": API_KEY, "accept": "audio/mpeg"}, json=payload
        )
        resp.raise_for_status()
        audio = resp.content
    if not audio:
        raise RuntimeError("elevenlabs returned empty audio")
    _record(len(text))
    return audio
