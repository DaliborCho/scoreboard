"""Editing what a company measures.

The catalogue used to be a tuple in a module, which meant a customer counting
something we had not guessed needed a release. These routes make it a form.

Everything here goes through `services.catalogue`, which enforces the one rule
the product rests on: a stored value is summed, a calculated one carries its
formula and is recomputed at whatever level is being totalled. A customer can
add either kind. Neither can escape the arithmetic.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from scoreboard.api.deps import auth_context, require_role, user_scope
from scoreboard.domain.formula import FUNCTIONS
from scoreboard.domain.metrics import ADDITIVE_ROLE, DERIVED_ROLE
from scoreboard.models import MetricField, Role
from scoreboard.services import audit
from scoreboard.services import catalogue as cat
from scoreboard.services.auth import AuthContext
from scoreboard.tenancy import TenantScope

router = APIRouter(prefix="/api/v1/fields", tags=["fields"])


class FieldRequest(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    short_label: str = Field(default="", max_length=16)
    kind: str = "number"
    role: str = ADDITIVE_ROLE
    numerator: str = ""
    denominator: str = ""
    scale: float = 1.0
    expression: str = Field(default="", max_length=500)

    def as_input(self) -> cat.FieldInput:
        return cat.FieldInput(**self.model_dump())


class ReorderRequest(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=200)


def _json(row: MetricField) -> dict:
    return {
        "id": row.id,
        "key": row.key,
        "label": row.label,
        "short_label": row.short_label,
        "kind": row.kind,
        "role": row.role,
        "numerator": row.numerator,
        "denominator": row.denominator,
        "scale": row.scale,
        "expression": row.expression,
        "position": row.position,
        "is_builtin": row.is_builtin,
    }


def _refuse(error: cat.CatalogueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(error))


@router.get("")
def list_fields(
    _: AuthContext = Depends(auth_context),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    metrics = cat.catalogue_for(scope)
    rows = cat.rows_for(scope)
    return {
        # Before the first edit an organization has no rows of its own, and the
        # shipped catalogue is shown instead. Saying so keeps the empty list
        # from reading as "you have no metrics".
        "using_defaults": not rows,
        "fields": [_json(row) for row in rows] or [
            {
                "id": None, "key": m.key, "label": m.label,
                "short_label": m.short_label, "kind": m.kind, "role": m.role,
                "numerator": m.formula.shorthand[0] if m.formula and m.formula.shorthand else "",
                "denominator": (
                    m.formula.shorthand[1] if m.formula and m.formula.shorthand else ""),
                "scale": m.formula.shorthand[2] if m.formula and m.formula.shorthand else 1.0,
                "expression": (
                    "" if not m.formula or m.formula.shorthand else m.formula.text),
                "position": index, "is_builtin": True,
            }
            for index, m in enumerate(metrics.definitions)
        ],
        "roles": list(cat.ROLES),
        "kinds": list(cat.KINDS),
        "additive": list(metrics.additive),
        # What a formula may name, and what it may call. Sent rather than
        # hard-coded in the page, so the editor cannot drift from the evaluator.
        "fields_available": [m.key for m in metrics.definitions
                             if m.kind in ("number", "currency", "percent")],
        "functions": sorted(FUNCTIONS),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_field(
    payload: FieldRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    try:
        row = cat.create(scope, payload.as_input())
    except cat.CatalogueError as exc:
        raise _refuse(exc) from exc

    audit.record(
        scope, "field.create", actor_user_id=context.user.id,
        actor_label=context.user.email, target=row.key,
        detail={"role": row.role, "kind": row.kind},
    )
    scope.commit()
    return _json(row)


@router.patch("/{field_id}")
def update_field(
    field_id: int,
    payload: FieldRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    cat.install_defaults(scope)
    row = scope.get(MetricField, field_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Field not found.")
    try:
        row = cat.update(scope, row, payload.as_input())
    except cat.CatalogueError as exc:
        raise _refuse(exc) from exc

    audit.record(
        scope, "field.update", actor_user_id=context.user.id,
        actor_label=context.user.email, target=row.key,
    )
    scope.commit()
    return _json(row)


@router.post("/{field_id}/delete")
def delete_field(
    field_id: int,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    row = scope.get(MetricField, field_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Field not found.")

    key = row.key
    try:
        cat.delete(scope, row)
    except cat.CatalogueError as exc:
        raise _refuse(exc) from exc

    audit.record(
        scope, "field.delete", actor_user_id=context.user.id,
        actor_label=context.user.email, target=key,
    )
    scope.commit()
    return {"ok": True}


@router.post("/reorder")
def reorder_fields(
    payload: ReorderRequest,
    context: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Set evaluation order.

    A calculated field may only use values available before it, so order is
    meaning rather than presentation.
    """
    try:
        cat.reorder(scope, payload.keys)
    except cat.CatalogueError as exc:
        raise _refuse(exc) from exc

    audit.record(
        scope, "field.reorder", actor_user_id=context.user.id,
        actor_label=context.user.email, target=f"{len(payload.keys)} fields",
    )
    scope.commit()
    return {"ok": True, "fields": [_json(row) for row in cat.rows_for(scope)]}


@router.post("/preview")
def preview_field(
    payload: FieldRequest,
    _: AuthContext = Depends(require_role(Role.org_admin)),
    scope: TenantScope = Depends(user_scope),
) -> dict:
    """Check a definition, and show what it would produce on real figures.

    Read-only. Nothing is saved, so someone can see the number before deciding
    whether the formula says what they meant.
    """

    from scoreboard.connectors.base import Period
    from scoreboard.domain.metrics import MetricCatalogue
    from scoreboard.services.board import rows_for_period

    try:
        cat.validate(payload.as_input(), cat.rows_for(scope), editing=payload.key)
    except cat.CatalogueError as exc:
        return {"ok": False, "error": str(exc)}

    if payload.role != DERIVED_ROLE:
        return {"ok": True, "sample": None,
                "note": "A stored value shows whatever the source sends."}

    current = cat.catalogue_for(scope)
    # Appended last, so it can use anything already defined and nothing can
    # depend on it. That matches how it would evaluate once saved.
    trial = MetricCatalogue(
        [*current.definitions, cat.definition_from(payload.as_input())]
    )

    # Period.current_month() reads UTC on its own, the same clock a refresh
    # uses, so a preview and the board it predicts never disagree by a day.
    rows = rows_for_period(scope, Period.current_month(), metrics=trial)
    if not rows:
        return {"ok": True, "sample": None, "note": "No figures loaded yet."}

    office = trial.roll_up(rows)
    return {
        "ok": True,
        "note": f"Across {len(rows)} people, currently.",
        "sample": {
            "office": office.get(payload.key),
            "people": [
                {"name": row["rep_name"], "value": row.get(payload.key)}
                for row in rows[:5]
            ],
        },
    }
