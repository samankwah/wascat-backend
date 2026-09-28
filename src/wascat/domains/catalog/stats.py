"""Archive-wide statistics for the public Statistics page.

The same rules as the facets apply: only published, non-retired records count,
and closed sets - the 24 hours of the day, the five coverage bins - always emit
every member, zeros included, so a chart keeps its axis as the archive changes.

Several of these fields are empty today. No frame carries ``captured_at`` or a
location yet, so each open-ended breakdown says what it was built from
(``basis``) and falls back to something the archive does have rather than
returning nothing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, cast, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.domains.catalog.facets import _published
from wascat.domains.catalog.models import Collection, ImageRecord

COVERAGE_BINS = ("0-20%", "20-40%", "40-60%", "60-80%", "80-100%")


def _live() -> tuple[Any, Any]:
    return ImageRecord.retired_at.is_(None), _published()


def hour_buckets(counts: Mapping[int, int]) -> list[dict[str, int]]:
    """All 24 UTC hours, in order, with the missing ones at zero."""
    return [{"hour": hour, "count": counts.get(hour, 0)} for hour in range(24)]


def coverage_bins(counts: Mapping[int, int]) -> list[dict[str, Any]]:
    """The five 20% coverage bins, in order, with the missing ones at zero."""
    return [
        {"label": label, "count": counts.get(index, 0)} for index, label in enumerate(COVERAGE_BINS)
    ]


def cumulative(monthly: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    """Monthly counts, oldest first, with the running total alongside."""
    running = 0
    out = []
    for month, count in sorted(monthly):
        running += count
        out.append({"month": month, "count": count, "cumulative": running})
    return out


def _percent(part: int | Decimal | None, whole: int) -> float | None:
    if part is None or whole == 0:
        return None
    return round(float(part) * 100 / whole, 1)


async def _counts_by(session: AsyncSession, column: Any, *where: Any) -> dict[Any, int]:
    # Grouped by the output name rather than the expression: an expression
    # with bound parameters renders them twice, and Postgres then refuses to
    # match the two copies.
    stmt = (
        select(column.label("key"), func.count(ImageRecord.id))
        .where(*_live(), column.is_not(None), *where)
        .group_by(literal_column("key"))
    )
    return {key: count for key, count in (await session.execute(stmt)).all()}


async def _collection_counts(session: AsyncSession) -> list[dict[str, Any]]:
    """Records per collection, labelled the way the facets label them."""
    stmt = (
        select(
            Collection.position,
            Collection.slug,
            Collection.title,
            Collection.location_name,
            func.count(ImageRecord.id),
        )
        .join(ImageRecord, ImageRecord.collection_id == Collection.id)
        .where(*_live())
        .group_by(Collection.position, Collection.slug, Collection.title, Collection.location_name)
        .order_by(Collection.position, Collection.slug)
    )
    rows = [
        {"value": slug, "label": title or location_name or slug, "title": title, "count": count}
        for _, slug, title, location_name, count in (await session.execute(stmt)).all()
    ]
    # Several sequences share a cloud type; repeated labels carry their slug so
    # a bar chart does not show three identical, unexplained bars.
    repeated = Counter(row["label"] for row in rows)
    for row in rows:
        if repeated[row["label"]] > 1:
            row["label"] = f"{row['label']} · {row['value']}"
    return rows


async def build_stats(session: AsyncSession) -> dict[str, Any]:
    total, measured, mean_fraction, segmented, timestamped = (
        await session.execute(
            select(
                func.count(ImageRecord.id),
                func.count(ImageRecord.cloud_fraction),
                func.avg(ImageRecord.cloud_fraction),
                func.count(ImageRecord.id).filter(ImageRecord.has_mask.is_(True)),
                func.count(ImageRecord.captured_at),
            ).where(*_live())
        )
    ).one()

    collections = await _collection_counts(session)

    # -- where ------------------------------------------------------------
    location_counts = await _counts_by(session, ImageRecord.location_label)
    if location_counts:
        location_basis = "locations"
        by_location = [
            {"value": value, "label": value, "count": count}
            for value, count in sorted(location_counts.items())
        ]
    else:
        location_basis = "collections"
        by_location = [
            {"value": row["value"], "label": row["label"], "count": row["count"]}
            for row in collections
        ]

    # -- what -------------------------------------------------------------
    # Two sources: the frame-level sky class, and the cloud type each
    # collection is named after. Whichever labels more of the archive wins;
    # a handful of classified frames would otherwise hide every other type.
    sky_counts: Counter[str] = Counter(await _counts_by(session, ImageRecord.sky_class_label))
    titled: Counter[str] = Counter()
    for row in collections:
        if row["title"]:
            titled[row["title"]] += row["count"]
    if sky_counts.total() >= titled.total() and sky_counts:
        cloud_type_basis, type_counts = "skyClasses", sky_counts
    else:
        cloud_type_basis, type_counts = "collections", titled
    cloud_types = [
        {"label": label, "count": count}
        for label, count in sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))
    ]

    # -- when -------------------------------------------------------------
    hour = cast(func.extract("hour", func.timezone("UTC", ImageRecord.captured_at)), Integer)
    hour_counts = await _counts_by(session, hour)

    # Capture month where known, ingest month otherwise, so growth has a
    # timeline before any capture timestamps arrive.
    month = func.to_char(
        func.date_trunc(
            "month",
            func.timezone("UTC", func.coalesce(ImageRecord.captured_at, ImageRecord.created_at)),
        ),
        "YYYY-MM",
    )
    month_counts = await _counts_by(session, month)

    # -- how cloudy -------------------------------------------------------
    # floor(fraction * 5) puts 1.0 in a sixth bin; least() folds it into the
    # last one, which is closed at 100%. least() also ignores NULL - it would
    # file every unmeasured frame under 80-100% - hence the explicit filter.
    coverage_bin = cast(func.least(func.floor(ImageRecord.cloud_fraction * 5), 4), Integer)
    coverage_counts = await _counts_by(
        session, coverage_bin, ImageRecord.cloud_fraction.is_not(None)
    )

    return {
        "totals": {
            "images": total,
            "sites": len(by_location),
            "sitesBasis": location_basis,
            "processedPct": _percent(segmented, total),
            "avgCoveragePct": (
                round(float(mean_fraction) * 100, 1) if mean_fraction is not None else None
            ),
            "measured": measured,
        },
        "byLocation": {"basis": location_basis, "items": by_location},
        "cloudTypes": {"basis": cloud_type_basis, "items": cloud_types},
        "byHourUtc": {"hasTimestamps": timestamped > 0, "items": hour_buckets(hour_counts)},
        "coverageHistogram": coverage_bins(coverage_counts),
        "growth": {
            "basis": "capturedAt" if timestamped == total and total else "capturedOrIngested",
            "items": cumulative(month_counts.items()),
        },
    }
