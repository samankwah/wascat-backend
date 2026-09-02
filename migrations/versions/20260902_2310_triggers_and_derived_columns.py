"""triggers and derived columns

Where the domain invariants stop being conventions and become facts.

The service layer checks these too, because it can produce a decent error
message. The triggers exist so the rules hold regardless of which code path
writes - an admin endpoint, a bulk import, a psql session during an incident.

Revision ID: 7b20a592f028
Revises: 913452105ad1
Created: 2026-09-02 23:10:48.818043
"""

from collections.abc import Sequence

from alembic import op

revision: str = "7b20a592f028"
down_revision: str | None = "913452105ad1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Derived columns
# ---------------------------------------------------------------------------
# sort_key cannot be a GENERATED ALWAYS column: to_char() is STABLE rather than
# IMMUTABLE (its output depends on session settings), and PostgreSQL rejects
# non-immutable functions in generated columns. A BEFORE trigger it is.
DERIVE = """
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
    -- with '0', so timestamped records sort above untimestamped ones. The
    -- column is COLLATE "C" so the ordering is byte-wise and identical on
    -- every platform and locale.
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

# ---------------------------------------------------------------------------
# has_source / has_mask, maintained from the artifact rows
# ---------------------------------------------------------------------------
SYNC_FLAGS = """
CREATE OR REPLACE FUNCTION artifacts_sync_flags() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target text;
BEGIN
    target := coalesce(NEW.image_id, OLD.image_id);

    UPDATE image_records r SET
        has_source = EXISTS (
            SELECT 1 FROM artifacts a WHERE a.image_id = r.id AND a.type = 'source'
        ),
        has_mask = EXISTS (
            SELECT 1 FROM artifacts a WHERE a.image_id = r.id AND a.type = 'mask'
        )
    WHERE r.id = target;

    -- An UPDATE that moves an artifact between records has to refresh both.
    IF TG_OP = 'UPDATE' AND NEW.image_id IS DISTINCT FROM OLD.image_id THEN
        UPDATE image_records r SET
            has_source = EXISTS (
                SELECT 1 FROM artifacts a WHERE a.image_id = r.id AND a.type = 'source'
            ),
            has_mask = EXISTS (
                SELECT 1 FROM artifacts a WHERE a.image_id = r.id AND a.type = 'mask'
            )
        WHERE r.id = OLD.image_id;
    END IF;

    RETURN NULL;
