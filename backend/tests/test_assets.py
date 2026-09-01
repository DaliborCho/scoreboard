"""Upload validation.

The rule that carries the weight: what a file claims to be is not evidence.
Its own first bytes decide.
"""
import struct
import zlib

import pytest

from scoreboard.services import assets


def png(width=8, height=8) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"".join(b"\x00" + b"\xe0\x24\x24" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
GIF = b"GIF89a" + b"\x00" * 40
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 40
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.mark.parametrize(
    "data,expected",
    [(png(), "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_recognises_real_images(data, expected):
    assert assets.sniff(data) == expected
    assert assets.validate(data) == expected


def test_svg_is_refused_by_its_bytes():
    """Not on the extension, and not on the declared type — on the content."""
    assert assets.sniff(SVG) is None
    with pytest.raises(assets.AssetError) as exc:
        assets.validate(SVG, "image/png")
    assert "script" in str(exc.value)


def test_a_png_lying_about_its_type_is_still_refused():
    """The declared type is checked too, so a mislabelled upload is caught."""
    with pytest.raises(assets.AssetError):
        assets.validate(png(), "image/svg+xml")


def test_declared_type_is_not_trusted_on_its_own():
    """Claiming PNG does not make an HTML file into one."""
    with pytest.raises(assets.AssetError):
        assets.validate(b"<html><body>not an image</body></html>", "image/png")


def test_empty_upload():
    with pytest.raises(assets.AssetError) as exc:
        assets.validate(b"")
    assert "empty" in str(exc.value)


def test_oversized_upload():
    with pytest.raises(assets.AssetError) as exc:
        assets.validate(b"\x89PNG\r\n\x1a\n" + b"\x00" * (assets.MAX_BYTES + 1))
    message = str(exc.value)
    assert "larger than" in message
    # Regression: it once claimed the file's size and the limit were equal.
    assert message.count("KB") == 1


def test_every_accepted_type_has_an_extension():
    for content_type, suffix in assets.ALLOWED.items():
        assert suffix.startswith("."), content_type


def test_paths_are_separated_by_organization():
    """A mis-scoped read should be a visible path bug, not a silent hit."""
    one = assets.path_for(1, "abc", "image/png")
    two = assets.path_for(2, "abc", "image/png")
    assert one != two
    assert one.parent.name == "1" and two.parent.name == "2"
