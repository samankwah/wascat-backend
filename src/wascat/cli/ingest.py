"""Ingest commands.

``labels`` loads the observer's table - tag, cloud genus, oktas - onto records
that already exist. ``predictions`` loads a classifier's probability vectors
for the same frames. Neither creates a record and neither touches a
measurement: they add what a person saw and what a model said, next to what
the segmentation pipeline delivered.

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
from wascat.domains.ingest import labels as labels_service
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


TABLE_ARGUMENT = typer.Argument(
    ...,
    help="The table to load: CSV, TSV, or XLSX where openpyxl is installed.",
)
LABELS_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Report what would change without writing anything."
)
MODEL_OPTION = typer.Option(
    ..., "--model", help="Slug of the model these probabilities came from, e.g. 'allsky-cnn'."
)
MODEL_NAME_OPTION = typer.Option(
    None, "--model-name", help="Display name, used when the model is recorded for the first time."
)
MODEL_VERSION_OPTION = typer.Option(
    ..., "--model-version", help="The model's own version: a tag, a date or a commit."
)


@ingest_app.command("labels")
def labels(
    table: Path = TABLE_ARGUMENT,
    dry_run: bool = LABELS_DRY_RUN_OPTION,
) -> None:
    """Load the observer's cloud genus and okta count onto existing records.

    The table is three columns: the archive's tag number, the cloud type as a
    standard two-letter abbreviation, and the total cloud cover in oktas -
    "WAS-V11-F1892, SC, 07". A header row is skipped if present.
    """
    if not table.is_file():
        typer.secho(f"No such file: {table}", fg=typer.colors.RED)
        raise typer.Exit(1)
    asyncio.run(_labels(table=table, dry_run=dry_run))


async def _labels(*, table: Path, dry_run: bool) -> None:
    try:
        rows = labels_service.parse_labels(table)
    except (labels_service.LabelFormatError, ValueError) as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(1) from None

    try:
        async with get_sessionmaker()() as session:
            report = await labels_service.apply_labels(
                session,
                rows,
                dry_run=dry_run,
                # The archive's records all live in published releases, and
                # the observer's classification is exactly the kind of
                # curated field a release never claimed to pin. Measurements
                # and artifacts are untouched either way.
                allow_published=True,
            )
            if not dry_run:
                await session.commit()
    finally:
        await dispose_engine()

    _report(report.summary(), report.terms_created, report.unknown_tags, dry_run=dry_run)


@ingest_app.command("predictions")
def predictions(
    table: Path = TABLE_ARGUMENT,
    model: str = MODEL_OPTION,
    model_version: str = MODEL_VERSION_OPTION,
    model_name: str | None = MODEL_NAME_OPTION,
    dry_run: bool = LABELS_DRY_RUN_OPTION,
) -> None:
    """Load a classifier's probability vectors for frames the archive holds.

    Long form is one row per class - tag, cloud type, probability. Wide form is
    one column per class, with the codes in the header row. Either way the
    whole vector is expected: a frame's probabilities must sum to 1, because a
    truncated vector stored as a complete one would misreport the model.
    """
    if not table.is_file():
        typer.secho(f"No such file: {table}", fg=typer.colors.RED)
        raise typer.Exit(1)
    asyncio.run(
        _predictions(
            table=table,
            slug=model,
            version=model_version,
            name=model_name or model,
            dry_run=dry_run,
        )
    )


async def _predictions(*, table: Path, slug: str, version: str, name: str, dry_run: bool) -> None:
    try:
        triples = labels_service.parse_predictions(table)
    except (labels_service.LabelFormatError, ValueError) as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(1) from None

    try:
        async with get_sessionmaker()() as session:
            record = await labels_service.upsert_model(
                session, slug=slug, name=name, version=version
            )
            try:
                report = await labels_service.apply_predictions(
                    session, triples, model=record, dry_run=dry_run
                )
            except labels_service.LabelFormatError as error:
                await session.rollback()
                typer.secho(str(error), fg=typer.colors.RED)
                raise typer.Exit(1) from None
            if not dry_run:
                await session.commit()
    finally:
        await dispose_engine()

    typer.echo(f"Model {slug} version {version}.")
    _report(report.summary(), report.terms_created, report.unknown_tags, dry_run=dry_run)


def _report(
    summary: str, terms_created: list[str], unknown_tags: list[str], *, dry_run: bool
) -> None:
    """One shape of output for both loaders.

    Unknown tags are listed rather than counted, up to a point: a handful is a
    typo worth seeing, and a thousand means the table is keyed on something
    other than the archive's tag number, which the count alone would not say.
    """
    typer.secho(summary, fg=typer.colors.GREEN if not dry_run else typer.colors.BLUE)
    if terms_created:
        verb = "would be added to" if dry_run else "added to"
        typer.echo(f"Sky classes {verb} the vocabulary: {', '.join(terms_created)}")
    for tag in unknown_tags[:10]:
        typer.secho(f"  no record for {tag}", fg=typer.colors.YELLOW)
    if len(unknown_tags) > 10:
        typer.secho(f"  ... and {len(unknown_tags) - 10:,} more", fg=typer.colors.YELLOW)
    if dry_run:
        typer.echo("Dry run: nothing written.")
