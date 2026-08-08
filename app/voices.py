"""Persistent voice roster.

Two layers:
  1. CURATED — researched, dialect-matched voices for known authors/speakers.
     Each source's author gets a voice chosen to fit their accent and delivery.
  2. MATCHED — a named person in a transcript (interview guest, screenshot
     conversation) gets a voice of their own gender, in the closest available
     accent. Who they are is looked up once and cached (speaker_profile).
  3. POOL — everything else (quote voices, image describers, labels that are
     not a specific person) draws a contrasting voice from a pool on first use.

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
import logging

from sqlmodel import select

from . import db
from .db import KV

log = logging.getLogger("podcastfeeds")

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

# Every en-*/da-* edge-tts voice, tagged (gender, accent) — from
# edge_tts.list_voices() on 2026-08-08. The POOL above is a *contrast* pool
# built for quote/describer voices, so handing it out for real people made
# gender essentially random: Jerusalem Demsas drew a male New Zealand voice
# while Matt Yglesias drew a female Indian one (ep. 447 feedback). Named
# speakers are matched against this table instead.
VOICE_CATALOG: dict[str, tuple[str, str]] = {
    "en-US-AndrewNeural": ("m", "US"),
    "en-US-AndrewMultilingualNeural": ("m", "US"),
    "en-US-BrianNeural": ("m", "US"),
    "en-US-BrianMultilingualNeural": ("m", "US"),
    "en-US-ChristopherNeural": ("m", "US"),
    "en-US-EricNeural": ("m", "US"),
    "en-US-GuyNeural": ("m", "US"),
    "en-US-RogerNeural": ("m", "US"),
    "en-US-SteffanNeural": ("m", "US"),
    "en-US-AriaNeural": ("f", "US"),
    "en-US-AvaNeural": ("f", "US"),
    "en-US-AvaMultilingualNeural": ("f", "US"),
    "en-US-EmmaNeural": ("f", "US"),
    "en-US-EmmaMultilingualNeural": ("f", "US"),
    "en-US-JennyNeural": ("f", "US"),
    "en-US-MichelleNeural": ("f", "US"),
    "en-US-AnaNeural": ("f", "US"),          # child voice — never auto-assigned
    "en-GB-RyanNeural": ("m", "GB"),
    "en-GB-ThomasNeural": ("m", "GB"),
    "en-GB-LibbyNeural": ("f", "GB"),
    "en-GB-MaisieNeural": ("f", "GB"),
    "en-GB-SoniaNeural": ("f", "GB"),
    "en-CA-LiamNeural": ("m", "CA"),
    "en-CA-ClaraNeural": ("f", "CA"),
    "en-AU-WilliamMultilingualNeural": ("m", "AU"),
    "en-AU-NatashaNeural": ("f", "AU"),
    "en-IE-ConnorNeural": ("m", "IE"),
    "en-IE-EmilyNeural": ("f", "IE"),
    "en-NZ-MitchellNeural": ("m", "NZ"),
    "en-NZ-MollyNeural": ("f", "NZ"),
    "en-ZA-LukeNeural": ("m", "ZA"),
    "en-ZA-LeahNeural": ("f", "ZA"),
    "en-IN-PrabhatNeural": ("m", "IN"),
    "en-IN-NeerjaNeural": ("f", "IN"),
    "en-IN-NeerjaExpressiveNeural": ("f", "IN"),
    "en-SG-WayneNeural": ("m", "SG"),
    "en-SG-LunaNeural": ("f", "SG"),
    "en-HK-SamNeural": ("m", "HK"),
    "en-HK-YanNeural": ("f", "HK"),
    "en-PH-JamesNeural": ("m", "PH"),
    "en-PH-RosaNeural": ("f", "PH"),
    "en-KE-ChilembaNeural": ("m", "KE"),
    "en-KE-AsiliaNeural": ("f", "KE"),
    "en-NG-AbeoNeural": ("m", "NG"),
    "en-NG-EzinneNeural": ("f", "NG"),
    "en-TZ-ElimuNeural": ("m", "TZ"),
    "en-TZ-ImaniNeural": ("f", "TZ"),
    "da-DK-JeppeNeural": ("m", "DK"),
    "da-DK-ChristelNeural": ("f", "DK"),
}

# The accents a speaker can be matched to, in the order tried when their own
# has no voice left (or no voice at all — there is no Ukrainian or German
# English voice, so Zelenskyy lands on a neutral one rather than a random one).
ACCENTS = ("US", "GB", "CA", "AU", "IE", "NZ", "ZA", "IN",
           "SG", "HK", "PH", "KE", "NG", "TZ", "DK")

# en-GB-Ryan/Thomas stay out of the contrast POOL (they are fixed config
# voices) but the matcher may still use them: they are the only British male
# voices edge-tts has, so excluding them would leave every British man reading
# in an American accent. Only the child voice is off-limits outright.
NOT_AUTO_ASSIGNED = frozenset({"en-US-AnaNeural"})

# The image describer is the app's own narrator, not the publication's: the
# same voice explaining a screenshot in every episode whatever the source
# (Hans, 2026-08-08). It was previously drawn per source ("{slug}#images"), so
# the describer changed identity between feeds. Kept OUT of VOICE_POOLS so no
# source's quote or question voice can collide with it inside an episode.
#
# Danish has only two voices, both needed for narration, so the Danish
# describer necessarily doubles as some sources' quote voice.
DESCRIBER_VOICES: dict[str, str] = {
    "en": "en-GB-LibbyNeural",       # calm British female — distinct from the
                                     # mostly-American narrators and from the
                                     # "view from Denmark" closer (Aria)
    "da": "da-DK-ChristelNeural",
}


def describer_voice(language: str) -> str:
    """The one voice that reads image descriptions, in every episode."""
    return DESCRIBER_VOICES.get(language, DESCRIBER_VOICES["en"])


ROSTER_PREFIX = "voice:"
PROFILE_PREFIX = "speaker-profile:"


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


def _accent_order(accent: str) -> tuple[str, ...]:
    """The speaker's own accent first, then the rest by how commonly they read
    as neutral — so a miss degrades to 'plausible' rather than 'random'."""
    return (accent,) + tuple(a for a in ACCENTS if a != accent)


def match_voice(gender: str, accent: str, language: str,
                avoid: frozenset[str] = frozenset()) -> str | None:
    """The closest voice of the right gender, or None if gender is unknown.

    Priority is gender, then accent, then a voice nobody else has yet. Gender
    is never traded away, and the speaker's own accent outranks freshness: an
    Indian speaker sharing a voice with another Indian speaker in some other
    episode beats being read in an American accent. `avoid` holds the voices
    already speaking in THIS episode, which is the one clash worth dodging.
    """
    if gender not in ("m", "f"):
        return None
    prefix = f"{language}-"
    taken = set(get_roster().values()) | set(CURATED.values()) | set(avoid)

    def candidates(acc: str) -> list[str]:
        return [
            v for v, (g, a) in VOICE_CATALOG.items()
            if g == gender and a == acc and v.startswith(prefix)
            and v not in NOT_AUTO_ASSIGNED and v not in avoid
        ]

    own = _accent_order(accent)[:1]
    rest = _accent_order(accent)[1:]
    for group in (own, rest):
        for acc in group:
            free = [v for v in candidates(acc) if v not in taken]
            if free:
                return free[0]
        # nothing free in this group: reuse rather than break gender or, for
        # the speaker's own accent, rather than move to a foreign one
        for acc in group:
            reusable = candidates(acc)
            if reusable:
                return reusable[0]
    return None


async def speaker_profile(name: str, context: str = "") -> tuple[str, str]:
    """(gender, accent) for a named speaker, cached forever in the KV table.

    Returns ("unknown", "") when the label is not a specific real person
    ("Person 1", "Sources", "USA", "Villain"), when the LLM cannot say, or
    when no LLM backend is available — the caller then falls back to the
    contrast pool, which is the previous behaviour.
    """
    import json as _json

    key = f"{PROFILE_PREFIX}{name.lower()}"
    with db.session() as s:
        cached = db.kv_get(s, key)
    if cached:
        gender, _, accent = cached.partition("/")
        return gender, accent

    prompt = (
        "Identify this speaker from a podcast or interview transcript. Reply "
        'with ONLY a JSON object: {"person": true/false, "gender": "m"|"f"|'
        '"unknown", "accent": "<code>"}\n\n'
        '- "person": false if the label is not one specific real human — a '
        'placeholder ("Person 1", "First speaker"), a role ("Villain", '
        '"User"), an organisation, a country, or a product.\n'
        '- "gender": that person\'s gender. Use "unknown" unless you are '
        "confident who they are.\n"
        f"- \"accent\": the closest English accent from {', '.join(ACCENTS)} "
        "— their own nationality where one fits, else the nearest neutral.\n\n"
        "Search the web if the name alone is not enough to place them; most of "
        "these are working journalists, academics or public figures rather than "
        "household names.\n\n"
        + (f"Context: this transcript is {context}.\n" if context else "")
        + f"Speaker label: {name}"
    )
    try:
        from .summarize import llm

        # One cached lookup per speaker ever, so the search is worth it: without
        # it the model would not place Jerusalem Demsas, the speaker whose
        # swapped voice started all this.
        text = await llm(prompt, tools=["WebSearch"])
        start, end = text.find("{"), text.rfind("}")
        data = _json.loads(text[start:end + 1])
        gender = data.get("gender") if data.get("person") else "unknown"
        gender = gender if gender in ("m", "f") else "unknown"
        accent = data.get("accent") if data.get("accent") in ACCENTS else ""
        if gender == "unknown":
            accent = ""  # nothing to match on; the pool decides
    except Exception as exc:
        # No LLM (or a bad answer): stay silent and let the pool decide, so a
        # broken shim degrades to the old behaviour instead of failing the run.
        log.warning("speaker profile lookup failed for %r: %s", name, exc)
        return "unknown", ""

    with db.session() as s:
        db.kv_set(s, key, f"{gender}/{accent}")
    log.info("speaker profile: %s -> %s/%s", name, gender, accent or "-")
    return gender, accent


async def warm_speaker_voices(keyed_names: dict[str, str], language: str,
                              avoid: frozenset[str] = frozenset(),
                              context: str = "") -> None:
    """Resolve a matched voice for each {roster_key: display_name} that has no
    assignment yet, so the synchronous assign_voice() path below finds it.

    Curated keys and already-persisted ones are left alone — an established
    speaker keeps the voice the listener already knows.
    """
    for roster_key, display_name in keyed_names.items():
        if roster_key in CURATED:
            continue
        with db.session() as s:
            if db.kv_get(s, f"{ROSTER_PREFIX}{roster_key}"):
                continue
        gender, accent = await speaker_profile(display_name, context)
        voice = match_voice(gender, accent, language, avoid)
        if not voice:
            continue  # unknown speaker: assign_voice() draws from the pool
        with db.session() as s:
            db.kv_set(s, f"{ROSTER_PREFIX}{roster_key}", voice)
