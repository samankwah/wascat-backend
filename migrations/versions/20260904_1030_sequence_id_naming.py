"""sequence id naming: video_id -> sequence_id

Renames the capture-run identifier from the delivery pipeline's shorthand
("vid1"..."vid11") to a zero-padded, fixed-width form the archive already
calls itself by everywhere else - the README, the `sequences` facet, every UI
label - just not in the column that carries it.

"vid1" caused two real problems, not only a vocabulary mismatch:

* Unpadded, it does not sort. The collections grid orders by
  (position, slug), every position is 0 today, and the slug tiebreak was
  lexicographic: vid1, vid10, vid11, vid2, ... "seq-001" through "seq-011"
  fixes that with no code change.
* "vid1" is a prefix of "vid11", so the substring search the archive
  deliberately kept (rather than moving to full-text - see the pg_trgm
  migration) could match one sequence's frames against another's id. Fixed
  width ends that collision class.

The new form also has a slot for a capture date - "seq-20260904-001" - for
when the capture team supplies timestamps, without ever renumbering the
eleven sequences that exist today. A CHECK enforces that a dated id always
carries a timestamp, because sort_key's ordering only holds if it does.

sequence_number and sort_key do not need recomputing: both are derived from
the *ordinal* ("7" in "vid7" or "seq-007"), which the rename preserves
exactly. search_text embeds the identifier as text, so it does need
recomputing - done here by re-firing the derive trigger over every row.

Revision ID: b7757481e635
Revises: 2ca8f4766184
Created: 2026-09-04 10:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7757481e635"
down_revision: str | None = "2ca8f4766184"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Accepts "seq-001" today and "seq-20260904-001" once a sequence carries a
# capture date. Kept identical to catalog.models.SEQUENCE_ID_PATTERN.
SEQUENCE_ID_CHECK = r"sequence_id ~ '^seq-(\d{8}-)?\d{3}$'"

DERIVE = """
CREATE OR REPLACE FUNCTION image_records_derive() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    okta_tag text;
    pairing_tag text;
