"""Persistent voice roster.

Two layers:
  1. CURATED — researched, dialect-matched voices for known authors/speakers.
     Each source's author gets a voice chosen to fit their accent and delivery.
  2. POOL — everything else (quote voices, image describers, unknown interview
     guests) draws a contrasting voice from a pool on first use.

Either way the assignment is PERSISTED in the KV table, so a given speaker
always sounds the same. `reset_roster()` clears persisted assignments (the
CURATED map then re-applies on next use); explicit `voice:` in sources.yaml
still bypasses everything (fixed voices like general news / Home Assistant).

Accent note: edge-tts covers US/GB/IE/AU/IN/CA/NZ/ZA English but has NO
German-accented English — Fabian Hoffmann (Missile Matters) is therefore an
approximation and a good candidate for an ElevenLabs voice later.
"""
from __future__ import annotations

import hashlib

from sqlmodel import select

from . import db
from .db import KV

# Reserved fixed voices (set via sources.yaml, kept OUT of the pool):
#   en-GB-RyanNeural   — general news / AI digests (male, British anchor)
#   en-GB-ThomasNeural — Home Assistant release notes (male, British)

# Researched per-author voices. Reuse across sources is fine — two sources are
# never heard in the same episode; within an episode the quote/guest/describer
# voices are drawn from the (contrasting) pool.
CURATED: dict[str, str] = {
    # ── American male essayists (distinct US timbres) ────────────────────
    "noahpinion":          "en-US-AndrewNeural",       # Noah Smith — warm, casual, conversational
    "slowboring":          "en-US-BrianNeural",        # Matt Yglesias — measured, wonky
    "silverbulletin":      "en-US-GuyNeural",          # Nate Silver — even, analytical
    "derekthompson":       "en-US-EricNeural",         # Derek Thompson — clear broadcast (podcaster)
    "understandingai":     "en-US-RogerNeural",        # Timothy B. Lee — plain, precise
    "pobrien":             "en-US-SteffanNeural",      # Phillips O'Brien — authoritative military historian
    "pmarca":              "en-US-ChristopherNeural",  # Marc Andreessen — intense, high-energy
    "astralcodexten":      "en-CA-LiamNeural",         # Scott Alexander — thoughtful, distinct N. American
    "thezvi":              "en-US-AndrewNeural",        # Zvi Mowshowitz — rapid analytical (reuse ok)
    "constructionphysics": "en-US-RogerNeural",        # Brian Potter — matter-of-fact engineer (reuse ok)
    "aifutures":           "en-US-EricNeural",         # Daniel Kokotajlo et al. (reuse ok)
    "garymarcus":          "en-US-ChristopherNeural",  # Gary Marcus — assertive, combative (reuse ok)
    # ── Accent-matched ───────────────────────────────────────────────────
    "aisnakeoil":          "en-IN-PrabhatNeural",      # Arvind Narayanan / Sayash Kapoor — Indian-American
    "missilematters":      "en-IE-ConnorNeural",       # Fabian Hoffmann — German (unavailable); distinct European-ish
    # ── Known recurring interview guests (speaker_voice keys) ────────────
    "speaker:callard":     "en-US-AriaNeural",         # Agnes Callard — American philosopher (f)
}

# Contrast pool for quotes / image describers / unknown guests / Danish closer.
# Female-first so it contrasts the mostly-male authors above, with varied accents.
VOICE_POOLS: dict[str, list[str]] = {
    "en": [
        "en-US-AriaNeural",
        "en-GB-SoniaNeural",
        "en-US-JennyNeural",
        "en-AU-NatashaNeural",
        "en-US-EmmaNeural",
        "en-IE-EmilyNeural",
        "en-US-MichelleNeural",
        "en-IN-NeerjaNeural",
        "en-CA-ClaraNeural",
        "en-US-AvaMultilingualNeural",
        "en-NZ-MitchellNeural",
        "en-ZA-LukeNeural",
    ],
    "da": [
        "da-DK-ChristelNeural",
        "da-DK-JeppeNeural",
    ],
}

ROSTER_PREFIX = "voice:"


def get_roster() -> dict[str, str]:
    """All persisted assignments: roster_key -> voice."""
    with db.session() as s:
        rows = s.exec(select(KV).where(KV.key.startswith(ROSTER_PREFIX))).all()  # type: ignore[attr-defined]
    return {r.key[len(ROSTER_PREFIX):]: r.value for r in rows}


def reset_roster() -> int:
    """Clear all persisted voice assignments (CURATED re-applies on next use).
    Returns the number cleared. Does not touch fixed config voices."""
    with db.session() as s:
        rows = s.exec(select(KV).where(KV.key.startswith(ROSTER_PREFIX))).all()  # type: ignore[attr-defined]
        for r in rows:
            s.delete(r)
        s.commit()
        return len(rows)


def assign_voice(roster_key: str, language: str) -> str:
    """Return the voice for this use case, persisting it on first use. Curated
    keys get their researched voice; everything else draws from the pool."""
    with db.session() as s:
        existing = db.kv_get(s, f"{ROSTER_PREFIX}{roster_key}")
        if existing:
            return existing
        if roster_key in CURATED:
            voice = CURATED[roster_key]
        else:
            pool = VOICE_POOLS.get(language, VOICE_POOLS["en"])
            used = set(get_roster().values()) | set(CURATED.values())
            free = [v for v in pool if v not in used]
            if free:
                voice = free[0]
            else:  # pool exhausted: stable hash pick so the key still maps consistently
                idx = int(hashlib.sha256(roster_key.encode()).hexdigest(), 16) % len(pool)
                voice = pool[idx]
        db.kv_set(s, f"{ROSTER_PREFIX}{roster_key}", voice)
        return voice
