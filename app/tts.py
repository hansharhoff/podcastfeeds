"""Text-to-speech via edge-tts, with chunking, concat and ID3 tagging."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import edge_tts
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, CHAP, CTOC, ID3, TIT2, CTOCFlags, ID3NoHeaderError
from mutagen.mp3 import MP3

from .config import MEDIA_DIR

log = logging.getLogger("podcastfeeds")

CHUNK_CHARS = 4000


def _split_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph, then sentence boundaries, keeping chunks under limit."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        pieces = [para] if len(para) <= limit else re.split(r"(?<=[.!?]) +", para)
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            while len(piece) > limit:  # pathological unbroken run
                chunks.append(piece[:limit])
                piece = piece[limit:]
            if len(current) + len(piece) + 1 > limit:
                if current:
                    chunks.append(current)
                current = piece
            else:
                current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks or [""]


EDGE_FALLBACK_VOICE = "en-US-AndrewNeural"

# A chunk with no letter or digit has nothing to say, and edge-tts answers
# NoAudioReceived rather than returning silence — which retries cannot fix.
# Ep. 299 (45 minutes) failed three times over an hour on a single two-character
# chunk, the leftover "~~" of a markdown strikethrough.
_SPEAKABLE = re.compile(r"[^\W_]", re.UNICODE)


def has_speech(text: str) -> bool:
    """Whether this text contains anything a voice could actually pronounce."""
    return bool(_SPEAKABLE.search(text))


async def _synth_chunk(text: str, voice: str, out_path: Path, attempts: int = 11) -> None:
    # ElevenLabs voices are encoded as "eleven:<voice_id>". Budget was already
    # checked at episode level; if a call fails here, fall back to edge-tts so
    # the episode still completes.
    if voice.startswith("eleven:"):
        from . import elevenlabs
        try:
            audio = await elevenlabs.synth(text, voice.split(":", 1)[1])
            out_path.write_bytes(audio)
            return
        except Exception as exc:
            log.warning("elevenlabs synth failed (%s); falling back to edge-tts", exc)
            voice = EDGE_FALLBACK_VOICE
    for attempt in range(1, attempts + 1):
        try:
            await edge_tts.Communicate(text, voice=voice).save(str(out_path))
            if out_path.stat().st_size > 0:
                return
            raise RuntimeError("edge-tts produced empty file")
        except Exception as exc:
            if attempt == attempts:
                raise
            # edge-tts returns NoAudioReceived under Microsoft-side throttling,
            # where a long episode's hundreds of back-to-back requests can
            # outlast a short retry budget — hence minutes of patience rather
            # than the original 45 seconds, since waiting costs far less than
            # re-synthesising a whole episode.
            #
            # Retrying only helps when the cause is transient, though: the same
            # exception is raised for a chunk with nothing to pronounce, and
            # that fails identically every time (ep. 299, a stray "~~"). Those
            # are filtered by has_speech() before they get here.
            delay = min(5 * attempt, 60)
            # Name the voice and the text: ep. 299 failed identically eleven
            # times and the log said only "chunk failed", so finding the
            # offending chunk meant replaying the whole episode by hand.
            log.warning(
                "edge-tts chunk failed (attempt %d/%d) [voice=%s, %d chars]: %s; "
                "retrying in %ds — text starts: %r",
                attempt, attempts, voice, len(text), exc, delay, text[:120],
            )
            await asyncio.sleep(delay)


async def _concat_mp3s(parts: list[Path], out_path: Path) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in parts:
            f.write(f"file '{p.as_posix()}'\n")
        list_file = f.name
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
        "-c", "copy", str(out_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    Path(list_file).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {stderr.decode()[-500:]}")


def _tag(path: Path, title: str, album: str, artist: str, date: str) -> None:
    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        audio = MP3(str(path))
        audio.add_tags()
        audio.save()
        tags = EasyID3(str(path))
    tags["title"] = title
    tags["album"] = album
    tags["artist"] = artist
    tags["date"] = date
    tags.save()


def _write_chapters(path: Path, chapters: list[tuple[float, dict]], total: float) -> None:
    """chapters: [(start_seconds, {"title": str, "image": jpeg bytes | None})]"""
    id3 = ID3(str(path))
    child_ids = []
    for i, (start, meta) in enumerate(chapters):
        end = chapters[i + 1][0] if i + 1 < len(chapters) else total
        sub_frames = [TIT2(encoding=3, text=[meta.get("title") or f"Chapter {i + 1}"])]
        if meta.get("image"):
            sub_frames.append(APIC(
                encoding=3, mime="image/jpeg", type=3, desc=f"chapter{i}",
                data=meta["image"],
            ))
        element_id = f"chp{i}"
        id3.add(CHAP(
            element_id=element_id, start_time=int(start * 1000),
            end_time=int(end * 1000), sub_frames=sub_frames,
        ))
        child_ids.append(element_id)
    id3.add(CTOC(
        element_id="toc", flags=CTOCFlags.TOP_LEVEL | CTOCFlags.ORDERED,
        child_element_ids=child_ids, sub_frames=[TIT2(encoding=3, text=["Chapters"])],
    ))
    id3.save()


async def synthesize_blocks(
    blocks: list[dict], title: str, album: str, artist: str, date: str,
    cover: bytes | None = None,
) -> tuple[str, int, int]:
    """Multi-voice synthesis with optional embedded chapters.

    blocks: [{"voice": str, "text": str, "chapter": {"title", "image"} | None}]
    A block with a "chapter" starts an ID3 chapter at its first spoken word.
    Returns (filename, bytes, seconds).
    """
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.mp3"
    out_path = MEDIA_DIR / filename

    chapters: list[tuple[float, dict]] = []
    with tempfile.TemporaryDirectory() as tmp:
        parts: list[Path] = []
        offset = 0.0
        for b in blocks:
            text = b["text"].strip()
            if not text:
                continue
            if b.get("chapter"):
                chapters.append((offset, b["chapter"]))
            for chunk in _split_text(text):
                if not has_speech(chunk):
                    # Punctuation-only leftovers (stray "~~", "---", bullets)
                    # would fail the whole episode; there is nothing to lose by
                    # dropping them.
                    log.info("skipping unspeakable chunk: %r", chunk[:40])
                    continue
                part = Path(tmp) / f"part{len(parts):05d}.mp3"
                await _synth_chunk(chunk, b["voice"], part)
                parts.append(part)
                offset += MP3(str(part)).info.length
        if not parts:
            raise RuntimeError("nothing to synthesize")
        if len(parts) == 1:
            shutil.move(str(parts[0]), out_path)  # not rename: /tmp and MEDIA_DIR may be different filesystems
        else:
            await _concat_mp3s(parts, out_path)

    _tag(out_path, title=title, album=album, artist=artist, date=date)
    if cover:
        mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        id3 = ID3(str(out_path))
        id3.add(APIC(encoding=3, mime=mime, type=3, desc="cover", data=cover))
        id3.save()
    total = MP3(str(out_path)).info.length
    if len(chapters) > 1:
        _write_chapters(out_path, chapters, total)
    return filename, out_path.stat().st_size, int(total)


async def synthesize(
    text: str, voice: str, title: str, album: str, artist: str, date: str,
    cover: bytes | None = None,
) -> tuple[str, int, int]:
    """Single-voice synthesis. Returns (filename, bytes, seconds)."""
    return await synthesize_blocks(
        [{"voice": voice, "text": text, "chapter": None}],
        title=title, album=album, artist=artist, date=date, cover=cover,
    )
