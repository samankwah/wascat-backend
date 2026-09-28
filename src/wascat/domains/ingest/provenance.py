"""Applying the per-sequence provenance file to records that already exist.

``seed/provenance.json`` holds what the capture team knows about each
sequence: the station, its coordinates, when frame 0 was taken and how far
apart frames are. The seed import only copies the station onto the
collection; this puts it on every frame as well, and derives each frame's
capture time as ``startedAt + frameIndex * frameIntervalSeconds``.

Only the provenance columns are written - capture time, place, coordinates,
instrument. Measurements, artifacts and the observer's labels are untouched,
which is what makes it safe to apply to a published release.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.domains.catalog.models import Collection, ImageRecord


class ProvenanceError(ValueError):
    """The provenance file is malformed for a sequence it claims to describe."""


@dataclass(frozen=True)
class SequenceProvenance:
    sequence_id: str
    station: str | None
    site: str | None
    latitude: Decimal | None
    longitude: Decimal | None
    started_at: datetime | None
    interval: timedelta | None
    instrument: str | None

    def captured_at(self, frame_index: int) -> datetime | None:
        # Both are needed: a start time alone would stamp every frame with it.
        if self.started_at is None or self.interval is None:
            return None
        return self.started_at + frame_index * self.interval


@dataclass
class ProvenanceReport:
    sequences: list[str] = field(default_factory=list)
    records: int = 0
    collections: int = 0

    def summary(self) -> str:
        return (
            f"{self.records:,} records and {self.collections} collections updated "
            f"across {len(self.sequences)} sequences."
        )


def _decimal(value: Any) -> Decimal | None:
    return None if value is None or value == "" else Decimal(str(value))


def _moment(sequence_id: str, value: Any) -> datetime | None:
    if not value:
        return None
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ProvenanceError(f"{sequence_id}: startedAt must carry a UTC offset, got {value!r}")
    return moment


def parse_provenance(path: Path) -> list[SequenceProvenance]:
    """Every sequence in the file that supplies at least one provenance field."""
    document = json.loads(path.read_text(encoding="utf-8"))
    parsed: list[SequenceProvenance] = []
    for sequence_id, meta in (document.get("sequences") or {}).items():
        interval = meta.get("frameIntervalSeconds")
        if interval is not None and float(interval) <= 0:
            raise ProvenanceError(f"{sequence_id}: frameIntervalSeconds must be positive")
        latitude, longitude = _decimal(meta.get("latitude")), _decimal(meta.get("longitude"))
        if (latitude is None) != (longitude is None):
            raise ProvenanceError(f"{sequence_id}: latitude and longitude come as a pair")
        entry = SequenceProvenance(
            sequence_id=sequence_id,
            station=meta.get("station") or None,
            site=meta.get("site") or None,
            latitude=latitude,
            longitude=longitude,
            started_at=_moment(sequence_id, meta.get("startedAt")),
            interval=None if interval is None else timedelta(seconds=float(interval)),
            instrument=meta.get("instrument") or None,
        )
        if any(
            value is not None
            for value in (
                entry.station,
                entry.site,
                entry.latitude,
                entry.started_at,
                entry.instrument,
            )
        ):
            parsed.append(entry)
    return parsed


async def apply_provenance(
    session: AsyncSession,
    sequences: list[SequenceProvenance],
    *,
    dry_run: bool = False,
) -> ProvenanceReport:
    """Write each sequence's provenance onto its records and its collection.

    A field the file leaves empty is left as it is in the database rather than
    cleared, so a curator's edit made in the dashboard survives a re-run.
    """
    report = ProvenanceReport()
    if not dry_run:
        # SET LOCAL, so it lasts exactly as long as this transaction.
        await session.execute(text("SET LOCAL wascat.allow_published_writes = 'on'"))

    for entry in sequences:
        records = (
            (
                await session.execute(
                    select(ImageRecord).where(ImageRecord.sequence_id == entry.sequence_id)
                )
            )
            .scalars()
            .all()
        )
        if not records:
            continue
        report.sequences.append(entry.sequence_id)
        report.records += len(records)
        if dry_run:
            continue

        for record in records:
            captured = entry.captured_at(record.frame_index)
            if captured is not None:
                record.captured_at = captured
            if entry.site:
                record.location_label = entry.site
            if entry.latitude is not None:
                record.latitude = entry.latitude
                record.longitude = entry.longitude
            if entry.instrument:
                record.instrument = entry.instrument

        for collection_id in {record.collection_id for record in records}:
            collection = await session.get(Collection, collection_id)
            if collection is None:
                continue
            if entry.station or entry.site:
                collection.location_name = entry.station or entry.site
            if entry.latitude is not None:
                collection.latitude = entry.latitude
                collection.longitude = entry.longitude
            if entry.instrument:
                collection.instrument = entry.instrument
            report.collections += 1

    await session.flush()
    return report
