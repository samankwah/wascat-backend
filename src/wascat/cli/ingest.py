"""Ingest commands.

``backfill`` is the one that matters today: it adds the unsegmented frames the
TypeScript pipeline left out. Those need no measurement - a frame with no mask
has no cloud cover - so the backfill only hashes, probes dimensions, uploads
and inserts. That keeps the greyscale-parity question entirely out of the way
of growing the archive.
"""

from __future__ import annotations

import typer

ingest_app = typer.Typer(no_args_is_help=True)


@ingest_app.command("backfill")
def backfill() -> None:
    """Add the delivered frames that are not yet in the archive."""
    typer.secho(
        "Not implemented yet. The legacy import (`wascat db seed`) covers the "
        "published catalogue; the backfill lands with the ingest pipeline.",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(1)
