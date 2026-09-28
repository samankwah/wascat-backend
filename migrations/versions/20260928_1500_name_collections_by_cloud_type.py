"""name collections by cloud type

Every collection was titled after its sequence number - "Sequence 01" from the
generated catalogue, "Capture sequence 01" on the site. The archive's curator
has read the cloud type off each sequence's frames, and that is what a reader
should see. The names live in seed/provenance.json ("title") so a fresh seed
gets them; this revision brings databases seeded before that up to date.

A collection is matched by the sequence its records carry, not by its slug:
a slug is a URL a curator can change (seq-011 already has, on some
databases), the sequence id is not. A title is written only where it is still
empty or still the generated "Sequence NN", so a name given in the dashboard
since is left alone. Collections only - no record, release or measurement is
touched.

Revision ID: c4f1a9e27b10
Revises: b8e4c1d7f302
Created: 2026-09-28 15:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f1a9e27b10"
down_revision: str | None = "b8e4c1d7f302"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Kept identical to the "title" entries in seed/provenance.json at the time
#: of writing. Repeated rather than read from the file: a migration records one
#: moment, and the file will go on being edited.
NAMES: tuple[tuple[str, str], ...] = (
    ("seq-001", "Cirrus"),
    ("seq-002", "Cirrostratus"),
    ("seq-003", "Stratocumulus"),
    ("seq-004", "Altostratus"),
    ("seq-005", "Altostratus"),
    ("seq-006", "Nimbostratus"),
    ("seq-007", "Altocumulus"),
    ("seq-008", "Cirrocumulus"),
    ("seq-009", "Stratus"),
    ("seq-010", "Cumulonimbus"),
    ("seq-011", "Cumulus"),
)


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            r"""
            UPDATE collections c
               SET title = :title
             WHERE (c.title IS NULL OR c.title ~ '^Sequence \d+$')
               AND EXISTS (
                   SELECT 1 FROM image_records r
                    WHERE r.collection_id = c.id AND r.sequence_id = :sequence_id
               )
            """
        ),
        [{"sequence_id": sequence_id, "title": title} for sequence_id, title in NAMES],
    )


def downgrade() -> None:
    """Back to the generated title, only where the name is still this one's."""
    op.get_bind().execute(
        sa.text(
            """
            UPDATE collections c
               SET title = 'Sequence ' || substring(:sequence_id from '(\\d{2})$')
             WHERE c.title = :title
               AND EXISTS (
                   SELECT 1 FROM image_records r
                    WHERE r.collection_id = c.id AND r.sequence_id = :sequence_id
               )
            """
        ),
        [{"sequence_id": sequence_id, "title": title} for sequence_id, title in NAMES],
    )
