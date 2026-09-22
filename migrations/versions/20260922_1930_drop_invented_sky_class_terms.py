"""drop the invented sky-class vocabulary

The nine SKY_CLASS terms seeded by c1a4e9f2b6d3 were written alongside the
per-sequence `skyClass` values in seed/provenance.json, and neither came from
an observer - the sequence-wide label has since been removed for the same
reason. The terms also bundle genera the observer's own codes keep apart
("Cirrocumulus / Altocumulus" for both CC and AC, "Stratus / Nimbostratus" for
both ST and NS), so keeping them would force real labels into coarser buckets
than the data has.

SKY_CLASS is left empty on purpose. `wascat ingest labels` creates exactly the
terms the label table uses, so the vocabulary ends up matching the archive's
own codes rather than a list someone guessed in advance.

Only unreferenced terms are removed. A term some record still carries is left
alone and reported by `wascat doctor` instead: a migration that silently
unclassifies records would be exactly the kind of invisible data loss the
vocabulary's merge-never-delete rule exists to prevent.

Revision ID: 4f2b8c7d1e05
Revises: c1a4e9f2b6d3
Created: 2026-09-22 19:30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f2b8c7d1e05"
down_revision: str | None = "c1a4e9f2b6d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The exact labels c1a4e9f2b6d3 seeded. Named explicitly so a curator's own
# SKY_CLASS terms, added since, are never caught by this.
SEEDED: tuple[str, ...] = (
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
    # Aliases first: a term cannot be deleted while one points at it, and an
    # alias of a term nothing uses has nothing to resolve to either.
    bind.execute(
        sa.text(
            """
            DELETE FROM vocabulary_aliases
            WHERE term_id IN (
                SELECT t.id FROM vocabulary_terms t
                WHERE t.kind = 'SKY_CLASS'
                  AND t.label = ANY(:labels)
                  AND NOT EXISTS (
                      SELECT 1 FROM image_records r WHERE r.sky_class_id = t.id
                  )
            )
            """
        ),
        {"labels": list(SEEDED)},
    )
    bind.execute(
        sa.text(
            """
            DELETE FROM vocabulary_terms t
            WHERE t.kind = 'SKY_CLASS'
              AND t.label = ANY(:labels)
              AND NOT EXISTS (
                  SELECT 1 FROM image_records r WHERE r.sky_class_id = t.id
              )
            """
        ),
        {"labels": list(SEEDED)},
    )


def downgrade() -> None:
    # Restores what c1a4e9f2b6d3 seeded, in its original order.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO vocabulary_terms (id, kind, slug, label, position, system)
            VALUES (gen_random_uuid(), CAST(:kind AS vocab_kind), :slug, :label, :position, false)
            ON CONFLICT (kind, slug) DO NOTHING
            """
        ),
        [
            {
                "kind": "SKY_CLASS",
                "slug": "-".join(p for p in label.lower().replace("/", " ").split() if p),
                "label": label,
                "position": position,
            }
            for position, label in enumerate(SEEDED)
        ],
    )
