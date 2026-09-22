"""clear the invented site names and sky classes already in the database

Reverting seed/provenance.json fixes what a *future* import writes. It does
nothing to a database that has already been seeded: `location_name` is a
column, and every deployed environment is carrying the eleven invented city
names right now, serving them as the public collection titles. Without this,
correcting them means `wascat db seed --replace`, which clears the catalogue
and takes every curator edit with it.

So the values are cleared here, precisely and by name. Each row is matched
against the exact string e31006a wrote, with its coordinates, so a real site
name supplied since - which is the entire point of the provenance file - is
left alone. A blanket `SET location_name = NULL` would have been shorter and
would have thrown away the first genuine answer anyone gave.

Sky class is cleared wholesale instead, because there is no comparable risk:
5e3c06b set it on every record from a per-sequence value, and until the
dashboard gained a control for it there was no other way for one to be set.

This revision originally sat between c1a4e9f2b6d3 and 4f2b8c7d1e05, which
made it unreachable and is why it has moved. Production had already deployed
4f2b8c7d1e05, so it was stamped at what was still the head; inserting a new
revision *behind* that point left `alembic upgrade head` with nothing to do,
and it ran to completion having skipped this entirely. A migration only ever
runs on a database that has not yet passed the revision it is chained to, so
a cleanup written after the fact has to be chained to the head, never spliced
into history. It now follows 4f2b8c7d1e05.

That reordering is also why this deletes the nine invented SKY_CLASS terms
rather than leaving them to 4f2b8c7d1e05. That migration removes a term only
when no record references it - the guard that stops a migration silently
unclassifying records - and on any database seeded before the revert every
record still carried one when it ran, so it removed nothing. Clearing the
records here is what finally leaves those terms unreferenced, so this has to
be what removes them. On a database where 4f2b8c7d1e05 already did the work,
the delete matches nothing and costs a statement.

All three statements lift the published-release guard. Every record in the
archive sits in a published release, and what a release pins is its files and
its measurements; neither is touched here.

Revision ID: d3b1c7a49f20
Revises: 4f2b8c7d1e05
Created: 2026-09-22 19:40
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3b1c7a49f20"
down_revision: str | None = "4f2b8c7d1e05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Exactly what e31006a wrote, slug by slug: the site name and the public city
#: coordinates that went with it. A row is cleared only when it still matches
#: all three, so an edit made since survives untouched.
INVENTED: tuple[tuple[str, str, str, str], ...] = (
    ("seq-001", "Kumasi, Ghana", "6.6885", "-1.6244"),
    ("seq-002", "Accra, Ghana", "5.6037", "-0.1870"),
    ("seq-003", "Tamale, Ghana", "9.4034", "-0.8393"),
    ("seq-004", "Ouagadougou, Burkina Faso", "12.3714", "-1.5197"),
    ("seq-005", "Lagos, Nigeria", "6.5244", "3.3792"),
    ("seq-006", "Abuja, Nigeria", "9.0765", "7.3986"),
    ("seq-007", "Ibadan, Nigeria", "7.3775", "3.9470"),
    ("seq-008", "Bobo-Dioulasso, Burkina Faso", "11.1771", "-4.2979"),
    ("seq-009", "Takoradi, Ghana", "4.8845", "-1.7554"),
    ("seq-010", "Kano, Nigeria", "12.0022", "8.5920"),
    ("seq-011", "Koudougou, Burkina Faso", "12.2530", "-2.3620"),
)


#: The nine terms c1a4e9f2b6d3 seeded, which 4f2b8c7d1e05 could not remove
#: while records still pointed at them.
INVENTED_SKY_CLASSES: tuple[str, ...] = (
    "Clear sky",
    "Cirrus",
    "Cirrocumulus / Altocumulus",
    "Cumulus",
    "Stratocumulus",
    "Stratus / Nimbostratus",
    "Cumulonimbus",
    "Overcast",
    "Haze / Dust",
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL wascat.allow_published_writes = 'on'"))

    bind.execute(
        sa.text(
            """
            UPDATE collections
               SET location_name = NULL, latitude = NULL, longitude = NULL
             WHERE slug = :slug
               AND location_name = :site
               AND latitude = CAST(:latitude AS numeric)
               AND longitude = CAST(:longitude AS numeric)
            """
        ),
        [
            {"slug": slug, "site": site, "latitude": latitude, "longitude": longitude}
            for slug, site, latitude, longitude in INVENTED
        ],
    )

    bind.execute(
        sa.text(
            "UPDATE image_records SET sky_class_id = NULL, sky_class_label = NULL "
            "WHERE sky_class_id IS NOT NULL OR sky_class_label IS NOT NULL"
        )
    )

    # Now that nothing references them, the terms 4f2b8c7d1e05 had to leave in
    # place can go. Named explicitly, so a curator's own SKY_CLASS term - added
    # since, and not one of these nine - is never caught by this.
    bind.execute(
        sa.text(
            """
            DELETE FROM vocabulary_aliases
             WHERE term_id IN (
                 SELECT id FROM vocabulary_terms
                  WHERE kind = 'SKY_CLASS' AND label = ANY(:labels)
             )
            """
        ),
        {"labels": list(INVENTED_SKY_CLASSES)},
    )
    bind.execute(
        sa.text(
            "DELETE FROM vocabulary_terms t "
            " WHERE t.kind = 'SKY_CLASS' AND t.label = ANY(:labels) "
            "   AND NOT EXISTS (SELECT 1 FROM image_records r WHERE r.sky_class_id = t.id)"
        ),
        {"labels": list(INVENTED_SKY_CLASSES)},
    )


def downgrade() -> None:
    """Deliberately not reinstated.

    The point of the upgrade is that these values were never observed. Writing
    them back would put fabricated provenance into the archive again, which is
    not something a rollback should be able to do by accident. Re-seeding from
    a provenance file that carries real values is the way back.
    """
