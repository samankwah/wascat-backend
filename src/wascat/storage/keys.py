"""Object key templates.

Every key in the system is built here, so the layout is one edit rather than
several. Keys carry the sequence directory - ``frames/seq-001/5-source.jpg`` -
and the seed uploads to exactly the paths the regenerated catalogue records,
so what the site serves and what the object store holds cannot drift apart.
"""

from __future__ import annotations

from typing import Literal

ArtifactType = Literal["source", "mask", "thumbnail", "webp"]

FRAMES_PREFIX = "frames"
DERIVATIVES_PREFIX = "derivatives"
UPLOADS_PREFIX = "uploads/staging"
BUNDLES_PREFIX = "bundles"


def frame_key(sequence_id: str, frame_index: int, artifact_type: Literal["source", "mask"]) -> str:
    """e.g. frames/seq-001/5-source.jpg"""
    return f"{FRAMES_PREFIX}/{sequence_id}/{frame_index}-{artifact_type}.jpg"


def derivative_key(sequence_id: str, frame_index: int, kind: str, width: int) -> str:
    """e.g. derivatives/seq-001/5-thumbnail-320.webp"""
    return f"{DERIVATIVES_PREFIX}/{sequence_id}/{frame_index}-{kind}-{width}.webp"


def upload_key(upload_id: str, filename: str) -> str:
    return f"{UPLOADS_PREFIX}/{upload_id}/{filename}"


def bundle_key(collection_slug: str, version: str) -> str:
    return f"{BUNDLES_PREFIX}/{collection_slug}/{version}.zip"
