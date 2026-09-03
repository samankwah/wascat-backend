"""A single command that says whether this machine can run the backend.

Every check here corresponds to something that has actually gone wrong during
setup: a stopped Docker daemon whose error message blames the socket, a
database that exists but has never been migrated, a bucket nobody created.
"""

from __future__ import annotations

import asyncio
import sys

import typer
from sqlalchemy import text

from wascat.core.config import get_settings
from wascat.core.db import create_engine, dispose_engine

OK = "  ok    "
BAD = "  FAIL  "
WARN = "  warn  "


def doctor_command() -> None:
    failures = asyncio.run(_run_checks())
    if failures:
        typer.secho(f"\n{failures} check(s) failed.", fg=typer.colors.RED)
        raise typer.Exit(1)
    typer.secho("\nEverything looks ready.", fg=typer.colors.GREEN)


async def _run_checks() -> int:
    settings = get_settings()
    failures = 0

    typer.echo("Python")
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] == (3, 13):
        typer.echo(f"{OK}{version}")
    else:
        typer.echo(f"{WARN}{version} (this project pins 3.13)")

    typer.echo("\nPostgreSQL")
    engine = create_engine()
    try:
        async with engine.connect() as conn:
            server = (await conn.execute(text("SHOW server_version"))).scalar_one()
            typer.echo(f"{OK}connected, server {server}")

            stamped = (
                await conn.execute(
                    text(
                        "SELECT version_num FROM alembic_version"
                        " WHERE EXISTS (SELECT 1 FROM information_schema.tables"
                        "               WHERE table_name = 'alembic_version')"
                    )
                )
            ).scalar_one_or_none()
            if stamped:
                typer.echo(f"{OK}migrations applied ({stamped})")
            else:
                typer.echo(f"{BAD}no migrations applied - run: wascat db upgrade")
                failures += 1

            for extension in ("pg_trgm", "citext"):
                present = (
                    await conn.execute(
                        text("SELECT 1 FROM pg_extension WHERE extname = :name"),
                        {"name": extension},
                    )
                ).scalar_one_or_none()
                typer.echo(f"{OK if present else BAD}extension {extension}")
                failures += 0 if present else 1

            records = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM image_records"
                        " WHERE EXISTS (SELECT 1 FROM information_schema.tables"
                        "               WHERE table_name = 'image_records')"
                    )
                )
            ).scalar_one_or_none()
            if records:
                typer.echo(f"{OK}{records:,} image records")
            else:
                typer.echo(f"{WARN}no image records - run: wascat db seed --frames <dir>")
    except Exception as exc:
        typer.echo(f"{BAD}{type(exc).__name__}: {exc}")
        typer.echo("        is the database running?  .\tasks.ps1 up")
        failures += 1
    finally:
        await engine.dispose()
        await dispose_engine()

    typer.echo("\nObject storage")
    if settings.storage_backend == "local":
        typer.echo(f"{OK}local filesystem at {settings.local_storage_root}")
    else:
        # Only needed on this branch, and it pulls in aioboto3.
        from wascat.storage.s3 import S3ObjectStore  # noqa: PLC0415

        store = S3ObjectStore.from_settings()
        try:
            await store.connect()
            await store.client.head_bucket(Bucket=store.bucket)
            typer.echo(f"{OK}bucket {store.bucket} at {settings.s3_endpoint_url}")
        except Exception as exc:
            typer.echo(f"{BAD}{type(exc).__name__}: {exc}")
            typer.echo("        is MinIO running?  .\tasks.ps1 up")
            failures += 1
        finally:
            await store.close()

    typer.echo("\nConfiguration")
    typer.echo(f"{OK}environment: {settings.environment}")
    if settings.public_base_url:
        typer.echo(f"{OK}public base URL: {settings.public_base_url}")
    else:
        typer.echo(f"{WARN}WASCAT_PUBLIC_BASE_URL unset - links.self will echo this host")
    typer.echo(f"{OK}asset base URL: {settings.public_asset_base_url or '(relative)'}")

    return failures
