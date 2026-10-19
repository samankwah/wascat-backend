"""revoke supabase api roles

Supabase serves the ``public`` schema over its auto-generated REST API
(PostgREST) to the ``anon`` and ``authenticated`` roles, and by default grants
them full access to every table ``postgres`` creates there. The ``anon`` key
is public by design, so on a fresh Supabase project ``users``, ``sessions``
and the rest would be readable and writable by anyone who found the project
URL. This service never goes through that API - it connects as ``postgres``
and enforces access itself - so those roles get nothing.

The revoke also covers default privileges, so tables added by later
migrations are not granted either.

A no-op on plain PostgreSQL (local Docker, tests), where the roles don't
exist.

Revision ID: 3c9f1a7e2b80
Revises: e7a3d5b9c1f4
Created: 2026-10-19 12:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "3c9f1a7e2b80"
down_revision: str | None = "e7a3d5b9c1f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            api_role text;
        BEGIN
            FOREACH api_role IN ARRAY ARRAY['anon', 'authenticated'] LOOP
                IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = api_role) THEN
                    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', api_role);
                    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', api_role);
                    EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', api_role);
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
                        api_role
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM %I',
                        api_role
                    );
                    EXECUTE format(
                        'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM %I',
                        api_role
                    );
                END IF;
            END LOOP;
        END
        $$
        """
    )


def downgrade() -> None:
    # Left revoked on purpose. Re-granting would reopen the tables to the
    # public API key, which no schema rollback should ever do.
    pass
