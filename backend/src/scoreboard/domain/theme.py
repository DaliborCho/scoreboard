"""Theme tokens and the rules that keep a screen readable.

Customers get colours, a logo and a font from a curated set. They never get
CSS. A television runs unattended for months in a room where nobody can fix
it, so the system has to guarantee that no combination a user can reach
produces an unreadable board.

Contrast is therefore checked on save and refused, not warned about. A warning
in a settings screen is advice; this needs to be a rule.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Curated because a user-supplied font file is a support burden and a licence
# question, and because these are the ones that stay legible at four metres.
FONTS = {
    "inter": "Inter, system-ui, sans-serif",
    "barlow": "Barlow Condensed, Impact, sans-serif",
    "roboto": "Roboto, Arial, sans-serif",
    "oswald": "Oswald, Haettenschweiler, sans-serif",
    "source": "Source Sans 3, Segoe UI, sans-serif",
    "jetbrains": "JetBrains Mono, Consolas, monospace",
}

DENSITIES = ("comfortable", "compact")

COLOR_TOKENS = ("background", "surface", "text", "muted", "primary", "accent")

DEFAULT_TOKENS: dict = {
    "background": "#0b1020",
    "surface": "#161d33",
    "text": "#f6f8ff",
    "muted": "#9aa7c7",
    "primary": "#3b6cf6",
    "accent": "#f5a524",
    "font": "inter",
    "density": "comfortable",
    "logo_url": "",
    "background_image_url": "",
}

# Web minimum is 4.5 for body text. A board is read across a room, so text on
# its own background is held to a higher bar and the rest to the standard one.
MIN_CONTRAST_TEXT = 7.0
MIN_CONTRAST_SECONDARY = 4.5
MIN_CONTRAST_ACCENT = 3.0


@dataclass
class Problem:
    token: str
    message: str

    def as_dict(self) -> dict:
        return {"token": self.token, "message": self.message}


# ---------------------------------------------------------------- colour maths
def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def parse_hex(value: str) -> tuple[int, int, int]:
    text = (value or "").strip()
    if not HEX.match(text):
        raise ValueError(f"'{value}' is not a hex colour.")
    digits = text.lstrip("#")
    if len(digits) == 3:
        digits = "".join(d * 2 for d in digits)
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def luminance(color: str) -> float:
    r, g, b = parse_hex(color)
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG contrast, from 1 (identical) to 21 (black on white)."""
    a, b = luminance(first), luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


# ---------------------------------------------------------------- tokens
def resolve(*layers: dict | None) -> dict:
    """Merge theme layers, later winning over earlier.

    Used as resolve(DEFAULT_TOKENS, org_tokens, team_tokens) so a team may
    override its own colours while inheriting everything it has not set, and
    an organization's brand still shows through the gaps.
    """
    merged = dict(DEFAULT_TOKENS)
    for layer in layers:
        if not layer:
            continue
        for key, value in layer.items():
            if key in merged and value not in (None, ""):
                merged[key] = value
    return merged


def validate(tokens: dict) -> list[Problem]:
    """Every reason this theme would be unacceptable on a screen."""
    problems: list[Problem] = []
    resolved = resolve(tokens)

    for token in COLOR_TOKENS:
        try:
            parse_hex(resolved[token])
        except ValueError as exc:
            problems.append(Problem(token, str(exc)))

    if problems:
        # Contrast checks would be meaningless with an unparsable colour.
        return problems

    if resolved["font"] not in FONTS:
        problems.append(
            Problem("font", f"Choose one of: {', '.join(sorted(FONTS))}.")
        )
    if resolved["density"] not in DENSITIES:
        problems.append(
            Problem("density", f"Choose one of: {', '.join(DENSITIES)}.")
        )

    checks = (
        ("text", "background", MIN_CONTRAST_TEXT, "Main text on the board background"),
        ("text", "surface", MIN_CONTRAST_TEXT, "Main text on cards"),
        ("muted", "background", MIN_CONTRAST_SECONDARY, "Secondary text on the background"),
        ("muted", "surface", MIN_CONTRAST_SECONDARY, "Secondary text on cards"),
        ("accent", "surface", MIN_CONTRAST_ACCENT, "Highlight colour on cards"),
        ("primary", "background", MIN_CONTRAST_ACCENT, "Primary colour on the background"),
    )
    for foreground, background, minimum, label in checks:
        ratio = contrast_ratio(resolved[foreground], resolved[background])
        if ratio < minimum:
            problems.append(
                Problem(
                    foreground,
                    # Two decimals, matching readability_report. Rounding the
                    # same ratio to one decimal in two places produced "1.4"
                    # in the list and "1.5" in the message beside it.
                    f"{label} is too faint to read across a room "
                    f"({ratio:.2f}:1, needs {minimum:.2f}:1). "
                    f"Darken the {background} or lighten the {foreground}.",
                )
            )

    if resolved["surface"] == resolved["background"]:
        problems.append(
            Problem("surface", "Cards would be invisible against the background.")
        )

    return problems


def readability_report(tokens: dict) -> dict:
    """Ratios for the settings screen to show live while someone picks colours."""
    resolved = resolve(tokens)
    try:
        pairs = {
            "text_on_background": contrast_ratio(resolved["text"], resolved["background"]),
            "text_on_surface": contrast_ratio(resolved["text"], resolved["surface"]),
            "muted_on_background": contrast_ratio(resolved["muted"], resolved["background"]),
            "accent_on_surface": contrast_ratio(resolved["accent"], resolved["surface"]),
        }
    except ValueError:
        return {"ok": False, "ratios": {}}
    return {
        "ok": not validate(tokens),
        "ratios": {k: round(v, 2) for k, v in pairs.items()},
        "font_stack": FONTS.get(resolved["font"], FONTS["inter"]),
    }


def catalogue() -> dict:
    """What the editor may offer. The UI never invents options of its own."""
    return {
        "fonts": [{"key": k, "stack": v} for k, v in sorted(FONTS.items())],
        "densities": list(DENSITIES),
        "color_tokens": list(COLOR_TOKENS),
        "defaults": dict(DEFAULT_TOKENS),
        "minimums": {
            "text": MIN_CONTRAST_TEXT,
            "secondary": MIN_CONTRAST_SECONDARY,
            "accent": MIN_CONTRAST_ACCENT,
        },
    }
