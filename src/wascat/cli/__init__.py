"""The ``wascat`` command line.

Everything an operator does outside the dashboard lives here: applying
migrations, loading the archive, creating the first administrator, and
checking that the machine is actually set up.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

# Every entrypoint has to import the registry before touching the ORM:
# relationships reference each other by name, so a partial import fails at
# the first query rather than at start-up. The four entrypoints are this
# module, api/app.py, migrations/env.py and tests/conftest.py.
import wascat.models  # noqa: F401
from wascat.cli.db import db_app
from wascat.cli.doctor import doctor_command
from wascat.cli.ingest import ingest_app

app = typer.Typer(
    name="wascat",
    help="WASCAT backend operations.",
    no_args_is_help=True,
    add_completion=False,
)

app.add_typer(db_app, name="db", help="Migrations and seeding.")
app.add_typer(ingest_app, name="ingest", help="Load imagery into the archive.")
app.command("doctor", help="Check that this machine is ready to run the backend.")(doctor_command)

OUT_OPTION = typer.Option(Path("openapi.json"), "--out", "-o", help="Where to write the schema.")


@app.command("openapi")
def dump_openapi(out: Path = OUT_OPTION) -> None:
    """Write the OpenAPI schema.

    The frontend generates its TypeScript types from this file, so the two
    repositories share one contract without sharing a package.
    """
    # Imported here rather than at module load: building the FastAPI app
    # imports every router and model, which would slow down `wascat --help`
    # and every other command for no reason.
    from wascat.main import app as api  # noqa: PLC0415

    schema = api.openapi()
    out.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    typer.echo(f"Wrote {out} ({len(schema.get('paths', {}))} paths)")


__all__ = ["app"]
