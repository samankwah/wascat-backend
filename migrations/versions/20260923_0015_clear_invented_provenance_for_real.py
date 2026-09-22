"""clear the invented provenance on databases that ran past d3b1c7a49f20

d3b1c7a49f20 does the right thing and has never run in production. Twice now
it has been chained *behind* a revision production had already applied, and
both times `alembic upgrade head` found the database at the head, had nothing
to do, and exited clean:

  #3  chained it behind 4f2b8c7d1e05, which #1's deploy had applied
  #7  chained it behind 89aa875b8008, which #5's deploy had applied

The second is the instructive one. When #7 was written the pointer was at
4f2b8c7d1e05, so chaining onto that looked right; #5 had deployed in between
and moved it to 89aa875b8008 without anything saying so. Checking where the
head is once is not enough, because a deploy can move it while the fix is in
review.

So this revision is appended to the head rather than reasoning about where
the head ought to be, and it is written to be a no-op wherever d3b1c7a49f20
already ran. Appending is always safe; splicing is safe only while every
environment is still behind the splice point, which is not a property this
repository can check and not one a reviewer can be asked to hold in mind.

d3b1c7a49f20 stays. It is correct for a database built from scratch, it has
run on those, and removing an applied revision would strand them.

What this does is exactly what that one does, and the statements are repeated
here rather than imported: a migration is a historical record of one moment,
and one that reached into another for its behaviour would change meaning
whenever that other was edited.

Revision ID: b8e4c1d7f302
Revises: 89aa875b8008
Created: 2026-09-23 00:15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e4c1d7f302"
down_revision: str | None = "89aa875b8008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: Exactly what e31006a wrote, slug by slug: the site name and the public city
#: coordinates that went with it. A row is cleared only when it still matches
#: all three, so a real site name supplied since - which is the entire point
#: of the provenance file - is left alone.
INVENTED_SITES: tuple[tuple[str, str, str, str], ...] = (
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
    # Every record sits in a published release, and what a release pins is its
    # files and its measurements. Neither is touched here.
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
            for slug, site, latitude, longitude in INVENTED_SITES
        ],
    )

    bind.execute(
        sa.text(
            "UPDATE image_records SET sky_class_id = NULL, sky_class_label = NULL "
            "WHERE sky_class_id IS NOT NULL OR sky_class_label IS NOT NULL"
        )
    )

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
