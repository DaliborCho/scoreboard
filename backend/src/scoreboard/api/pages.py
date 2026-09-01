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


@router.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Scoreboard</title>"
        "<body style='font-family:system-ui;padding:3rem;max-width:40rem'>"
        "<h1>Scoreboard</h1>"
        "<p><a href='/console'>Console</a> &mdash; sign in to manage teams, themes and screens.</p>"
        "<p><a href='/docs'>API reference</a></p>"
        "<p style='color:#666'>A television opens its own link: <code>/tv/&lt;token&gt;</code></p>"
    )
