"""Theme rules.

The contrast checks are the product promise, not a nicety: a television runs
unattended for months, so an unreadable board has to be impossible to save
rather than merely discouraged.
"""
import pytest

from scoreboard.domain.theme import (
    DEFAULT_TOKENS,
    MIN_CONTRAST_TEXT,
    contrast_ratio,
    parse_hex,
    readability_report,
    resolve,
    validate,
)

READABLE = {
    "background": "#0a0f1e", "surface": "#151d33", "text": "#ffffff",
    "muted": "#9fb0d4", "primary": "#4f7cff", "accent": "#ffb020",
}


# ---------------------------------------------------------------- colour maths
def test_contrast_extremes():
    assert round(contrast_ratio("#000000", "#ffffff"), 2) == 21.0
    assert contrast_ratio("#336699", "#336699") == 1.0


def test_contrast_is_symmetric():
    assert contrast_ratio("#123456", "#eeeeee") == contrast_ratio("#eeeeee", "#123456")


@pytest.mark.parametrize("value,expected", [("#fff", (255, 255, 255)), ("#0a0f1e", (10, 15, 30))])
def test_short_and_long_hex(value, expected):
    assert parse_hex(value) == expected


@pytest.mark.parametrize("value", ["", "red", "#12345", "0a0f1e", "#gggggg", None])
def test_bad_hex_is_rejected(value):
    with pytest.raises(ValueError):
        parse_hex(value)


# ---------------------------------------------------------------- validation
def test_readable_theme_passes():
    assert validate(READABLE) == []
    assert readability_report(READABLE)["ok"] is True


def test_defaults_are_readable():
    """The shipped palette has to pass its own rule."""
    assert validate(DEFAULT_TOKENS) == []


def test_faint_text_is_refused():
    problems = validate({**READABLE, "text": "#2b3040"})
    assert problems
    assert any(p.token == "text" for p in problems)


def test_invisible_cards_are_refused():
    problems = validate({**READABLE, "surface": READABLE["background"]})
    assert any(p.token == "surface" for p in problems)


def test_unknown_font_is_refused():
    assert any(p.token == "font" for p in validate({**READABLE, "font": "comic-sans"}))


def test_unparsable_colour_short_circuits_contrast():
    """A broken colour must not produce a cascade of nonsense ratio messages."""
    problems = validate({**READABLE, "text": "not-a-colour"})
    assert len(problems) == 1
    assert problems[0].token == "text"


def test_message_and_report_agree_on_the_number():
    """Regression: the same ratio was rounded twice, showing 1.4 beside 1.5."""
    tokens = {**READABLE, "text": "#2b3040"}
    ratio = readability_report(tokens)["ratios"]["text_on_background"]
    message = next(p.message for p in validate(tokens) if p.token == "text")
    assert f"{ratio:.2f}:1" in message
    assert f"{MIN_CONTRAST_TEXT:.2f}:1" in message


# ---------------------------------------------------------------- inheritance
def test_team_layer_overrides_only_what_it_sets():
    merged = resolve(READABLE, {"primary": "#e02424"})
    assert merged["primary"] == "#e02424"
    assert merged["background"] == READABLE["background"]
    assert merged["accent"] == READABLE["accent"]


def test_blank_values_do_not_erase_the_organization_brand():
    merged = resolve(READABLE, {"primary": "", "accent": None})
    assert merged["primary"] == READABLE["primary"]
    assert merged["accent"] == READABLE["accent"]


def test_unknown_keys_are_ignored():
    merged = resolve(READABLE, {"evil": "<script>", "primary": "#111111"})
    assert "evil" not in merged
    assert merged["primary"] == "#111111"


def test_empty_layer_yields_defaults():
    assert resolve({}) == DEFAULT_TOKENS
    assert resolve(None, None) == DEFAULT_TOKENS
