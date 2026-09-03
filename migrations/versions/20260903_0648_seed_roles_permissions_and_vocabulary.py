"""seed roles permissions and vocabulary

Reference data the application needs to function at all: the permission set the
dashboard checks against, the three roles, and the controlled vocabularies the
public API validates against.

It lives in a migration rather than a script because the API's query schema
rejects a season that is not in this list. If the rows were optional, a fresh
database would serve an API whose filters silently matched nothing.

Written idempotently, so re-running against a database that already has them
is a no-op rather than a unique-constraint failure.

Revision ID: 2ca8f4766184
Revises: affcbf99e685
Created: 2026-09-03 06:48
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2ca8f4766184"
down_revision: str | None = "affcbf99e685"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("catalog:read", "View image records, collections and releases in the dashboard"),
    ("catalog:write", "Create, edit and retire image records, collections and releases"),
    ("release:publish", "Publish and retire releases"),
    ("vocab:read", "View controlled vocabularies"),
    ("vocab:write", "Add, rename, merge and reorder vocabulary terms"),
    ("ingest:run", "Start ingest runs and backfills"),
    ("user:manage", "Manage users and role assignments"),
    ("audit:read", "Read the audit log"),
)

ROLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "admin",
        "Full access, including publishing, user management and the audit log",
        tuple(slug for slug, _ in PERMISSIONS),
    ),
    (
        "curator",
        "Edits the catalogue and vocabularies; cannot publish or manage users",
        ("catalog:read", "catalog:write", "vocab:read", "vocab:write", "ingest:run"),
    ),
    (
        "viewer",
        "Read-only access to the dashboard",
        ("catalog:read", "vocab:read", "audit:read"),
    ),
)

# Mirrors lib/vocab.ts. Marked `system` because the public query schema
# validates against these values, so removing one would narrow the API's
# accepted input without anyone intending it.
VOCABULARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("SEASON", ("Harmattan", "Dry season", "Wet season", "Transition")),
    ("TIME_OF_DAY", ("Morning", "Midday", "Afternoon", "Evening")),
)


def _slugify(label: str) -> str:
    return "-".join(part for part in label.lower().replace("/", " ").split() if part)


def upgrade() -> None:
    # Bound parameters throughout. The values here are module constants, but a
    # migration is exactly the kind of file someone later pastes a variable
    # into, and string-built SQL is a bad shape to leave lying around.
    bind = op.get_bind()

    bind.execute(
        sa.text(
            """
            INSERT INTO permissions (id, slug, description)
            VALUES (gen_random_uuid(), :slug, :description)
            ON CONFLICT (slug) DO UPDATE SET description = EXCLUDED.description
            """
        ),
        [{"slug": slug, "description": description} for slug, description in PERMISSIONS],
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO roles (id, slug, description, system)
            VALUES (gen_random_uuid(), :slug, :description, true)
            ON CONFLICT (slug) DO UPDATE
                SET description = EXCLUDED.description, system = true
            """
        ),
        [{"slug": slug, "description": description} for slug, description, _ in ROLES],
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO role_permissions (role_id, permission_id)
            SELECT r.id, p.id
              FROM roles r, permissions p
             WHERE r.slug = :role AND p.slug = :permission
            ON CONFLICT DO NOTHING
            """
        ),
        [
            {"role": role, "permission": permission}
            for role, _, permissions in ROLES
            for permission in permissions
        ],
    )

    bind.execute(
        sa.text(
            """
            INSERT INTO vocabulary_terms (id, kind, slug, label, position, system)
            VALUES (gen_random_uuid(), CAST(:kind AS vocab_kind), :slug, :label, :position, true)
            ON CONFLICT (kind, slug) DO UPDATE
                SET label = EXCLUDED.label,
                    position = EXCLUDED.position,
                    system = true
            """
        ),
        [
            {"kind": kind, "slug": _slugify(label), "label": label, "position": position}
            for kind, labels in VOCABULARY
            for position, label in enumerate(labels)
        ],
    )


def downgrade() -> None:
    # Removes the seeded reference data. Any user still holding a seeded role
    # loses it, which is the point of stepping the migration back.
    op.execute("DELETE FROM vocabulary_terms WHERE system")
    op.execute("DELETE FROM role_permissions WHERE role_id IN (SELECT id FROM roles WHERE system)")
    op.execute("DELETE FROM user_roles WHERE role_id IN (SELECT id FROM roles WHERE system)")
    op.execute("DELETE FROM roles WHERE system")
    op.execute("DELETE FROM permissions")
