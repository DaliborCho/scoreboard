"""Loading and editing a customer's metric catalogue.

An organization that has never touched its metrics uses the set this product
shipped with. The first edit materializes that set as rows, and from then on it
is theirs. Nothing has to be migrated ahead of time, and an installation that
never wants custom metrics never grows the rows.

The additive/derived split is enforced here rather than trusted. A customer can
add either kind, but neither can escape the arithmetic: additive values are
stored and summed, derived ones carry a formula and are recomputed at whatever
level is being totalled.
"""
from __future__ import annotations

from dataclasses import dataclass

from scoreboard.domain.metrics import (
    ADDITIVE_ROLE,
    DEFAULT_CATALOGUE,
    DERIVED_ROLE,
    NUMERIC_KINDS,
    SYSTEM_ROLE,
    TEXT_ROLE,
    MetricCatalogue,
    MetricDef,
    Ratio,
)
from scoreboard.models import MetricField
from scoreboard.tenancy import TenantScope

ROLES = (ADDITIVE_ROLE, DERIVED_ROLE, TEXT_ROLE, SYSTEM_ROLE)
KINDS = ("number", "currency", "percent", "text", "system")


class CatalogueError(ValueError):
    """A field definition that would not produce an honest number."""


@dataclass
class FieldInput:
    key: str
    label: str
    short_label: str = ""
    kind: str = "number"
    role: str = ADDITIVE_ROLE
    numerator: str = ""
    denominator: str = ""
    scale: float = 1.0


# ---------------------------------------------------------------- reading
def _to_def(row: MetricField) -> MetricDef:
    formula = (
        Ratio(row.numerator, row.denominator, row.scale)
        if row.role == DERIVED_ROLE and row.numerator and row.denominator
        else None
    )
    return MetricDef(
        key=row.key,
        label=row.label,
        short_label=row.short_label or row.label[:6].upper(),
        kind=row.kind,
        role=row.role,
        formula=formula,
    )


def definition_from(field: FieldInput) -> MetricDef:
    """A definition from an unsaved input, for previewing before committing."""
    formula = (
        Ratio(field.numerator, field.denominator, float(field.scale))
        if field.role == DERIVED_ROLE and field.numerator and field.denominator
        else None
    )
    return MetricDef(
        key=field.key,
        label=field.label,
        short_label=(field.short_label or field.key[:6]).upper(),
        kind=field.kind,
        role=field.role,
        formula=formula,
    )


def rows_for(scope: TenantScope) -> list[MetricField]:
    return sorted(scope.all(MetricField), key=lambda r: (r.position, r.id))


def catalogue_for(scope: TenantScope) -> MetricCatalogue:
    """This organization's catalogue, or the shipped one if they have none."""
    rows = rows_for(scope)
    if not rows:
        return DEFAULT_CATALOGUE
    return MetricCatalogue([_to_def(row) for row in rows])


# ---------------------------------------------------------------- writing
def install_defaults(scope: TenantScope) -> int:
    """Materialize the shipped catalogue as this organization's own rows.

    Idempotent, and a no-op once anything exists. Called before the first edit
    rather than at sign-up, so an installation that never customizes its
    metrics never carries the rows.
    """
    if rows_for(scope):
        return 0

    for position, definition in enumerate(DEFAULT_CATALOGUE.definitions):
        formula = definition.formula
        scope.add(
            MetricField(
                key=definition.key,
                label=definition.label,
                short_label=definition.short_label,
                kind=definition.kind,
                role=definition.role,
                numerator=formula.numerator if formula else "",
                denominator=formula.denominator if formula else "",
                scale=formula.scale if formula else 1.0,
                position=position,
                is_builtin=True,
            )
        )
    scope.commit()
    return len(DEFAULT_CATALOGUE.definitions)


