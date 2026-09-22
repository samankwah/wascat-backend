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

import uuid
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from wascat.core.config import get_settings
from wascat.core.jsformat import bytes_to_size, iso_z, js_number, locale_int, okta_label

if TYPE_CHECKING:
    from wascat.domains.catalog.models import (
        Artifact,
        Collection,
        ImageClassPrediction,
        ImageRecord,
        Release,
    )

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
    ``/frames/seq-001/5-source.jpg``, byte-identical to what the bundled
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


def sequence_ordinal(sequence_id: str) -> int:
    """The trailing ordinal of a sequence id, mirroring what the trigger stores."""
    return int(sequence_id[-3:])


def sequence_label(sequence_id: str) -> str:
    """The reader-facing name of a capture sequence: "seq-001" -> "01".

    The identifier is a filter token and a URL segment. It does not belong in a
    heading, a sentence or alt text, so every human-facing string is built from
    this instead. Two digits minimum, more once the archive passes ninety-nine.
    """
    return f"{sequence_ordinal(sequence_id):02d}"


def build_alt(*, frame_index: int, sequence_id: str, oktas: int | None, has_source: bool) -> str:
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
        label = sequence_label(sequence_id)
        return f"All-sky camera frame {frame_index} of sequence {label}{measurement}."
    return (
        f"Binary cloud segmentation mask for frame {frame_index} "
        f"of sequence {sequence_label(sequence_id)}, "
        f"measured at {okta_label(oktas or 0)} cloud cover."
    )


def build_tags(
    *, sequence_id: str, oktas: int | None, has_source: bool, has_mask: bool
) -> list[str]:
    """Filter tokens, not prose - so tags[0] stays the raw sequence id."""
    if has_source and has_mask:
        pairing = "source + mask"
    elif has_mask:
        pairing = "mask only"
    else:
        pairing = "source only"
    return [sequence_id, "unsegmented" if oktas is None else okta_label(oktas), pairing]


def build_sort_key(*, captured_at: Any, sequence_number: int, frame_index: int) -> str:
    """Real timestamp when known, sequence position otherwise.

    Both forms are fixed width so they sort byte-wise, and an ISO timestamp
    starts with '2' while a sequence key starts with '0', so timestamped
    records sort above untimestamped ones under any collation. See risk R6.
    """
    if captured_at is not None:
        return iso_z(captured_at)
    return f"{sequence_number:03d}-{frame_index:07d}"


def record_predictions(record: ImageRecord) -> list[dict[str, Any]]:
    """Every model's full probability vector for this frame, ranked.

    One entry per model, each carrying every class that model scored - not a
    winning label. A sky holds several genera at once, so the top-1 is the
    least interesting thing a classifier says about an all-sky frame, and a
    reader comparing the model against the observer's own reading needs the
    runners-up to do it.

    Classes sort by probability descending, then by label so a tie is stable.
    Models sort by their curated position.
    """
    by_model: dict[uuid.UUID, list[ImageClassPrediction]] = defaultdict(list)
    for prediction in record.predictions:
        by_model[prediction.model_id].append(prediction)

    entries: list[tuple[int, str, dict[str, Any]]] = []
    for predictions in by_model.values():
        model = predictions[0].model
        if model.retired_at is not None:
            continue
        ranked = sorted(predictions, key=lambda p: (-p.probability, p.sky_class_label))
        entries.append(
            (
                model.position,
                model.slug,
                {
                    "model": {
                        "slug": model.slug,
                        "name": model.name,
                        "version": model.version,
                        **({"description": model.description} if model.description else {}),
                    },
                    "classes": [
                        {
                            "skyClass": p.sky_class_label,
                            "probability": js_number(p.probability),
                        }
                        for p in ranked
                    ],
                },
            )
        )
    return [entry for _, _, entry in sorted(entries, key=lambda e: (e[0], e[1]))]


def record_to_json(
    record: ImageRecord,
    *,
    collection_slug: str,
    release_version: str,
    include_predictions: bool = False,
) -> dict[str, Any]:
    """Serialise one image record.

    Optional fields are omitted rather than emitted as null. An unmeasured
    frame has no cloud cover to report, and reporting `null` would invite a
    client to render it as zero - which is precisely the clear sky the archive
    must never invent.

    `include_predictions` is off by default because the list endpoints do not
    render probability vectors: switching it on there would load and serialise
    one row per class per frame per model on every Explore page. The
    single-record read turns it on and loads `predictions` eagerly to match.
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
        "sequenceId": record.sequence_id,
        "frameIndex": record.frame_index,
    }

    # Fraction and bucket appear together or not at all.
    if record.cloud_fraction is not None:
        payload["cloudFraction"] = js_number(record.cloud_fraction)
        payload["cloudCoverOktas"] = oktas
    # The observer's own count, which travels with the cloud genus in the
    # label table. Independent of the measured pair above: a frame can have
    # one, the other, both or neither, and the two are never reconciled.
    if record.observed_cloud_cover_oktas is not None:
        payload["observedCloudCoverOktas"] = record.observed_cloud_cover_oktas

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
        sequence_id=record.sequence_id,
        oktas=oktas,
        has_source=source is not None,
    )
    payload["tags"] = build_tags(
        sequence_id=record.sequence_id,
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
    if record.sky_class_label:
        payload["skyClass"] = record.sky_class_label
    if include_predictions:
        predictions = record_predictions(record)
        if predictions:
            payload["predictions"] = predictions
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
    *, images: int, segmented: int, with_source: int, sequence_names: str
) -> str:
    mask_only = images - with_source
    source_only = images - segmented

    text = (
        f"{locale_int(images)} all-sky frames from capture sequence {sequence_names}. "
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
    sequence_ids: list[str],
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
    sequence_names = ", ".join(sequence_label(s) for s in sequence_ids)
    location_name = collection.location_name

    payload: dict[str, Any] = {
        "slug": collection.slug,
        "title": location_name or f"Capture sequence {sequence_names}",
        "shortTitle": location_name or f"Sequence {sequence_names}",
        "kicker": f"ALL-SKY CLOUD SEGMENTATION · SEQUENCE {sequence_names}",
        "description": build_collection_description(
            images=images,
            segmented=segmented,
            with_source=with_source,
            sequence_names=sequence_names,
        ),
        "coverage": build_coverage(
            min_frame=min_frame, max_frame=max_frame, segmented=segmented, images=images
        ),
        "sequenceIds": sequence_ids,
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
            {"sequenceId": sequence_id, "scale": js_number(scale), "corrected": scale > 1}
            for sequence_id, scale in (mask_registration or [])
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