END $$;
"""

# ---------------------------------------------------------------------------
# Invariant 2: a record holds a source, a mask, or both - never neither
# ---------------------------------------------------------------------------
# Deferred to the end of the transaction, because the record row is inserted
# before the artifacts that give it meaning. Note this is a CONSTRAINT TRIGGER
# rather than a CHECK: PostgreSQL cannot defer a CHECK, and an immediate one
# would reject every insert.
ASSERT_HAS_ARTIFACT = """
CREATE OR REPLACE FUNCTION image_records_assert_invariants() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    rec record;
BEGIN
    -- Re-read the row rather than trusting NEW. A deferred AFTER trigger fires
    -- with the tuple as it looked when the event was queued, and at INSERT
    -- time has_source/has_mask are still false: the artifacts that set them
    -- are written afterwards. Checking NEW would therefore reject every record
    -- ever inserted. What these invariants mean is "true once the transaction
    -- is done", so the current row is what has to be examined.
    SELECT has_source, has_mask, cloud_fraction
      INTO rec
      FROM image_records
     WHERE id = NEW.id;

    -- Deleted later in the same transaction; nothing left to assert about.
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    -- Invariant 2: a source, a mask, or both - never neither.
    IF NOT (rec.has_source OR rec.has_mask) THEN
        RAISE EXCEPTION
            'Image record % holds neither a source frame nor a mask', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;

    -- Invariant 3: cloud cover is measured from the mask, so a record
    -- reporting cover without carrying one is claiming a measurement that
    -- nothing produced.
    IF rec.cloud_fraction IS NOT NULL AND NOT rec.has_mask THEN
        RAISE EXCEPTION
            'Image record % reports cloud cover without a mask', NEW.id
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END $$;
"""

# ---------------------------------------------------------------------------
# Invariant 5: artifact dimensions match the record
# ---------------------------------------------------------------------------
# Derivatives are exempt. A thumbnail is a different size by definition, and a
# webp rendition may be too; only the frame and its mask describe the same
# pixels as the record does.
ASSERT_DIMENSIONS = """
CREATE OR REPLACE FUNCTION artifacts_assert_dimensions() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    rec record;
BEGIN
    IF NEW.type NOT IN ('source', 'mask') THEN
        RETURN NULL;
    END IF;
    IF NEW.width IS NULL OR NEW.height IS NULL THEN
        RETURN NULL;
    END IF;

    -- The artifact may have been replaced or removed later in the same
    -- transaction, in which case there is nothing left to check.
    IF NOT EXISTS (SELECT 1 FROM artifacts WHERE id = NEW.id) THEN
        RETURN NULL;
    END IF;

    SELECT width, height INTO rec FROM image_records WHERE id = NEW.image_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF NEW.width <> rec.width OR NEW.height <> rec.height THEN
        RAISE EXCEPTION
            'Artifact % (%x%) does not match the dimensions of record % (%x%)',
            NEW.type, NEW.width, NEW.height, NEW.image_id, rec.width, rec.height
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END $$;
"""

# ---------------------------------------------------------------------------
# Invariant 9: published releases are immutable
# ---------------------------------------------------------------------------
# The escape hatch is deliberate and narrow: a maintenance session can
#     SET LOCAL wascat.allow_published_writes = 'on';
# to correct a mistake. Being session-local it cannot be left on by accident,
# and the audit log still records whatever was changed.
# NOTE: this is a BEFORE trigger, so it must return the row. Returning NULL
# from a BEFORE ROW trigger silently CANCELS the operation - the insert appears
# to succeed and no row is written.
ASSERT_WRITABLE = """
CREATE OR REPLACE FUNCTION assert_release_writable() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    target_release uuid;
    release_state text;
    override text;
    result record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        result := OLD;
    ELSE
        result := NEW;
    END IF;

    override := current_setting('wascat.allow_published_writes', true);
    IF override IS NOT NULL AND lower(override) IN ('on', 'true', '1') THEN
        RETURN result;
    END IF;

    IF TG_TABLE_NAME = 'image_records' THEN
        target_release := coalesce(NEW.release_id, OLD.release_id);
    ELSE
        SELECT r.release_id INTO target_release
        FROM image_records r
        WHERE r.id = coalesce(NEW.image_id, OLD.image_id);
    END IF;

    IF target_release IS NULL THEN
        RETURN result;
    END IF;

    SELECT status::text INTO release_state FROM releases WHERE id = target_release;
    IF release_state IS NOT NULL AND release_state <> 'DRAFT' THEN
        RAISE EXCEPTION
            'Release % is %, and published releases are immutable',
            target_release, release_state
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN result;
END $$;
"""


def upgrade() -> None:
    op.execute(DERIVE)
    op.execute(SYNC_FLAGS)
    op.execute(ASSERT_HAS_ARTIFACT)
    op.execute(ASSERT_DIMENSIONS)
    op.execute(ASSERT_WRITABLE)

    op.execute("""
        CREATE TRIGGER image_records_derive_before
        BEFORE INSERT OR UPDATE ON image_records
        FOR EACH ROW EXECUTE FUNCTION image_records_derive()
    """)

    op.execute("""
        CREATE TRIGGER artifacts_sync_flags_after
        AFTER INSERT OR UPDATE OR DELETE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION artifacts_sync_flags()
    """)

    op.execute("""
        CREATE CONSTRAINT TRIGGER image_records_invariants
        AFTER INSERT OR UPDATE ON image_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION image_records_assert_invariants()
    """)

    op.execute("""
        CREATE CONSTRAINT TRIGGER artifacts_dimensions_match
        AFTER INSERT OR UPDATE ON artifacts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION artifacts_assert_dimensions()
    """)

    op.execute("""
        CREATE TRIGGER image_records_release_writable
        BEFORE INSERT OR UPDATE OR DELETE ON image_records
        FOR EACH ROW EXECUTE FUNCTION assert_release_writable()
    """)

    op.execute("""
        CREATE TRIGGER artifacts_release_writable
        BEFORE INSERT OR UPDATE OR DELETE ON artifacts
        FOR EACH ROW EXECUTE FUNCTION assert_release_writable()
    """)


def downgrade() -> None:
    for table, trigger in (
        ("artifacts", "artifacts_release_writable"),
        ("image_records", "image_records_release_writable"),
        ("artifacts", "artifacts_dimensions_match"),
        ("image_records", "image_records_invariants"),
        ("artifacts", "artifacts_sync_flags_after"),
        ("image_records", "image_records_derive_before"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")

    for function in (
        "assert_release_writable",
        "artifacts_assert_dimensions",
        "image_records_assert_invariants",
        "artifacts_sync_flags",
        "image_records_derive",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
