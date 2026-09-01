"""Storage for uploaded brand images.

A local directory today, behind an interface, because the only thing that has
to change for object storage later is this file. Nothing above it knows where
a file physically lives.

Files are addressed by an unguessable key rather than by id. A television has
no session and cannot send a bearer token for an image, so the logo URL has to
work on its own — but it must not be enumerable, or one customer could walk
another's brand assets by counting upward.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024

# SVG is deliberately absent. It can carry script, and while a browser will not
# run that inside an <img>, it will when the file is opened directly. Excluding
# it is cheaper than being sure about every way a URL might be visited.
ALLOWED = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


class AssetError(ValueError):
    """An upload we will not accept, with a reason a person can act on."""


@dataclass
class StoredAsset:
    key: str
    content_type: str
    size: int
    sha256: str


def storage_root() -> Path:
    root = Path(os.environ.get("SCOREBOARD_ASSET_DIR", "/data/assets"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def sniff(data: bytes) -> str | None:
    """Identify the file from its own bytes.

    The declared content type comes from the uploader and is not evidence. A
    .png header on a file called logo.svg is what actually decides.
    """
    for signature, content_type in MAGIC:
        if data.startswith(signature):
            return content_type
    # WebP is "RIFF....WEBP".
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate(data: bytes, declared: str = "") -> str:
    if not data:
        raise AssetError("The file is empty.")
    if len(data) > MAX_BYTES:
        # The caller reads only up to the ceiling, so the true size is unknown
        # here. Claiming one would produce "that file is 2048KB, the limit is
        # 2048KB", which reads as a contradiction.
        raise AssetError(
            f"That file is larger than the {MAX_BYTES // 1024}KB limit — "
            "a logo on a television never needs more."
        )
    actual = sniff(data)
    if actual is None:
        raise AssetError(
            "That is not a PNG, JPEG, WebP or GIF. SVG is not accepted because it "
            "can carry script."
        )
    if declared and declared.split(";")[0].strip() not in ALLOWED:
        raise AssetError(f"'{declared}' is not an accepted image type.")
    return actual


def save(org_id: int, data: bytes, declared: str = "") -> StoredAsset:
    content_type = validate(data, declared)
    key = secrets.token_urlsafe(24)
    digest = hashlib.sha256(data).hexdigest()

    # One directory per organization, so a mis-scoped read is a visible path
    # bug rather than a silent cross-tenant hit.
    folder = storage_root() / str(org_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{key}{ALLOWED[content_type]}").write_bytes(data)

    return StoredAsset(key=key, content_type=content_type, size=len(data), sha256=digest)


def path_for(org_id: int, key: str, content_type: str) -> Path:
    return storage_root() / str(org_id) / f"{key}{ALLOWED.get(content_type, '')}"


def delete(org_id: int, key: str, content_type: str) -> bool:
    target = path_for(org_id, key, content_type)
    if target.exists():
        target.unlink()
        return True
    return False
