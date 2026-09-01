"""Engine, session factory and the declarative base."""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from scoreboard.config import settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal = None


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(settings().database_url, pool_pre_ping=True, future=True)
    return _engine


def session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=engine(), autoflush=False, expire_on_commit=False)
    return _SessionLocal


def get_session() -> Iterator[Session]:
    with session_factory()() as session:
        yield session


def create_all() -> None:
    """Development bootstrap. Production uses Alembic migrations."""
    from scoreboard import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine())
