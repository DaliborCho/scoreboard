"""The tenant isolation chokepoint.

Every read and write of customer data goes through `TenantScope`. It is not a
convenience wrapper — it is the single place where "which organization is this"
is applied, so isolation is a property of the architecture rather than of each
developer remembering a WHERE clause.

Code that needs a raw session for cross-tenant work (migrations, admin tools)
must reach for it explicitly and obviously.
"""
from __future__ import annotations

from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from scoreboard.db import Base

T = TypeVar("T", bound=Base)


class TenantScope:
    """A session bound to one organization."""

    def __init__(self, session: Session, org_id: int):
        if not org_id:
            raise ValueError("TenantScope requires an organization id.")
        self.session = session
        self.org_id = org_id

    # ------------------------------------------------------------ reads
    def select(self, model: type[T]) -> Select:
        """A SELECT already filtered to this organization."""
        self._require_scoped(model)
        return select(model).where(model.org_id == self.org_id)

    def all(self, model: type[T]) -> list[T]:
        return list(self.session.scalars(self.select(model)).all())

    def get(self, model: type[T], obj_id: int) -> T | None:
        """Fetch by primary key, but only within this organization.

        Using `Session.get` directly would happily return another customer's
        row for a guessed id. This will not.
        """
        self._require_scoped(model)
        return self.session.scalars(
            self.select(model).where(model.id == obj_id)
        ).first()

    def one_by(self, model: type[T], **filters) -> T | None:
        statement = self.select(model)
        for key, value in filters.items():
            statement = statement.where(getattr(model, key) == value)
        return self.session.scalars(statement).first()

    # ------------------------------------------------------------ writes
    def add(self, obj: T) -> T:
        """Insert, stamping the organization rather than trusting the caller."""
        self._require_scoped(type(obj))
        obj.org_id = self.org_id
        self.session.add(obj)
        return obj

    def delete(self, obj: T) -> None:
        if getattr(obj, "org_id", None) != self.org_id:
            raise PermissionError("Refusing to delete a row belonging to another organization.")
        self.session.delete(obj)

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    # ------------------------------------------------------------ guard
    @staticmethod
    def _require_scoped(model: type) -> None:
        if not hasattr(model, "org_id"):
            raise TypeError(
                f"{model.__name__} has no org_id and cannot be accessed through a "
                "TenantScope. Global tables must be queried deliberately."
            )
