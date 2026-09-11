"""seed sky class vocabulary

Nine sky-classification terms for the `sky_class` field images and
collections already carry a column for, but which - unlike SEASON and
TIME_OF_DAY - has never had any vocabulary rows: the public query schema
doesn't validate against it, so unlike the two contract vocabularies these
are ordinary curator-manageable terms (addable/renameable/mergeable via the
existing /admin/vocabulary/sky_class endpoints), not `system` rows.

Written idempotently, so re-running against a database that already has them
is a no-op rather than a unique-constraint failure.

Revision ID: c1a4e9f2b6d3
Revises: b7757481e635
Created: 2026-09-11 21:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a4e9f2b6d3"
down_revision: str | None = "b7757481e635"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Standard cloud genera, plus Haze/Dust for the Harmattan conditions the
# archive's SEASON vocabulary already names. Mirrors lib/vocab.ts's
# oktaLabel's neighbouring convention: a short human label, ordered from
# clearest to most obscured sky.
SKY_CLASSES: tuple[str, ...] = (
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


def _slugify(label: str) -> str:
    return "-".join(part for part in label.lower().replace("/", " ").split() if part)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO vocabulary_terms (id, kind, slug, label, position, system)
            VALUES (gen_random_uuid(), CAST(:kind AS vocab_kind), :slug, :label, :position, false)
            ON CONFLICT (kind, slug) DO UPDATE
                SET label = EXCLUDED.label,
                    position = EXCLUDED.position
            """
        ),
        [
            {"kind": "SKY_CLASS", "slug": _slugify(label), "label": label, "position": position}
            for position, label in enumerate(SKY_CLASSES)
        ],
    )


def downgrade() -> None:
    op.execute("DELETE FROM vocabulary_terms WHERE kind = 'SKY_CLASS'")
