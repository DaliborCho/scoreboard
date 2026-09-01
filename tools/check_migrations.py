"""Fail if the models have drifted away from the migrations.

Drift is invisible in development, where the running container was migrated by
hand long ago, and shows up as a broken deploy. This asks Alembic the same
question `--autogenerate` asks — "what would you write?" — and fails when the
answer is anything at all.

    DATABASE_URL=... python tools/check_migrations.py

Expects the database to already be at head.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND / "src"))


def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set.")
        return 2

    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy import create_engine

    from scoreboard import models  # noqa: F401  registers every mapper
    from scoreboard.db import Base

    engine = create_engine(url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    if not differences:
        print("Models and migrations agree.")
        return 0

    print("Models have drifted from the migrations:\n")
    for difference in differences:
        print(f"  {difference}")
    print(
        "\nRun:  docker compose run --rm api alembic revision --autogenerate -m '<what changed>'"
        "\nthen review the generated file and commit it."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