BEGIN
    -- Guarded: a malformed id would make substring()::int raise a cast error
    -- from inside the trigger, masking the sequence_id_format CHECK that is
    -- the real, legible complaint. Fall back to 0 and let it fire. The last
    -- three digits are the ordinal in both the undated and dated forms.
    NEW.sequence_number := CASE
        WHEN NEW.sequence_id ~ '^seq-(\\d{8}-)?\\d{3}$'
            THEN substring(NEW.sequence_id from '(\\d{3})$')::int
        ELSE 0
    END;

    -- Real timestamp when known, sequence position otherwise. Both are fixed
    -- width, and an ISO timestamp starts with '2' while a sequence key starts
    -- with '0', so timestamped records sort above untimestamped ones under
    -- any collation. A dated sequence id always has captured_at (enforced by
    -- dated_sequence_has_timestamp), so it takes the timestamp branch here
    -- too - the two stay in step. The column is COLLATE "C" so the ordering
    -- is byte-wise and identical on every platform and locale.
    NEW.sort_key := coalesce(
        to_char(NEW.captured_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
        lpad(NEW.sequence_number::text, 3, '0') || '-' || lpad(NEW.frame_index::text, 7, '0')
    );

    -- Mirrors lib/search.ts:
    --   [id, sequenceId, String(frameIndex), location ?? "", ...tags].join(" ")
    -- where tags = [sequenceId, oktaLabel | "unsegmented", pairing].
    -- sequenceId therefore appears twice, and a missing location leaves an
    -- empty segment rather than collapsing: concat_ws skips NULL but keeps ''.
    okta_tag := CASE
        WHEN NEW.cloud_cover_oktas IS NULL THEN 'unsegmented'
        WHEN NEW.cloud_cover_oktas = 0 THEN '0/8 ' || chr(183) || ' Clear'
        WHEN NEW.cloud_cover_oktas = 8 THEN '8/8 ' || chr(183) || ' Overcast'
        ELSE NEW.cloud_cover_oktas::text || '/8'
    END;
    pairing_tag := CASE
        WHEN NEW.has_source AND NEW.has_mask THEN 'source + mask'
        WHEN NEW.has_mask THEN 'mask only'
        ELSE 'source only'
    END;

    NEW.search_text := lower(concat_ws(' ',
        NEW.id,
        NEW.sequence_id,
        NEW.frame_index::text,
        coalesce(NEW.location_label, ''),
        NEW.sequence_id,
        okta_tag,
        pairing_tag
    ));

    NEW.updated_at := now();
    RETURN NEW;
END $$;
"""

# The pre-rename derive function, verbatim from 20260902_2310, for downgrade.
_OLD_DERIVE = """
CREATE OR REPLACE FUNCTION image_records_derive() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    okta_tag text;
    pairing_tag text;
BEGIN
    -- Guarded: an id like 'video1' would make substring()::int raise a cast
    -- error from inside the trigger, masking the video_id_format CHECK that
    -- is the real, legible complaint. Fall back to 0 and let it fire.
    NEW.video_number := CASE
        WHEN NEW.video_id ~ '^vid[0-9]+$' THEN substring(NEW.video_id from 4)::int
        ELSE 0
    END;

    -- Real timestamp when known, sequence position otherwise. Both are fixed
    -- width, and an ISO timestamp starts with '2' while a sequence key starts
    -- with '0', so timestamped records sort above untimestamped ones under
    -- any collation. The column is COLLATE "C" so the ordering is byte-wise
    -- and identical on every platform and locale.
    NEW.sort_key := coalesce(
        to_char(NEW.captured_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
        lpad(NEW.video_number::text, 3, '0') || '-' || lpad(NEW.frame_index::text, 7, '0')
    );

    -- Mirrors lib/search.ts:
    --   [id, videoId, String(frameIndex), location ?? "", ...tags].join(" ")
    -- where tags = [videoId, oktaLabel | "unsegmented", pairing].
    -- videoId therefore appears twice, and a missing location leaves an empty
    -- segment rather than collapsing: concat_ws skips NULL but keeps ''.
    okta_tag := CASE
        WHEN NEW.cloud_cover_oktas IS NULL THEN 'unsegmented'
        WHEN NEW.cloud_cover_oktas = 0 THEN '0/8 ' || chr(183) || ' Clear'
        WHEN NEW.cloud_cover_oktas = 8 THEN '8/8 ' || chr(183) || ' Overcast'
        ELSE NEW.cloud_cover_oktas::text || '/8'
    END;
    pairing_tag := CASE
        WHEN NEW.has_source AND NEW.has_mask THEN 'source + mask'
        WHEN NEW.has_mask THEN 'mask only'
        ELSE 'source only'
    END;

    NEW.search_text := lower(concat_ws(' ',
        NEW.id,
        NEW.video_id,
        NEW.frame_index::text,
        coalesce(NEW.location_label, ''),
        NEW.video_id,
        okta_tag,
        pairing_tag
    ));

    NEW.updated_at := now();
    RETURN NEW;
END $$;
"""


def upgrade() -> None:
    # Drop the old-format CHECKs first: the rename below is a pure catalog
    # operation and PostgreSQL updates constraint expressions to follow it
    # automatically, so leaving the vid[0-9]+ CHECK attached would reject the
    # very data rewrite this migration exists to do.
    op.drop_constraint(
        op.f("ck_sequence_references_video_id_format"), "sequence_references", type_="check"
    )
    op.drop_constraint(op.f("ck_image_records_video_id_format"), "image_records", type_="check")

    op.alter_column("sequence_references", "video_id", new_column_name="sequence_id")
    op.alter_column("image_records", "video_id", new_column_name="sequence_id")
    op.alter_column("image_records", "video_number", new_column_name="sequence_number")

    # Install the new derive logic before touching any row, so the UPDATE
    # below - which fires this trigger - recomputes search_text against the
    # renamed values instead of erroring on the columns it used to read.
    op.execute(DERIVE)

    # A rename, not an edit - but assert_release_writable() cannot tell the
    # difference, and most of the archive's rows sit in a PUBLISHED release.
    # The documented escape hatch: session-local, so it cannot leak past this
    # migration, and the audit trail is unaffected because nothing here goes
    # through the service layer that writes it.
    op.execute("SET LOCAL wascat.allow_published_writes = 'on'")

    op.execute(
        "UPDATE sequence_references SET sequence_id = "
        "'seq-' || lpad(substring(sequence_id from '^vid([0-9]+)$'), 3, '0') "
        "WHERE sequence_id ~ '^vid[0-9]+$'"
    )
    # A no-op assignment (sequence_id to itself) still fires the BEFORE
    # trigger, which is what re-derives search_text from the new value.
    # sequence_number and sort_key do not change: both come from the ordinal,
    # which the rename preserves exactly ("vid7" and "seq-007" are both 7).
    op.execute(
        "UPDATE image_records SET sequence_id = "
        "'seq-' || lpad(substring(sequence_id from '^vid([0-9]+)$'), 3, '0') "
        "WHERE sequence_id ~ '^vid[0-9]+$'"
    )

    # A collection's slug defaults to its sequence id (documented in
    # provenance.json's _readme) but is stored independently, so the rename
    # above does not touch it. Only rows that still carry that default are
    # translated - a curator who renamed a collection to something else is
    # left alone, same as the frontend expects: the slug just happens to be
    # a URL segment a curator can already edit.
    op.execute(
        "UPDATE collections SET slug = "
        "'seq-' || lpad(substring(slug from '^vid([0-9]+)$'), 3, '0') "
        "WHERE slug ~ '^vid[0-9]+$'"
    )

    # The row-level rewrite above queues this table's deferred constraint
    # triggers (invariants 2 and 3). ALTER TABLE needs an exclusive lock and
    # PostgreSQL refuses to grant one over pending trigger events, so they
    # have to fire now rather than at commit.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")

    op.create_check_constraint(
        op.f("ck_sequence_references_sequence_id_format"),
        "sequence_references",
        SEQUENCE_ID_CHECK,
    )
    op.create_check_constraint(
        op.f("ck_image_records_sequence_id_format"), "image_records", SEQUENCE_ID_CHECK
    )
    # A dated sequence id repeats its ordinal across days, so two of them
    # would share a numeric sort_key prefix. That never happens because a
    # dated sequence sorts by its timestamp instead - which is only true if
    # the timestamp is actually there. Make the database say so.
    op.create_check_constraint(
        op.f("ck_image_records_dated_sequence_has_timestamp"),
        "image_records",
        r"sequence_id !~ '^seq-\d{8}-' OR captured_at IS NOT NULL",
    )

    op.execute(
        "ALTER TABLE sequence_references "
        "RENAME CONSTRAINT uq_sequence_references_video_id "
        "TO uq_sequence_references_sequence_id"
    )
    op.execute("ALTER INDEX ix_image_records_video_sort RENAME TO ix_image_records_sequence_sort")


def downgrade() -> None:
    op.execute("SET LOCAL wascat.allow_published_writes = 'on'")

    op.execute("ALTER INDEX ix_image_records_sequence_sort RENAME TO ix_image_records_video_sort")
    op.execute(
        "ALTER TABLE sequence_references "
        "RENAME CONSTRAINT uq_sequence_references_sequence_id "
        "TO uq_sequence_references_video_id"
    )

    op.drop_constraint(
        op.f("ck_image_records_dated_sequence_has_timestamp"), "image_records", type_="check"
    )
    op.drop_constraint(op.f("ck_image_records_sequence_id_format"), "image_records", type_="check")
    op.drop_constraint(
        op.f("ck_sequence_references_sequence_id_format"), "sequence_references", type_="check"
    )

    # Dated ids ("seq-20260904-001") have no vid<N> counterpart. A downgrade
    # after any such row exists cannot round-trip it and must not guess.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM image_records WHERE sequence_id ~ '^seq-\\d{8}-'
                UNION ALL
                SELECT 1 FROM sequence_references WHERE sequence_id ~ '^seq-\\d{8}-'
                UNION ALL
                SELECT 1 FROM collections WHERE slug ~ '^seq-\\d{8}-'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade: a dated sequence id has no vid<N> form.';
            END IF;
        END $$;
        """
    )

    op.execute(
        "UPDATE image_records SET sequence_id = "
        "'vid' || (substring(sequence_id from '(\\d{3})$')::int) "
        "WHERE sequence_id ~ '^seq-\\d{3}$'"
    )
    op.execute(
        "UPDATE sequence_references SET sequence_id = "
        "'vid' || (substring(sequence_id from '(\\d{3})$')::int) "
        "WHERE sequence_id ~ '^seq-\\d{3}$'"
    )
    # Only the default slugs the upgrade translated - a collection a curator
    # has since renamed away from "seq-NNN" is left exactly as it is.
    op.execute(
        "UPDATE collections SET slug = "
        "'vid' || (substring(slug from '(\\d{3})$')::int) "
        "WHERE slug ~ '^seq-\\d{3}$'"
    )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")

    op.alter_column("image_records", "sequence_number", new_column_name="video_number")
    op.alter_column("image_records", "sequence_id", new_column_name="video_id")
    op.alter_column("sequence_references", "sequence_id", new_column_name="video_id")

    op.execute(_OLD_DERIVE)
    # Re-fire the (now old) trigger so search_text carries "vid" tokens again
    # instead of the stale "seq-" text the rename left behind.
    op.execute("UPDATE image_records SET video_id = video_id")

    op.create_check_constraint(
        op.f("ck_sequence_references_video_id_format"),
        "sequence_references",
        r"video_id ~ '^vid[0-9]+$'",
    )
    op.create_check_constraint(
        op.f("ck_image_records_video_id_format"), "image_records", r"video_id ~ '^vid[0-9]+$'"
    )
