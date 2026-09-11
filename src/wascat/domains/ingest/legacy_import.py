"""Import the generated catalogue into PostgreSQL and object storage.

This is the fast path onto the new stack. It reads what the TypeScript
pipeline already produced - ``seed/catalog.generated.json`` plus the frame
JPEGs it copied into ``public/frames`` - and loads it verbatim.

It deliberately **recomputes nothing**. Every cloud fraction, okta bucket and
mask scale is copied across exactly as published. The measurements are the
output of a specific toolchain (libvips' greyscale conversion, a particular
JPEG decoder), and re-deriving them in Python could shift a value by a pixel's
worth of luma without anyone noticing. Numbers the archive has already
published must not change because the storage moved. Re-measuring is a
separate, explicit operation that produces a new release.

What it does verify is identity: every file's SHA-256 is recomputed and
compared against the checksum in the catalogue before it is uploaded, so a
corrupted or substituted frame stops the import rather than entering the
archive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from anyio import to_thread
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from wascat.domains.catalog.models import (
    Artifact,
    Collection,
    ImageRecord,
    Release,
    ReleaseStatus,
)
from wascat.domains.ingest.models import SequenceReference
from wascat.storage.base import IMMUTABLE_CACHE_CONTROL, ObjectStore

# Uploading 3,438 small objects one at a time is dominated by round-trip
# latency; a modest amount of concurrency turns minutes into seconds without
# overwhelming a local MinIO.
UPLOAD_CONCURRENCY = 32
CHUNK = 1000


@dataclass
class ImportReport:
    collections: int = 0
    releases: int = 0
    records: int = 0
    artifacts: int = 0
    uploaded: int = 0
    skipped: int = 0
    bytes_uploaded: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.collections} collections, {self.releases} releases, "
            f"{self.records} records, {self.artifacts} artifacts; "
            f"{self.uploaded} objects uploaded ({self.bytes_uploaded / 1024**2:.1f} MB), "
            f"{self.skipped} already present"
        )


class ChecksumMismatchError(RuntimeError):
    """A frame on disk is not the frame the catalogue recorded."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_json_if_present(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


async def _upload_artifact(
    store: ObjectStore,
    frames_root: Path,
    artifact: dict[str, Any],
    report: ImportReport,
    semaphore: asyncio.Semaphore,
) -> None:
    key = artifact["object_key"]
    async with semaphore:
        existing = await store.head(key)
        if existing is not None and existing.size == artifact["bytes"]:
            report.skipped += 1
            return

        # objectKey is always "frames/seq-NNN/<file>", and frames_root points at
        # the directory holding those sequence folders.
        source = frames_root / key[len("frames/") :]
        if not await to_thread.run_sync(source.exists):
            raise FileNotFoundError(f"Missing frame for {key}: looked in {source}")

        data = await to_thread.run_sync(source.read_bytes)
        checksum = await to_thread.run_sync(hashlib.sha256, data)
        if checksum.hexdigest() != artifact["checksum"]:
            raise ChecksumMismatchError(
                f"{source} does not match the checksum recorded for {key}. "
                "The catalogue and the imagery have diverged; import aborted."
            )

        await store.put(
            key,
            data,
            content_type=artifact["media_type"],
            cache_control=IMMUTABLE_CACHE_CONTROL,
        )
        report.uploaded += 1
        report.bytes_uploaded += len(data)


async def upload_frames(
    store: ObjectStore,
    frames_root: Path,
    artifacts: Iterable[dict[str, Any]],
    report: ImportReport,
) -> None:
    semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)
    await asyncio.gather(
        *(
            _upload_artifact(store, frames_root, artifact, report, semaphore)
            for artifact in artifacts
        )
    )


