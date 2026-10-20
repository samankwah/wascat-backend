"""horizontal visibility

`image_records.visibility_m` - the horizontal visibility the station reported
at the frame's capture time, in metres, as a METAR or SYNOP carries it. It is
an observation supplied alongside the frame, not something the archive derives
from the image: nothing here estimates visibility from pixels.

NULL until a station report is attached. It is never defaulted, because a
defaulted visibility would claim a clear view nobody reported.

Metres, because that is the unit both report formats use and so the value can
be stored exactly as reported. The upper bound is generous rather than tied to
one format: METAR caps at 9999 ("10 km or more") and SYNOP reports beyond
70 km.

Written on 2026-09-30, but chained after 3c9f1a7e2b80 when the two were
separated on 2026-10-19, so the filename carries that date: the newest file
must be the head (see tests/test_migrations.py).

Revision ID: 5b8e2c4a9d17
Revises: 3c9f1a7e2b80
Created: 2026-09-30 12:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5b8e2c4a9d17"
down_revision: str | None = "3c9f1a7e2b80"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("image_records", sa.Column("visibility_m", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_image_records_visibility_range"),
        "image_records",
        "visibility_m IS NULL OR visibility_m BETWEEN 0 AND 100000",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_image_records_visibility_range"), "image_records", type_="check")
    op.drop_column("image_records", "visibility_m")
