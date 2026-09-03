"""Row -> public JSON.

CONTRACT - DO NOT TIDY.

Every string built here is rendered directly by the site and pinned by the
recorded fixtures in tests/fixtures/contract. That includes things that look
like incidental prose: the en dash in "Frames 2-3,611", the middle dot in the
kicker, the comma grouping, the leading space before "A further ...", and the
exact clause that appears only when a sequence has mask-only records.

These derivations came from ``toRecord`` and ``toCollection`` in
lib/catalog.ts. They live server-side now because the public API already
returned them, so moving them would have changed the payload.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from wascat.core.config import get_settings
from wascat.core.jsformat import bytes_to_size, iso_z, js_number, locale_int, okta_label

if TYPE_CHECKING:
    from wascat.domains.catalog.models import Artifact, Collection, ImageRecord, Release

# The public `artifacts[]` array carries the frame and its mask only.
# Derivatives are additive rows used by the dashboard and would change
# `collection.artifacts` from 3,438 to something else. See risk R12.
PUBLIC_ARTIFACT_TYPES = ("source", "mask")

# Source before mask, matching the order the generator wrote them in. Sorting
# by `type` alphabetically would put mask first and silently reorder the array.
_ARTIFACT_RANK = {"source": 0, "mask": 1}


def public_url(object_key: str) -> str:
    """Absolute URL for a stored object.

    With an empty ``PUBLIC_ASSET_BASE_URL`` this renders
    ``/frames/vid1/5-source.jpg``, byte-identical to what the bundled
    catalogue served. Production points it at a CDN.
    """
    base = get_settings().public_asset_base_url
    return f"{base}/{object_key}" if base else f"/{object_key}"


def artifact_to_json(artifact: Artifact) -> dict[str, Any]:
    return {
        "type": artifact.type,
        "url": public_url(artifact.object_key),
        "objectKey": artifact.object_key,
        "bytes": artifact.bytes,
        "checksum": artifact.checksum,
        "mediaType": artifact.media_type,
    }


def _public_artifacts(record: ImageRecord) -> list[Artifact]:
    return sorted(
        (a for a in record.artifacts if a.type in PUBLIC_ARTIFACT_TYPES),
        key=lambda a: _ARTIFACT_RANK[a.type],
    )


def build_alt(*, frame_index: int, video_id: str, oktas: int | None, has_source: bool) -> str:
    """Alt text for the primary image.

    A frame and a bare mask are described differently, because what the reader
    is looking at is different. A mask always has a measurement - that is what
    a mask is - so only the source branch has an unsegmented case.
    """
    if has_source:
        measurement = (
            ", not yet segmented"
            if oktas is None
            else f", measured at {okta_label(oktas)} cloud cover"
        )
        return f"All-sky camera frame {frame_index} of sequence {video_id}{measurement}."
    return (
        f"Binary cloud segmentation mask for frame {frame_index} "
        f"of sequence {video_id}, measured at {okta_label(oktas or 0)} cloud cover."
    )


def build_tags(*, video_id: str, oktas: int | None, has_source: bool, has_mask: bool) -> list[str]:
    if has_source and has_mask:
        pairing = "source + mask"
    elif has_mask:
        pairing = "mask only"
    else:
        pairing = "source only"
    return [video_id, "unsegmented" if oktas is None else okta_label(oktas), pairing]


def build_sort_key(*, captured_at: Any, video_number: int, frame_index: int) -> str:
    """Real timestamp when known, sequence position otherwise.

    Both forms are fixed width so they sort byte-wise, and an ISO timestamp
    starts with '2' while a sequence key starts with '0', so timestamped
    records sort above untimestamped ones under any collation. See risk R6.
    """
    if captured_at is not None:
        return iso_z(captured_at)
    return f"{video_number:03d}-{frame_index:07d}"


def record_to_json(
    record: ImageRecord, *, collection_slug: str, release_version: str
) -> dict[str, Any]:
    """Serialise one image record.

    Optional fields are omitted rather than emitted as null. An unmeasured
    frame has no cloud cover to report, and reporting `null` would invite a
    client to render it as zero - which is precisely the clear sky the archive
    must never invent.
    """
    artifacts = _public_artifacts(record)
    by_type = {artifact.type: artifact for artifact in artifacts}
    source = by_type.get("source")
    mask = by_type.get("mask")
    source_url = public_url(source.object_key) if source else None
    mask_url = public_url(mask.object_key) if mask else None
    oktas = record.cloud_cover_oktas

    payload: dict[str, Any] = {
        "id": record.id,
        "collection": collection_slug,
        "release": release_version,
        "videoId": record.video_id,
        "frameIndex": record.frame_index,
    }

    # Fraction and bucket appear together or not at all.
    if record.cloud_fraction is not None:
        payload["cloudFraction"] = js_number(record.cloud_fraction)
        payload["cloudCoverOktas"] = oktas

    payload["maskScale"] = js_number(record.mask_scale)
    payload["width"] = record.width
    payload["height"] = record.height
    # Primary listing image: the source frame when delivered, else the mask.
    payload["image"] = source_url or mask_url
    if source_url is not None:
        payload["sourceUrl"] = source_url
    if mask_url is not None:
        payload["maskUrl"] = mask_url
    payload["hasSource"] = source is not None
    payload["hasMask"] = mask is not None
    payload["alt"] = build_alt(
        frame_index=record.frame_index,
        video_id=record.video_id,
        oktas=oktas,
        has_source=source is not None,
    )
    payload["tags"] = build_tags(
        video_id=record.video_id,
        oktas=oktas,
        has_source=source is not None,
        has_mask=mask is not None,
    )
    payload["artifacts"] = [artifact_to_json(artifact) for artifact in artifacts]
    payload["sortKey"] = record.sort_key

    # Provenance, present only once the capture team has supplied it.
    if record.captured_at is not None:
        payload["capturedAt"] = iso_z(record.captured_at)
    if record.location_label:
        payload["location"] = record.location_label
    if record.latitude is not None and record.longitude is not None:
        payload["coordinates"] = {
            "latitude": js_number(record.latitude),
            "longitude": js_number(record.longitude),
        }
    if record.season_label:
        payload["season"] = record.season_label
    if record.time_of_day_label:
        payload["timeOfDay"] = record.time_of_day_label
    if record.instrument:
        payload["instrument"] = record.instrument

    return payload


def release_to_json(release: Release, *, images: int, total_bytes: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": release.version,
        "images": images,
        "size": bytes_to_size(total_bytes),
        "current": release.current,
    }
    if release.published_at is not None:
        payload["publishedAt"] = iso_z(release.published_at)
    return payload


def build_collection_description(
    *, images: int, segmented: int, with_source: int, sequence_label: str
) -> str:
    mask_only = images - with_source
    source_only = images - segmented

    text = (
        f"{locale_int(images)} all-sky frames from capture sequence {sequence_label}. "
        f"{locale_int(segmented)} carry a binary cloud mask and a measured cloud cover"
    )
    if mask_only > 0:
        text += f", {locale_int(mask_only)} of them without the source frame"
    text += "."
    if source_only > 0:
        text += (
            f" A further {locale_int(source_only)} frames are sampled evenly from "
            "the rest of the sequence and have not been segmented yet."
        )
    return text


def build_coverage(
    *, min_frame: int | None, max_frame: int | None, segmented: int, images: int
) -> str:
    """Frame range and segmentation ratio. The dash is an en dash (U+2013) and
    the separator is a middle dot (U+00B7)."""
    if min_frame is None or max_frame is None:
        return "No frames"
    # The en dash and middle dot are deliberate: they are what the site
    # renders and what the recorded fixtures contain. Replacing them with
    # ASCII would be a visible change to every collection page.
    return (
        f"Frames {locale_int(min_frame)}–{locale_int(max_frame)} · "
        f"{locale_int(segmented)} of {locale_int(images)} segmented"
    )


def collection_to_json(
    collection: Collection,
    *,
    video_ids: list[str],
    images: int,
    artifacts: int,
    with_source: int,
    segmented: int,
    min_frame: int | None,
    max_frame: int | None,
    cover_image: str | None,
    cover_alt: str | None,
    releases: list[dict[str, Any]],
    mean_oktas: float | None = None,
    mask_registration: list[tuple[str, float]] | None = None,
) -> dict[str, Any]:
    sequence_label = ", ".join(video_ids)
    location_name = collection.location_name

    payload: dict[str, Any] = {
        "slug": collection.slug,
        "title": location_name or f"Capture sequence {sequence_label}",
        "shortTitle": location_name or sequence_label,
        "kicker": f"ALL-SKY CLOUD SEGMENTATION · {sequence_label.upper()}",
        "description": build_collection_description(
            images=images,
            segmented=segmented,
            with_source=with_source,
            sequence_label=sequence_label,
        ),
        "coverage": build_coverage(
            min_frame=min_frame, max_frame=max_frame, segmented=segmented, images=images
        ),
        "videoIds": video_ids,
        "images": images,
        "artifacts": artifacts,
        "withSource": with_source,
        "segmented": segmented,
        "image": cover_image or "",
        "imageAlt": cover_alt or "",
        "releases": releases,
        # Added by the migration. The collection page used to average the whole
        # in-memory catalogue to get this; the database can do it in the same
        # query that produces the counts.
        "meanCloudCoverOktas": None if mean_oktas is None else js_number(round(mean_oktas, 6)),
        # Which sequences had their masks delivered at a different scale from
        # the frames they segment. Masks are stored exactly as delivered; the
        # viewer scales the overlay back so the two line up.
        "maskRegistration": [
            {"videoId": video_id, "scale": js_number(scale), "corrected": scale > 1}
            for video_id, scale in (mask_registration or [])
        ],
    }

    if location_name:
        payload["locationName"] = location_name
        if collection.latitude is not None and collection.longitude is not None:
            payload["location"] = (
                f"{location_name} · {js_number(collection.latitude)}, "
                f"{js_number(collection.longitude)}"
            )
        else:
            payload["location"] = location_name
    if collection.latitude is not None and collection.longitude is not None:
        payload["coordinates"] = {
            "latitude": js_number(collection.latitude),
            "longitude": js_number(collection.longitude),
        }
    if collection.instrument:
        payload["instrument"] = collection.instrument
    if collection.license:
        payload["license"] = collection.license
    if collection.citation:
        payload["citation"] = collection.citation
    if collection.doi:
        payload["doi"] = collection.doi

    return payload
