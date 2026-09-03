"""Object key templates.

Every key in the system is built here. The frames prefix is deliberately
byte-identical to the ``objectKey`` the bundled catalogue used, so the URLs the
site already serves keep resolving after the move to object storage - the seed
uploads to exactly the paths the old JSON recorded.
"""

from __future__ import annotations

from typing import Literal

ArtifactType = Literal["source", "mask", "thumbnail", "webp"]

FRAMES_PREFIX = "frames"
DERIVATIVES_PREFIX = "derivatives"
UPLOADS_PREFIX = "uploads/staging"
BUNDLES_PREFIX = "bundles"


def frame_key(video_id: str, frame_index: int, artifact_type: Literal["source", "mask"]) -> str:
    """e.g. frames/vid1/5-source.jpg"""
    return f"{FRAMES_PREFIX}/{video_id}/{frame_index}-{artifact_type}.jpg"


def derivative_key(video_id: str, frame_index: int, kind: str, width: int) -> str:
    """e.g. derivatives/vid1/5-thumbnail-320.webp"""
    return f"{DERIVATIVES_PREFIX}/{video_id}/{frame_index}-{kind}-{width}.webp"


def upload_key(upload_id: str, filename: str) -> str:
    return f"{UPLOADS_PREFIX}/{upload_id}/{filename}"


def bundle_key(collection_slug: str, version: str) -> str:
    return f"{BUNDLES_PREFIX}/{collection_slug}/{version}.zip"
