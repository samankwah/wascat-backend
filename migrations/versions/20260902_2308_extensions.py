"""extensions

Revision ID: db6e860d3c40
Revises:
Created: 2026-09-02 23:08:54.263178
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "db6e860d3c40"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # citext backs users.email, so "Ada@example.org" and "ada@example.org"
    # cannot become two accounts.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    # pg_trgm indexes the substring search. lib/search.ts matched with
    # String.includes, so `q` stays a LIKE '%...%' rather than becoming a
    # tsquery, which would silently stop matching "vid1" inside "vid11".
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # Left installed on purpose. Dropping an extension that another schema in
    # the same database may be using is not this migration's business.
    pass
