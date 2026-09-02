"""Generate placeholder team crests and wordmarks.

A development helper, not part of the product. Real customers upload their own
artwork; this exists so a fresh installation looks like a leaderboard instead
of a spreadsheet, and so the theme editor has something to show.

Needs Pillow, which is deliberately not a project dependency — the application
never processes an image, it only stores and serves what it is given.

    docker compose exec api pip install pillow
    docker compose exec api python /app/tools/make_demo_logos.py --upload
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - helper script
    print("This helper needs Pillow:  pip install pillow")
    raise SystemExit(2) from None

BADGE = 320
HERO_W, HERO_H = 1400, 380
SUPERSAMPLE = 3  # Draw large and shrink, so the curves come out clean.


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def shield(size: int, fill, stroke, width: int) -> Image.Image:
    """A crest outline: straight shoulders, tapering to a point."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    w, h = size * 0.78, size * 0.86
    x, y = (size - w) / 2, (size - h) / 2

    points = [(x, y + h * 0.06)]
    points += [(x + w * 0.5, y), (x + w, y + h * 0.06)]
    points += [(x + w, y + h * 0.52)]
    # Sweep the lower half into a point rather than a rounded bowl; the
    # straighter taper reads better once it is 30 pixels wide on a wall.
    for step in range(21):
        t = step / 20
        angle = math.pi * 0.5 * t
        points.append((x + w * (1 - 0.5 * (1 - math.cos(angle))), y + h * (0.52 + 0.48 * math.sin(angle))))
    for step in range(21):
        t = 1 - step / 20
        angle = math.pi * 0.5 * t
        points.append((x + w * (0.5 * (1 - math.cos(angle))), y + h * (0.52 + 0.48 * math.sin(angle))))
    points.append((x, y + h * 0.52))

    draw.polygon(points, fill=fill)
    draw.line(points + [points[0]], fill=stroke, width=width, joint="curve")
    return canvas


def centred(draw: ImageDraw.ImageDraw, box, text: str, typeface, fill) -> None:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=typeface)
    x = box[0] + (box[2] - box[0] - (right - left)) / 2 - left
    y = box[1] + (box[3] - box[1] - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=typeface, fill=fill)


def make_badge(initials: str, primary: str, accent: str) -> Image.Image:
    size = BADGE * SUPERSAMPLE
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # A soft dark halo keeps the crest legible on both pale and busy artwork.
    halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse(
        [size * 0.06, size * 0.06, size * 0.94, size * 0.94], fill=(0, 0, 0, 90)
    )
    canvas.alpha_composite(halo.filter(ImageFilter.GaussianBlur(size * 0.03)))

    canvas.alpha_composite(shield(size, primary, accent, int(size * 0.035)))

    draw = ImageDraw.Draw(canvas)
    centred(
        draw,
        (size * 0.18, size * 0.18, size * 0.82, size * 0.74),
        initials,
        font(int(size * 0.34)),
        "#ffffff",
    )
    return canvas.resize((BADGE, BADGE), Image.LANCZOS)


def bold_text(draw, xy, text, typeface, fill, weight=2) -> None:
    """Fake a heavier weight by overprinting.

    The bundled default font has one thin weight, and a wordmark drawn with an
    outline instead reads as hollow rather than strong.
    """
    x, y = xy
    for dx in range(-weight, weight + 1):
        for dy in range(-weight, weight + 1):
            draw.text((x + dx, y + dy), text, font=typeface, fill=fill)


def make_hero(name: str, initial: str, primary: str, accent: str) -> Image.Image:
    width, height = HERO_W * SUPERSAMPLE // 2, HERO_H * SUPERSAMPLE // 2
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    # The crest sits in the upper half and the wordmark clears it entirely.
    # Overlapping the two made the name unreadable at the size a wall shows it.
    crest_size = int(height * 0.54)
    crest = shield(crest_size, primary, accent, int(crest_size * 0.05))
    crest_draw = ImageDraw.Draw(crest)
    centred(
        crest_draw,
        (crest_size * 0.18, crest_size * 0.16, crest_size * 0.82, crest_size * 0.72),
        initial,
        font(int(crest_size * 0.36)),
        "#ffffff",
    )
    canvas.alpha_composite(crest, (int(width / 2 - crest_size / 2), 0))

    draw = ImageDraw.Draw(canvas)
    letters = name.upper()
    size = int(height * 0.30)
    while draw.textlength(letters, font=font(size)) > width * 0.84 and size > 12:
        size -= 2
    typeface = font(size)

    text_width = draw.textlength(letters, font=typeface)
    x = (width - text_width) / 2
    y = height * 0.58

    bold_text(draw, (x + size * 0.05, y + size * 0.05), letters, typeface, (0, 0, 0, 150), weight=2)
    bold_text(draw, (x, y), letters, typeface, accent, weight=2)

    # Rules either side, as on a printed scoreboard.
    rule_y = y + size * 0.52
    thickness = max(3, int(size * 0.07))
    gap = size * 0.4
    for x0, x1 in ((width * 0.03, x - gap), (x + text_width + gap, width * 0.97)):
        if x1 - x0 > width * 0.04:
            draw.rectangle([x0, rule_y, x1, rule_y + thickness], fill=accent)

    return canvas.resize((HERO_W, HERO_H), Image.LANCZOS)


# Chosen bright enough to clear the 3:1 minimum against a dark board. The
# first pass used deeper shades and the theme validator refused them, which is
# the rule doing exactly what it exists for.
TEAMS = [
    ("Team Alpha", "ALPHA", "A", "#ef4444", "#fcd34d"),
    ("Team Bravo", "BRAVO", "B", "#22c55e", "#86efac"),
    ("Team Charlie", "CHARLIE", "C", "#06b6d4", "#67e8f9"),
    ("Team Delta", "DELTA", "D", "#f97316", "#fdba74"),
    ("Undisputed", "UNDISPUTED", "U", "#a855f7", "#e9d5ff"),
]
ORGANIZATION = ("Demo Company", "SALES LEADERBOARD", "#3b6cf6", "#fbbf24")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="/tmp/demo-logos")
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    for _, wordmark, initial, primary, accent in TEAMS:
        slug = wordmark.lower()
        make_badge(initial, primary, accent).save(out / f"badge-{slug}.png")
        make_hero(wordmark, initial, primary, accent).save(out / f"hero-{slug}.png")
        print(f"  {slug}: badge + hero")

    _, wordmark, primary, accent = ORGANIZATION
    make_hero(wordmark, "S", primary, accent).save(out / "hero-office.png")
    print("  office hero")

    print(f"\nWritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
