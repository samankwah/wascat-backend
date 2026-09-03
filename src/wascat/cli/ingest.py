"""Ingest commands.

``backfill`` adds the delivered frames the original catalogue left out. The
TypeScript pipeline sampled 100 unsegmented frames per sequence because
keeping all of them would have added ~360 MB to a git repository - a
constraint that belonged to the repository rather than to the archive.

These frames need no measurement: with no mask there is no cloud cover, so
the backfill only hashes, probes and stores. The published measurements are
untouched by construction.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from wascat.core.db import dispose_engine, get_sessionmaker
from wascat.domains.ingest import backfill as backfill_service
from wascat.storage.factory import close_store, get_store

ingest_app = typer.Typer(no_args_is_help=True)

SOURCE_OPTION = typer.Option(
    ...,
    "--source",
    help="Directory of delivered source frames (the corner_mask folder).",
)
LIMIT_OPTION = typer.Option(
    None, "--limit", help="Stop after this many new frames. Useful for a trial run."
)
DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Report what would be added without writing anything."
)
YES_OPTION = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt.")
INTO_OPTION = typer.Option(
    None,
    "--into-version",
    help=(
        "Put the frames in a draft release of this version, creating it per "
        "collection if needed. Required when the current release is published, "
        "since a published release is immutable."
    ),
)


@ingest_app.command("backfill")
def backfill(
    source: Path = SOURCE_OPTION,
    limit: int | None = LIMIT_OPTION,
    dry_run: bool = DRY_RUN_OPTION,
    yes: bool = YES_OPTION,
    into_version: str | None = INTO_OPTION,
) -> None:
    """Ingest every delivered frame the archive does not already hold."""
    if not source.is_dir():
        typer.secho(f"No such directory: {source}", fg=typer.colors.RED)
        raise typer.Exit(1)

    asyncio.run(
        _backfill(
            source=source,
            limit=limit,
            dry_run=dry_run,
            assume_yes=yes,
            into_version=into_version,
        )
    )


async def _backfill(
    *,
    source: Path,
    limit: int | None,
    dry_run: bool,
    assume_yes: bool,
    into_version: str | None,
) -> None:
    store = await get_store()
    try:
        async with get_sessionmaker()() as session:
            before = await backfill_service.archive_totals(session)
            typer.echo(
                f"Archive holds {before['records']:,} records "
                f"({before['segmented']:,} segmented, {before['unsegmented']:,} not)."
            )

            preview = await backfill_service.backfill(
                session,
                store,
                source=source,
                limit=limit,
                dry_run=True,
                into_version=into_version,
            )
            typer.echo(
                f"{preview.scanned:,} delivered frames; "
                f"{preview.already_present:,} already held; "
                f"{preview.added:,} to add."
            )
            for note in preview.skipped[:5]:
                typer.secho(f"  skipping {note}", fg=typer.colors.YELLOW)

            if dry_run or preview.added == 0:
                typer.echo("Nothing written.")
                return

            if not assume_yes and not typer.confirm(
                f"Ingest {preview.added:,} frames?", default=False
            ):
                typer.echo("Cancelled.")
                return

            report = await backfill_service.backfill(
                session, store, source=source, limit=limit, into_version=into_version
            )
            await session.commit()

            after = await backfill_service.archive_totals(session)

        typer.secho(report.summary(), fg=typer.colors.GREEN)
        for note in report.skipped[:10]:
            typer.secho(f"  skipped {note}", fg=typer.colors.YELLOW)

        typer.echo(
            f"Archive now holds {after['records']:,} records "
            f"({after['segmented']:,} segmented, {after['unsegmented']:,} not) "
            f"and {after['artifacts']:,} artifacts."
        )
        # The measurements are the reason this is safe to run.
        if after["segmented"] != before["segmented"]:
            typer.secho(
                "WARNING: the segmented count changed. A backfill adds only "
                "unsegmented frames and must never touch a measurement.",
                fg=typer.colors.RED,
            )
    finally:
        await close_store()
        await dispose_engine()
