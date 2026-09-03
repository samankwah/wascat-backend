"""frame uniqueness is per release

A frame is unique within a release, not across the whole archive. The original
index spanned (video_id, frame_index) globally, which quietly made a second
release of any sequence impossible: re-ingesting vid1 frame 5 - re-measured,
re-encoded, or simply carried forward - would collide with the first release's
row. Found by a test that created a record while the seeded catalogue was
present.

Revision ID: affcbf99e685
Revises: 7b20a592f028
Created: 2026-09-03 06:16:47.134961
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "affcbf99e685"
down_revision: str | None = "7b20a592f028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_image_records_frame", table_name="image_records")
    op.create_index(
        "ix_image_records_release_frame",
        "image_records",
        ["release_id", "video_id", "frame_index"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_image_records_release_frame", table_name="image_records")
    op.create_index(
        "ix_image_records_frame",
        "image_records",
        ["video_id", "frame_index"],
        unique=True,
    )
