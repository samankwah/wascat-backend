"""place sequences at GMet synoptic stations

Gives every sequence the station, place, coordinates and capture timing now
in seed/provenance.json, for databases seeded before those were filled in.
Production's image is built without seed/, so `wascat ingest provenance`
cannot reach it there; this revision does the same writes on deploy.

The stations and coordinates are real GMet synoptic stations (positions from
NOAA's ISD station history). Which station each sequence is assigned to, and
the capture times, are placeholders pending the capture team's records - see
the provenance file's readme.

A value is written only where the column is still empty, so an edit made in
the dashboard is left alone. capturedAt is startedAt + frameIndex x interval.
Provenance columns only: no measurement, artifact or label is touched.

Revision ID: e7a3d5b9c1f4
Revises: c4f1a9e27b10
Created: 2026-09-28 22:00
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "e7a3d5b9c1f4"
down_revision: str | None = "c4f1a9e27b10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (sequence, station, place, latitude, longitude, startedAt, interval seconds)
#: Copied from seed/provenance.json at the time of writing. Repeated rather
#: than read from the file: a migration records one moment, and the file will
#: go on being edited.
STATIONS: tuple[tuple[str, str, str, float, float, str, float], ...] = (
    (
        "seq-001",
        "Tamale Synoptic Station",
        "Tamale, Ghana",
        9.557,
        -0.863,
        "2026-08-30T06:00:00Z",
        12.0,
    ),
    (
        "seq-002",
        "Accra (Kotoka) Synoptic Station",
        "Accra, Ghana",
        5.605,
        -0.167,
        "2026-08-30T06:00:00Z",
        4.2,
    ),
    (
        "seq-003",
        "Kumasi Synoptic Station",
        "Kumasi, Ghana",
        6.715,
        -1.591,
        "2026-08-30T06:00:00Z",
        43.0,
    ),
    (
        "seq-004",
        "Sunyani Synoptic Station",
        "Sunyani, Ghana",
        7.362,
        -2.329,
        "2026-08-30T06:00:00Z",
        75.8,
    ),
    ("seq-005", "Ho Synoptic Station", "Ho, Ghana", 6.58, 0.533, "2026-08-30T06:00:00Z", 82.9),
    (
        "seq-006",
        "Takoradi Synoptic Station",
        "Takoradi, Ghana",
        4.896,
        -1.775,
        "2026-08-30T06:00:00Z",
        147.4,
    ),
    (
        "seq-007",
        "Koforidua Synoptic Station",
        "Koforidua, Ghana",
        6.083,
        -0.25,
        "2026-08-30T06:00:00Z",
        23.5,
    ),
    (
        "seq-008",
        "Navrongo Synoptic Station",
        "Navrongo, Ghana",
        10.9,
        -1.1,
        "2026-08-30T06:00:00Z",
        48.9,
    ),
    (
        "seq-009",
        "Saltpond Synoptic Station",
        "Saltpond, Ghana",
        5.2,
        -1.067,
        "2026-08-30T06:00:00Z",
        490.9,
    ),
    ("seq-010", "Wa Synoptic Station", "Wa, Ghana", 10.083, -2.508, "2026-08-30T06:00:00Z", 23.6),
    (
        "seq-011",
        "Axim Synoptic Station",
        "Axim, Ghana",
        4.867,
        -2.233,
        "2026-08-30T06:00:00Z",
        20.4,
    ),
)


def _params() -> list[dict[str, object]]:
    return [
        {
            "sequence_id": sequence_id,
            "station": station,
            "site": site,
            "latitude": latitude,
            "longitude": longitude,
            # asyncpg binds timestamptz from a datetime, not a string.
            "started_at": datetime.fromisoformat(started_at.replace("Z", "+00:00")),
            "interval": interval,
        }
        for sequence_id, station, site, latitude, longitude, started_at, interval in STATIONS
    ]


def upgrade() -> None:
    bind = op.get_bind()
    # Every record sits in a published release; provenance is exactly the
    # curated kind of field a release never claimed to pin.
    bind.execute(sa.text("SET LOCAL wascat.allow_published_writes = 'on'"))
    bind.execute(
        sa.text(
            """
            UPDATE image_records r
               SET captured_at = coalesce(
                       r.captured_at,
                       CAST(:started_at AS timestamptz)
                         + r.frame_index * CAST(:interval AS double precision)
                           * interval '1 second'),
                   location_label = coalesce(r.location_label, :site),
                   latitude = CASE WHEN r.latitude IS NULL THEN :latitude ELSE r.latitude END,
                   longitude = CASE WHEN r.latitude IS NULL THEN :longitude ELSE r.longitude END
             WHERE r.sequence_id = :sequence_id
            """
        ),
        _params(),
    )
    bind.execute(
        sa.text(
            """
            UPDATE collections c
               SET location_name = coalesce(c.location_name, :station),
                   latitude = CASE WHEN c.latitude IS NULL THEN :latitude ELSE c.latitude END,
                   longitude = CASE WHEN c.latitude IS NULL THEN :longitude ELSE c.longitude END
             WHERE EXISTS (
                   SELECT 1 FROM image_records r
                    WHERE r.collection_id = c.id AND r.sequence_id = :sequence_id
               )
            """
        ),
        _params(),
    )


def downgrade() -> None:
    """Clear what this revision wrote, only where it is still this revision's value."""
    bind = op.get_bind()
    bind.execute(sa.text("SET LOCAL wascat.allow_published_writes = 'on'"))
    bind.execute(
        sa.text(
            """
            UPDATE image_records r
               SET captured_at = NULL, location_label = NULL, latitude = NULL, longitude = NULL
             WHERE r.sequence_id = :sequence_id
               AND r.location_label = :site
               AND r.latitude = :latitude AND r.longitude = :longitude
            """
        ),
        _params(),
    )
    bind.execute(
        sa.text(
            """
            UPDATE collections c
               SET location_name = NULL, latitude = NULL, longitude = NULL
             WHERE c.location_name = :station
               AND EXISTS (
                   SELECT 1 FROM image_records r
                    WHERE r.collection_id = c.id AND r.sequence_id = :sequence_id
               )
            """
        ),
        _params(),
    )
