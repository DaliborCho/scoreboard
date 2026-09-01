"""Application composition root.

The one place where routers, dependencies and startup work are wired. Modules
do not reach into each other at import time; if something needs to exist, it
is assembled here.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from scoreboard import __version__
from scoreboard.api import console, routes


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup checks only.

    The schema is owned by Alembic in every environment, including
    development. Creating tables from the models at boot would let the two
    drift apart silently and would make the first production migration a
    guess.
    """
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
    app.include_router(console.router)
    return app


app = create_app()