def _batched(items: list[Any], size: int) -> Iterable[list[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def import_catalogue(
    session: AsyncSession,
    *,
    catalog_path: Path,
    provenance_path: Path | None = None,
    frames_root: Path | None = None,
    store: ObjectStore | None = None,
    publish: bool = True,
    replace: bool = False,
) -> ImportReport:
    """Load the generated catalogue.

    The release is created as a DRAFT and only published at the end. That is
    not ceremony: published releases are immutable at the database level, so
    importing straight into a published release would be rejected by the very
    rule the archive depends on. Building as a draft and publishing once
    complete is also what an operator does by hand.
    """
    report = ImportReport()

    # The catalogue is ~2.7 MB; parsing it on the event loop would stall every
    # other task for the duration.
    catalogue = await to_thread.run_sync(_read_json, catalog_path)
    provenance: dict[str, Any] = {}
    if provenance_path is not None:
        provenance = await to_thread.run_sync(_read_json_if_present, provenance_path)
    sequences: dict[str, Any] = provenance.get("sequences", {}) or {}

    release_version: str = catalogue["release"]
    generated_at = _parse_iso(catalogue.get("generatedAt"))
    mask_registration: dict[str, Any] = catalogue.get("maskRegistration", {}) or {}

    if replace:
        await _clear_catalogue(session)

    # -- collections and draft releases ------------------------------------
    releases_by_slug: dict[str, Release] = {}
    for index, entry in enumerate(catalogue["collections"]):
        slug = entry["slug"]
        collection = (
            (await session.execute(select(Collection).where(Collection.slug == slug)))
            .scalars()
            .first()
        )
        if collection is None:
            collection = Collection(slug=slug)
            session.add(collection)
            report.collections += 1

        # Editorial fields are copied only when the catalogue or the
        # provenance file (below) supplies them. Left null, that's exactly
        # the gap the dashboard exists to close.
        collection.title = entry.get("title")
        # Ordering comes from the catalogue. Zero-padded slugs sort correctly
        # on their own now, but position stays explicit so a curator can group
        # collections by site rather than by capture order.
        collection.position = index
        for source_key, attribute in (
            ("locationName", "location_name"),
            ("instrument", "instrument"),
            ("license", "license"),
            ("citation", "citation"),
            ("doi", "doi"),
        ):
            if entry.get(source_key):
                setattr(collection, attribute, entry[source_key])
        coordinates = entry.get("coordinates")
        if coordinates:
            collection.latitude = Decimal(str(coordinates["latitude"]))
            collection.longitude = Decimal(str(coordinates["longitude"]))

        # The generated catalogue rarely carries these - they come from the
        # per-sequence provenance file instead, keyed by sequence rather than
        # by collection. Fall back to the first of this collection's
        # sequences that has one set, and only where the catalogue didn't
        # already supply a value.
        for sequence_id in entry.get("sequenceIds", []):
            sequence_meta = sequences.get(sequence_id) or {}
            if not collection.location_name and sequence_meta.get("site"):
                collection.location_name = sequence_meta["site"]
            if not collection.instrument and sequence_meta.get("instrument"):
                collection.instrument = sequence_meta["instrument"]
            if (
                collection.latitude is None
                and sequence_meta.get("latitude") is not None
                and sequence_meta.get("longitude") is not None
            ):
                collection.latitude = Decimal(str(sequence_meta["latitude"]))
                collection.longitude = Decimal(str(sequence_meta["longitude"]))

        await session.flush()

        release = (
            (
                await session.execute(
                    select(Release).where(
                        Release.collection_id == collection.id,
                        Release.version == release_version,
                    )
                )
            )
            .scalars()
            .first()
        )
        if release is None:
            release = Release(
                collection_id=collection.id,
                version=release_version,
                status=ReleaseStatus.DRAFT,
                meta={
                    "importedFrom": catalog_path.name,
                    "generatedAt": catalogue.get("generatedAt"),
                },
            )
            session.add(release)
            report.releases += 1
        else:
            # Re-importing into an existing release only makes sense while it
            # is still a draft; the trigger would refuse anything else.
            release.status = ReleaseStatus.DRAFT
            release.current = False
        await session.flush()
        releases_by_slug[slug] = release

    # -- per-sequence reference data ---------------------------------------
    for sequence_id, registration in mask_registration.items():
        reference = (
            (
                await session.execute(
                    select(SequenceReference).where(SequenceReference.sequence_id == sequence_id)
                )
            )
            .scalars()
            .first()
        )
        if reference is None:
            reference = SequenceReference(sequence_id=sequence_id, reference_frame_index=0)
            session.add(reference)
        reference.mask_scale = Decimal(str(registration.get("scale", 1)))
        reference.corrected = bool(registration.get("corrected", False))
        outside = registration.get("cloudOutsideFieldOfView")
        reference.cloud_outside_field_of_view = (
            Decimal(str(round(float(outside), 6))) if outside is not None else None
        )
        sequence_meta = sequences.get(sequence_id) or {}
        reference.frames = sequence_meta.get("frames")
        reference.geometry = sequence_meta.get("geometry")
    await session.flush()

    # -- records and artifacts ---------------------------------------------
    images: list[dict[str, Any]] = catalogue["images"]
    record_rows: list[dict[str, Any]] = []
    artifact_rows: list[dict[str, Any]] = []

    for image in images:
        slug = image["collection"]
        release = releases_by_slug[slug]
        fraction = image.get("cloudFraction")
        # The catalogue has never carried a per-image skyClass - it comes from
        # the per-sequence provenance file instead, same as site/instrument
        # above. It is a curator's read of the sequence's dominant condition,
        # not a per-frame measurement, so every frame in the sequence shares it.
        sequence_meta = sequences.get(image["sequenceId"]) or {}
        record_rows.append(
            {
                "id": image["id"],
                "release_id": release.id,
                "collection_id": release.collection_id,
                "sequence_id": image["sequenceId"],
                "frame_index": image["frameIndex"],
                # str() first: Decimal(float) would carry the float's noise
                # into an exact column.
                "cloud_fraction": None if fraction is None else Decimal(str(fraction)),
                "cloud_cover_oktas": image.get("cloudCoverOktas"),
                "mask_scale": Decimal(str(image.get("maskScale", 1))),
                "width": image["width"],
                "height": image["height"],
                "captured_at": _parse_iso(image.get("capturedAt")),
                "instrument": image.get("instrument"),
                "location_label": image.get("location"),
                "season_label": image.get("season"),
                "time_of_day_label": image.get("timeOfDay"),
                "sky_class_label": image.get("skyClass") or sequence_meta.get("skyClass"),
                "condition_tags": [],
                "provenance": image.get("provenance", {}),
            }
        )
        for artifact in image["artifacts"]:
            artifact_rows.append(
                {
                    "image_id": image["id"],
                    "type": artifact["type"],
                    "media_type": artifact["mediaType"],
                    "object_key": artifact["objectKey"],
                    "checksum": artifact["checksum"],
                    "bytes": artifact["bytes"],
                    "width": artifact.get("width"),
                    "height": artifact.get("height"),
                }
            )

    # -- object storage, before the rows that point at it ------------------
    if store is not None and frames_root is not None:
        await upload_frames(store, frames_root, artifact_rows, report)

    for batch in _batched(record_rows, CHUNK):
        await session.execute(insert(ImageRecord), batch)
        report.records += len(batch)
    for batch in _batched(artifact_rows, CHUNK):
        await session.execute(insert(Artifact), batch)
        report.artifacts += len(batch)

    # The deferred invariant triggers run here rather than silently at commit,
    # so a bad import fails inside this function with a legible message.
    await session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    if publish:
        published_at = generated_at or datetime.now(UTC)
        for release in releases_by_slug.values():
            release.status = ReleaseStatus.PUBLISHED
            release.published_at = published_at
            release.current = True
        await session.flush()

    for slug, release in releases_by_slug.items():
        count = (
            await session.execute(
                select(func.count(ImageRecord.id)).where(ImageRecord.release_id == release.id)
            )
        ).scalar_one()
        if count == 0:
            report.warnings.append(f"{slug}: release {release.version} has no records")

    return report


async def _clear_catalogue(session: AsyncSession) -> None:
    """Remove the existing catalogue so an import starts from a clean slate."""
    # Published releases are immutable, so the guard has to be lifted
    # explicitly for a deliberate rebuild. SET LOCAL keeps it to this
    # transaction.
    await session.execute(text("SET LOCAL wascat.allow_published_writes = 'on'"))
    await session.execute(delete(Artifact))
    await session.execute(delete(ImageRecord))
    await session.execute(delete(Release))
    await session.execute(delete(Collection))
    await session.flush()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
