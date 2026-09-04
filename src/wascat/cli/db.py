"""Database commands."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import typer

from wascat.core.config import get_settings
from wascat.core.db import dispose_engine, get_sessionmaker
from wascat.domains.ingest.legacy_import import import_catalogue
from wascat.storage.base import ObjectStore
from wascat.storage.local import LocalObjectStore
from wascat.storage.s3 import S3ObjectStore

db_app = typer.Typer(no_args_is_help=True)

REPO_ROOT = Path(__file__).resolve().parents[3]

CATALOG_OPTION = typer.Option(
    REPO_ROOT / "seed" / "catalog.generated.json",
    "--catalog",
    help="The generated catalogue to import.",
)
PROVENANCE_OPTION = typer.Option(
    REPO_ROOT / "seed" / "provenance.json",
    "--provenance",
    help="Per-sequence editorial provenance.",
)
FRAMES_OPTION = typer.Option(
    None,
    "--frames",
    help="Directory holding the seq-NNN frame folders. Omit to skip uploading.",
)
REPLACE_OPTION = typer.Option(False, "--replace", help="Delete the existing catalogue first.")
PUBLISH_OPTION = typer.Option(
    True, "--publish/--draft", help="Publish the release, or leave it a draft."
)


@db_app.command("upgrade")
def upgrade() -> None:
    """Apply migrations (``alembic upgrade head``)."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, check=False
    )
    raise typer.Exit(result.returncode)


@db_app.command("seed")
def seed(
    catalog: Path = CATALOG_OPTION,
    provenance: Path = PROVENANCE_OPTION,
    frames: Path | None = FRAMES_OPTION,
    replace: bool = REPLACE_OPTION,
    publish: bool = PUBLISH_OPTION,
) -> None:
    """Import the generated catalogue into PostgreSQL and object storage.

    Measurements are copied verbatim; nothing is recomputed. Every frame's
    SHA-256 is checked against the catalogue before upload, so imagery that has
    drifted from its record stops the import instead of entering the archive.
    """
    if not catalog.exists():
        typer.secho(f"No catalogue at {catalog}", fg=typer.colors.RED)
        raise typer.Exit(1)

    asyncio.run(_seed(catalog, provenance, frames, replace=replace, publish=publish))


async def _seed(
    catalog: Path,
    provenance: Path,
    frames: Path | None,
    *,
    replace: bool,
    publish: bool,
) -> None:
    settings = get_settings()
    store: ObjectStore | None = None
    s3: S3ObjectStore | None = None

    if frames is not None:
        if settings.storage_backend == "local":
            store = LocalObjectStore(
                settings.local_storage_root, public_base_url=settings.public_asset_base_url
            )
        else:
            s3 = S3ObjectStore.from_settings()
            await s3.connect()
            await s3.ensure_bucket()
            store = s3

    try:
        async with get_sessionmaker()() as session:
            report = await import_catalogue(
                session,
                catalog_path=catalog,
                provenance_path=provenance,
                frames_root=frames,
                store=store,
                publish=publish,
                replace=replace,
            )
            await session.commit()
    finally:
        if s3 is not None:
            await s3.close()
        await dispose_engine()

    typer.secho(report.summary(), fg=typer.colors.GREEN)
    for warning in report.warnings:
        typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)