def validate(field: FieldInput, existing: list[MetricField], *, editing: str = "") -> None:
    """Everything that would make this field dishonest or unusable."""
    key = (field.key or "").strip()
    if not key.replace("_", "").isalnum() or not key[:1].isalpha():
        raise CatalogueError(
            "A key must start with a letter and contain only letters, digits "
            "and underscores — it appears in payloads and column headings."
        )
    if field.role not in ROLES:
        raise CatalogueError(f"Role must be one of: {', '.join(ROLES)}.")
    if field.kind not in KINDS:
        raise CatalogueError(f"Type must be one of: {', '.join(KINDS)}.")
    if not (field.label or "").strip():
        raise CatalogueError("A field needs a label; it is what appears on the board.")

    keys = {row.key for row in existing if row.key != editing}
    if key in keys:
        raise CatalogueError(f"'{key}' already exists in this organization.")

    if field.role == DERIVED_ROLE:
        known = keys | {key}
        for side, name in (("numerator", field.numerator), ("denominator", field.denominator)):
            if not name:
                raise CatalogueError(f"A calculated field needs a {side}.")
            if name not in known:
                raise CatalogueError(f"'{name}' is not a field in this organization.")
        if field.numerator == field.denominator:
            raise CatalogueError("Dividing a value by itself is always 1; that is not a metric.")
        try:
            scale = float(field.scale)
        except (TypeError, ValueError):
            raise CatalogueError("Scale must be a number.") from None
        if scale == 0:
            raise CatalogueError("A scale of zero would make every value zero.")
        if field.kind not in NUMERIC_KINDS:
            raise CatalogueError("A calculated field has to be a number, currency or percent.")
    elif field.role == ADDITIVE_ROLE and field.kind not in NUMERIC_KINDS:
        raise CatalogueError("A stored value has to be a number, currency or percent.")


def create(scope: TenantScope, field: FieldInput) -> MetricField:
    install_defaults(scope)
    existing = rows_for(scope)
    validate(field, existing)

    row = scope.add(
        MetricField(
            key=field.key.strip(),
            label=field.label.strip(),
            short_label=(field.short_label or field.label[:6]).strip().upper()[:16],
            kind=field.kind,
            role=field.role,
            numerator=field.numerator if field.role == DERIVED_ROLE else "",
            denominator=field.denominator if field.role == DERIVED_ROLE else "",
            scale=float(field.scale) if field.role == DERIVED_ROLE else 1.0,
            position=(existing[-1].position + 1) if existing else 0,
            is_builtin=False,
        )
    )
    scope.flush()
    _reject_cycles(scope)
    scope.commit()
    return row


def update(scope: TenantScope, row: MetricField, field: FieldInput) -> MetricField:
    install_defaults(scope)
    validate(field, rows_for(scope), editing=row.key)

    if row.is_builtin and field.role != row.role:
        # Stored figures and saved screens already assume what this field is.
        raise CatalogueError(
            f"'{row.key}' ships with the product; its label can change but not "
            "whether it is stored or calculated."
        )
    if row.is_builtin and field.key != row.key:
        raise CatalogueError(f"'{row.key}' ships with the product; its key cannot change.")

    row.key = field.key.strip()
    row.label = field.label.strip()
    row.short_label = (field.short_label or field.label[:6]).strip().upper()[:16]
    row.kind = field.kind
    row.role = field.role
    row.numerator = field.numerator if field.role == DERIVED_ROLE else ""
    row.denominator = field.denominator if field.role == DERIVED_ROLE else ""
    row.scale = float(field.scale) if field.role == DERIVED_ROLE else 1.0

    scope.flush()
    _reject_cycles(scope)
    scope.commit()
    return row


def delete(scope: TenantScope, row: MetricField) -> None:
    if row.is_builtin:
        raise CatalogueError(
            f"'{row.key}' ships with the product. Stored figures and saved "
            "screens refer to it, so it can be relabelled but not removed."
        )

    dependents = [
        other.key for other in rows_for(scope)
        if other.id != row.id and row.key in (other.numerator, other.denominator)
    ]
    if dependents:
        raise CatalogueError(
            f"'{row.key}' is used by {', '.join(dependents)}. Remove those first."
        )

    scope.delete(row)
    scope.commit()


def _reject_cycles(scope: TenantScope) -> None:
    """Refuse a catalogue that cannot be evaluated.

    Evaluation walks the fields in order, so a formula naming something defined
    after it would silently read zero rather than fail. Catching that here means
    a board never shows a confidently wrong number.
    """
    seen: set[str] = set()
    for row in rows_for(scope):
        if row.role == DERIVED_ROLE:
            for name in (row.numerator, row.denominator):
                if name not in seen:
                    raise CatalogueError(
                        f"'{row.label}' uses '{name}', which is not available "
                        "before it. Move it earlier, or use a stored value."
                    )
        seen.add(row.key)


def reorder(scope: TenantScope, keys: list[str]) -> None:
    """Set evaluation order. A formula may only use what comes before it."""
    install_defaults(scope)
    by_key = {row.key: row for row in rows_for(scope)}
    unknown = [key for key in keys if key not in by_key]
    if unknown:
        raise CatalogueError(f"Unknown field: {', '.join(unknown)}.")

    ordered = keys + [key for key in by_key if key not in keys]
    for position, key in enumerate(ordered):
        by_key[key].position = position

    scope.flush()
    _reject_cycles(scope)
    scope.commit()
