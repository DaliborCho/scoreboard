"""Application composition root.

The one place where routers, dependencies and startup work are wired. Modules
do not reach into each other at import time; if something needs to exist, it
is assembled here.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from scoreboard import __version__
from scoreboard.api import routes
from scoreboard.config import settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings().is_dev:
        # Development convenience only. Production schema changes go through
        # Alembic so they are reviewable and reversible.
        from scoreboard.db import create_all

        create_all()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Scoreboard API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
    )
    app.include_router(routes.system)
    app.include_router(routes.ingest)
    app.include_router(routes.display)
    return app


app = create_app()
