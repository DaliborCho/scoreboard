"""Uploading and serving brand images.

Serving is the interesting half. A television has no session, so the image URL
has to work unauthenticated — which means the URL itself is the credential and
has to be unguessable, and the response has to be locked down enough that a
crafted upload cannot do anything if someone opens it directly.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from scoreboard.api.deps import require_role, user_scope
from scoreboard.db import get_session
from scoreboard.models import Asset, Role
from scoreboard.services import assets as store
from scoreboard.services import audit
from scoreboard.services.auth import AuthContext
from scoreboard.tenancy import TenantScope

router = APIRouter(tags=["assets"])


def _asset_json(asset: Asset) -> dict:
    return {
        "id": asset.id,
        "url": f"/assets/{asset.public_key}",
        "filename": asset.filename,
        "content_type": asset.content_type,
        "size_bytes": asset.size_bytes,
        "created_at": asset.created_at.isoformat(),
    }


@router.get("/api/v1/assets")
def list_assets(
    _: AuthContext = Depends(require_role(Role.team_lead)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    return {
        "assets": [_asset_json(a) for a in scope.all(Asset)],
        "max_bytes": store.MAX_BYTES,
        "accepted": sorted(store.ALLOWED),
    }


@router.post("/api/v1/assets", status_code=status.HTTP_201_CREATED)
async def upload_asset(
    file: UploadFile = File(...),
    context: AuthContext = Depends(require_role(Role.team_lead)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    # Read with a hard ceiling rather than trusting the declared length, so a
    # lying Content-Length cannot pull an arbitrary amount into memory.
    data = await file.read(store.MAX_BYTES + 1)
    try:
        stored = store.save(scope.org_id, data, file.content_type or "")
    except store.AssetError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    asset = scope.add(
        Asset(
            public_key=stored.key,
            filename=(file.filename or "")[:255],
            content_type=stored.content_type,
            size_bytes=stored.size,
            sha256=stored.sha256,
            uploaded_by=context.user.id,
        )
    )
    scope.flush()
    audit.record(
        scope, "asset.upload", actor_user_id=context.user.id,
        actor_label=context.user.email, target=asset.filename,
        detail={"size_bytes": stored.size, "content_type": stored.content_type},
    )
    scope.commit()
    return _asset_json(asset)


@router.post("/api/v1/assets/{asset_id}/delete")
def delete_asset(
    asset_id: int,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    asset = scope.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="Asset not found.")

    store.delete(scope.org_id, asset.public_key, asset.content_type)
    audit.record(
        scope, "asset.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=asset.filename,
    )
    scope.delete(asset)
    scope.commit()
    # A theme still pointing at this file will fall back to no logo rather
    # than breaking the board.
    return {"ok": True}


@router.get("/assets/{public_key}", include_in_schema=False)
def serve_asset(public_key: str, session: Session = Depends(get_session)):
    """Serve an image by its unguessable key.

    Deliberately not tenant-scoped: the key identifies the file, and there is
    no session on a television to scope it by. The lookup is by the random key
    alone, so no id can be substituted for another organization's.
    """
    asset = session.scalars(select(Asset).where(Asset.public_key == public_key)).first()
    if asset is None:
        raise HTTPException(status_code=404, detail="Not found.")

    path = store.path_for(asset.org_id, asset.public_key, asset.content_type)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not found.")

    return FileResponse(
        path,
        media_type=asset.content_type,
        headers={
            # Content is immutable — a new upload gets a new key — so it can be
            # cached hard. The rest stops a crafted image from behaving as
            # anything other than an image if the URL is opened directly.
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "Content-Disposition": "inline",
        },
    )
