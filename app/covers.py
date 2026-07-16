"""Generate simple per-feed cover art (1400x1400 PNG, color from slug hash)."""
from __future__ import annotations

import colorsys
import hashlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import DATA_DIR

COVER_DIR = DATA_DIR / "covers"
SIZE = 1400


def _colors(slug: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    hue = int(hashlib.sha256(slug.encode()).hexdigest()[:4], 16) / 0xFFFF
    top = colorsys.hls_to_rgb(hue, 0.32, 0.55)
    bottom = colorsys.hls_to_rgb((hue + 0.08) % 1, 0.18, 0.6)
    return (
        tuple(int(c * 255) for c in top),  # type: ignore[return-value]
        tuple(int(c * 255) for c in bottom),  # type: ignore[return-value]
    )


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def cover_path(slug: str, name: str) -> Path:
    """Return (and lazily create) the cover PNG for a feed."""
    COVER_DIR.mkdir(parents=True, exist_ok=True)
    path = COVER_DIR / f"{slug}.png"
    if path.exists():
        return path

    top, bottom = _colors(slug)
    img = Image.new("RGB", (SIZE, SIZE))
    for y in range(SIZE):  # vertical gradient
        t = y / SIZE
        row = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3))
        ImageDraw.Draw(img).line([(0, y), (SIZE, y)], fill=row)

    draw = ImageDraw.Draw(img)
    words = name.split()
    lines, current = [], ""
    font = _font(120)
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) > SIZE - 200 and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    lines = lines[:4]

    total_h = len(lines) * 150
    y = (SIZE - total_h) // 2
    for line in lines:
        w = draw.textlength(line, font=font)
        draw.text(((SIZE - w) // 2, y), line, font=font, fill=(255, 255, 255))
        y += 150

    small = _font(48)
    draw.text((100, SIZE - 130), "podcastfeeds", font=small, fill=(255, 255, 255, 128))
    img.save(path)
    return path
