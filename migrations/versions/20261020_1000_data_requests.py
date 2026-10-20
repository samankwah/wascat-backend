"""data requests

Downloads are off for now. A researcher asks for a collection's data through
the site, the administrators are told, one of them approves or declines it in
the dashboard, and an approved requester is mailed a private link that expires.

`data_requests` holds that whole exchange. The access token is stored only as
its SHA-256, as refresh tokens are, so the table cannot be turned into links.

`request:review` is a new permission, given to the admin role. Existing admins
pick it up the next time they sign in, since the access token carries the
permissions it was issued with.

Revision ID: 8c41d7e2a9b3
Revises: 5b8e2c4a9d17
Created: 2026-10-20 10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "8c41d7e2a9b3"
down_revision: str | None = "5b8e2c4a9d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSION = ("request:review", "Review data requests and send or decline the data")


def upgrade() -> None:
    sa.Enum("PENDING", "APPROVED", "DECLINED", name="data_request_status").create(op.get_bind())

    op.create_table(
        "data_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("reference", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING", "APPROVED", "DECLINED", name="data_request_status", create_type=False
            ),
            server_default=sa.text("'PENDING'"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("organisation", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("collections", sa.ARRAY(sa.Text()), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("access_token_hash", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admins_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requester_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_mail_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "cardinality(collections) > 0", name=op.f("ck_data_requests_asks_for_something")
        ),
        sa.CheckConstraint(
            "status = 'PENDING' OR reviewed_at IS NOT NULL",
            name=op.f("ck_data_requests_decided_requests_are_reviewed"),
        ),
        sa.CheckConstraint(
            "status <> 'APPROVED' OR (access_token_hash IS NOT NULL AND access_expires_at IS NOT NULL)",
            name=op.f("ck_data_requests_approved_requests_have_access"),
        ),
        sa.CheckConstraint(
            "status <> 'DECLINED' OR decision_note IS NOT NULL",
            name=op.f("ck_data_requests_declines_give_a_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name=op.f("fk_data_requests_reviewed_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_requests")),
        sa.UniqueConstraint("reference", name=op.f("uq_data_requests_reference")),
        sa.UniqueConstraint("access_token_hash", name=op.f("uq_data_requests_access_token_hash")),
    )
    op.create_index("ix_data_requests_status_created_at", "data_requests", ["status", "created_at"])
    op.create_index("ix_data_requests_email_created_at", "data_requests", ["email", "created_at"])
    op.create_index("ix_data_requests_ip_created_at", "data_requests", ["ip", "created_at"])

    bind = op.get_bind()
    bind.execute(
        sa.text(
            """
            INSERT INTO permissions (id, slug, description)
            VALUES (gen_random_uuid(), :slug, :description)
            ON CONFLICT (slug) DO UPDATE SET description = EXCLUDED.description
            """
        ),
        {"slug": PERMISSION[0], "description": PERMISSION[1]},
    )
    bind.execute(
        sa.text(
            """
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id FROM roles r, permissions p
             WHERE r.slug = 'admin' AND p.slug = :slug
            ON CONFLICT DO NOTHING
            """
        ),
        {"slug": PERMISSION[0]},
    )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "DELETE FROM role_permissions WHERE permission_id IN (SELECT id FROM permissions WHERE slug = :slug)"
        ),
        {"slug": PERMISSION[0]},
    )
    bind.execute(sa.text("DELETE FROM permissions WHERE slug = :slug"), {"slug": PERMISSION[0]})
    op.drop_index("ix_data_requests_ip_created_at", table_name="data_requests")
    op.drop_index("ix_data_requests_email_created_at", table_name="data_requests")
    op.drop_index("ix_data_requests_status_created_at", table_name="data_requests")
    op.drop_table("data_requests")
    sa.Enum(name="data_request_status").drop(op.get_bind())
