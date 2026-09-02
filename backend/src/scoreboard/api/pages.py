"""The two pages a human actually opens.

Served from the API rather than a separate front-end host so a customer has
one origin to allow, one certificate, and no CORS to configure. The pages are
static; everything they show comes from the JSON API.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

router = APIRouter(include_in_schema=False)

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def _page(name: str) -> FileResponse:
    return FileResponse(
        WEB_ROOT / name,
        media_type="text/html",
        # The shell is static; the data behind it is not. Letting a television
        # cache the page for a day would strand it on an old renderer.
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/tv/{token}", response_class=HTMLResponse)
def television(token: str) -> FileResponse:
    """The board itself.

    The token is not checked here. The page reads it from its own URL and
    calls the API, which is the only place that decides whether it is valid,
    so there is one answer to that question rather than two.
    """
    return _page("tv.html")


@router.get("/console", response_class=HTMLResponse)
def console_page() -> FileResponse:
    return _page("console.html")


@router.get("/preview/screen/{screen_id}", response_class=HTMLResponse)
def preview_screen_page(screen_id: int) -> FileResponse:
    """Preview a saved screen using the television's own renderer.

    Serving the same file rather than a second implementation is the point: a
    preview built separately drifts, and then it is showing something nobody
    will ever see on a wall.
    """
    return _page("tv.html")


@router.get("/preview/live", response_class=HTMLResponse)
def preview_live_page() -> FileResponse:
    """Preview an unsaved combination, for the theme and screen editors."""
    return _page("tv.html")


@router.get("/architecture", response_class=HTMLResponse)
def architecture_page() -> FileResponse:
    """The system map: how the parts fit together and why.

    Served beside the thing it documents, so it is read where it is checked
    against rather than in a folder somebody has to be told about.
    """
    return _page("architecture.html")


@router.get("/admin", response_class=HTMLResponse)
def admin_page() -> FileResponse:
    """The platform operator's panel.

    Served to anyone who asks; the page is a shell and every call behind it
    answers 404 without the platform flag, so there is nothing here to guard.
    """
    return _page("admin.html")


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Scoreboard</title>"
        "<body style='font-family:system-ui;padding:3rem;max-width:40rem'>"
        "<h1>Scoreboard</h1>"
        "<p><a href='/console'>Company console</a> &mdash; teams, themes, screens and displays.</p>"
        "<p><a href='/admin'>Platform admin</a> &mdash; add and manage companies.</p>"
        "<p><a href='/architecture'>System map</a> &mdash; how it all fits together.</p>"
        "<p><a href='/docs'>API reference</a></p>"
        "<p style='color:#666'>A television opens its own link: <code>/tv/&lt;token&gt;</code></p>"
    )
