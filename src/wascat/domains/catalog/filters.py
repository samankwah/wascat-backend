"""ImageQuery -> SQL.

A direct port of ``filterImages`` in lib/search.ts. Two of its rules look like
omissions and are in fact the point of the archive:

* A cloud-cover filter cannot match an unsegmented frame. Cover is measured
  from the mask, so a frame without one has no measurement - and treating its
  absence as 0 would invent a clear sky that nobody observed.
* A date filter cannot match a frame with no capture timestamp, for the same
  reason. Both bounds behave this way; the TypeScript's ``to`` did not, which
  was a bug fixed before this port.

Each rule is also the partial index the planner uses, so the SQL and the
storage agree rather than merely coinciding.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Select, and_, exists, func, or_, select

from wascat.domains.catalog.models import Artifact, Collection, ImageRecord, Release
from wascat.domains.catalog.query import ImageQuery


def apply_filters(stmt: Select[Any], query: ImageQuery) -> Select[Any]:
    """Narrow a statement over image_records according to the query."""
    conditions = []

    if query.q:
        # lib/search.ts used String.includes on a lowercased join, so this
        # stays a substring match rather than becoming a tsquery. Full-text
        # would stop matching "vid1" inside "vid11", which the archive's own
        # tests assert. autoescape neutralises % and _ in user input.
        conditions.append(ImageRecord.search_text.contains(query.q.lower(), autoescape=True))

    if query.collection:
        conditions.append(Collection.slug == query.collection)

    if query.release:
        conditions.append(Release.version == query.release)

    if query.video:
        conditions.append(ImageRecord.video_id == query.video)

    if query.segmented is not None:
        conditions.append(ImageRecord.has_mask.is_(query.segmented))

    # --- cloud cover: measured records only -------------------------------
    if query.oktas is not None:
        conditions.append(
            and_(
                ImageRecord.cloud_cover_oktas.is_not(None),
                ImageRecord.cloud_cover_oktas == query.oktas,
            )
        )
    if query.oktas_min is not None:
        conditions.append(
            and_(
                ImageRecord.cloud_cover_oktas.is_not(None),
                ImageRecord.cloud_cover_oktas >= query.oktas_min,
            )
        )
    if query.oktas_max is not None:
        conditions.append(
            and_(
                ImageRecord.cloud_cover_oktas.is_not(None),
                ImageRecord.cloud_cover_oktas <= query.oktas_max,
            )
        )

    if query.season:
        conditions.append(ImageRecord.season_label == query.season)

    if query.time:
        conditions.append(ImageRecord.time_of_day_label == query.time)

    if query.location:
        # Substring, case-insensitive, matching the TypeScript's
        # location.toLowerCase().includes(...).
        conditions.append(
            func.lower(func.coalesce(ImageRecord.location_label, "")).contains(
                query.location.lower(), autoescape=True
            )
        )

    if query.artifact:
        conditions.append(
            exists(
                select(1)
                .select_from(Artifact)
                .where(
                    Artifact.image_id == ImageRecord.id,
                    Artifact.type == query.artifact,
                )
            )
        )

    # --- capture date: timestamped records only ---------------------------
    if query.date_from is not None:
        conditions.append(
            and_(
                ImageRecord.captured_at.is_not(None),
                func.date(func.timezone("UTC", ImageRecord.captured_at))
                >= date.fromisoformat(query.date_from),
            )
        )
    if query.date_to is not None:
        conditions.append(
            and_(
                ImageRecord.captured_at.is_not(None),
                func.date(func.timezone("UTC", ImageRecord.captured_at))
                <= date.fromisoformat(query.date_to),
            )
        )

    # Retired records are hidden from the public API but kept for the audit
    # trail and for the dashboard to restore.
    conditions.append(ImageRecord.retired_at.is_(None))

    return stmt.where(and_(*conditions)) if conditions else stmt


def apply_ordering(stmt: Select[Any], query: ImageQuery) -> Select[Any]:
    """Order by (sort_key, id).

    The id tiebreak makes the ordering total, which keyset pagination needs:
    without it, two records sharing a sort_key could be skipped or repeated
    across a page boundary.
    """
    if query.sort == "oldest":
        return stmt.order_by(ImageRecord.sort_key.asc(), ImageRecord.id.asc())
    return stmt.order_by(ImageRecord.sort_key.desc(), ImageRecord.id.desc())


def apply_cursor(
    stmt: Select[Any], query: ImageQuery, sort_key: str, record_id: str
) -> Select[Any]:
    """Continue after a given (sort_key, id).

    Expressed as a row-value comparison so PostgreSQL can walk the composite
    index directly instead of filtering after the fact.
    """
    if query.sort == "oldest":
        return stmt.where(
            or_(
                ImageRecord.sort_key > sort_key,
                and_(ImageRecord.sort_key == sort_key, ImageRecord.id > record_id),
            )
        )
    return stmt.where(
        or_(
            ImageRecord.sort_key < sort_key,
            and_(ImageRecord.sort_key == sort_key, ImageRecord.id < record_id),
        )
    )
