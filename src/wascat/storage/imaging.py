"""Inspecting and deriving images.

Two jobs, both of which must happen on the *bytes the server received* rather
than on what the client claimed about them:

* **Probe.** What is this file really? An upload's declared content type and
  filename are hints, not facts. The archive records dimensions and a checksum
  against every artifact, and a record whose stored dimensions disagree with
  its pixels is worse than no record.

* **Derive.** Thumbnails and WebP renditions for the dashboard's grids, which
  would otherwise pull 25 KB JPEGs a hundred at a time.

Everything here is synchronous and CPU-bound. Callers run it through
``anyio.to_thread.run_sync``; on the event loop a single large decode would
stall every other request for its duration.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import Final

from PIL import Image, ImageFile, UnidentifiedImageError

#: What an all-sky frame or a mask may be delivered as. Deliberately short: an
#: archive that accepts anything eventually contains everything.
ALLOWED_MEDIA_TYPES: Final[frozenset[str]] = frozenset({"image/jpeg", "image/png", "image/webp"})

#: Magic bytes, checked instead of trusting the declared type. A file claiming
#: to be a JPEG while being something else should be refused before Pillow is
#: asked to parse it.
_SIGNATURES: Final[tuple[tuple[bytes, str], ...]] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    # WebP is "RIFF....WEBP"; the size field sits between, so it is matched in
    # two parts by sniff().
    (b"RIFF", "image/webp"),
)

#: Pillow's own guard against decompression bombs, left at its default rather
#: than raised. A 640x360 frame is four orders of magnitude below it.
MAX_PIXELS: Final[int] = Image.MAX_IMAGE_PIXELS or 178_956_970

#: A truncated upload should raise rather than silently yield a half image
#: whose lower rows are grey.
ImageFile.LOAD_TRUNCATED_IMAGES = False


class UnsupportedImageError(ValueError):
    """The bytes are not an image this archive accepts."""


@dataclass(frozen=True, slots=True)
class Probe:
    """What the bytes actually are."""

    media_type: str
    width: int
    height: int
    checksum: str
    bytes: int

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000


def sniff(data: bytes) -> str | None:
    """The media type according to the file's own leading bytes."""
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            if media_type == "image/webp":
                # RIFF is a container; only the WEBP form is an image.
                return "image/webp" if data[8:12] == b"WEBP" else None
            return media_type
    return None


def probe(data: bytes) -> Probe:
    """Identify and measure an upload.

    Raises :class:`UnsupportedImageError` for anything that is not one of the
    accepted formats, or that Pillow cannot open. The checksum is of the bytes
    as received, so it matches whatever is ultimately stored.
    """
    if not data:
        raise UnsupportedImageError("The file is empty.")

    media_type = sniff(data)
    if media_type is None:
        raise UnsupportedImageError(
            "That file is not a JPEG, PNG or WebP image. The archive stores "
            "camera frames and segmentation masks."
        )
    if media_type not in ALLOWED_MEDIA_TYPES:
        raise UnsupportedImageError(f"{media_type} images are not accepted.")

    try:
        with Image.open(io.BytesIO(data)) as image:
            # verify() checks structure without decoding pixels, so a
            # deliberately malformed file is rejected cheaply.
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
    except UnidentifiedImageError as exc:
        raise UnsupportedImageError("The file could not be read as an image.") from exc
    except Image.DecompressionBombError as exc:
        raise UnsupportedImageError("That image is too large to process safely.") from exc
    except OSError as exc:
        # Pillow raises a bare OSError for a truncated file.
        raise UnsupportedImageError("The file appears to be truncated or corrupt.") from exc

    if width <= 0 or height <= 0:
        raise UnsupportedImageError("The image has no pixels.")

    return Probe(
        media_type=media_type,
        width=width,
        height=height,
        checksum=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
    )


# ---------------------------------------------------------------------------
# Derivatives
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DerivativeSpec:
    kind: str
    width: int
    quality: int


#: What gets generated on ingest.
#:
#: A frame is 640x360, so the 640 rendition is a re-encode rather than a
#: resize - worth it because WebP at q82 is roughly half the bytes of the
#: delivered JPEG with no visible loss, and the dashboard's grids pull a
#: hundred at a time.
DERIVATIVES: Final[tuple[DerivativeSpec, ...]] = (
    DerivativeSpec(kind="thumbnail", width=320, quality=72),
    DerivativeSpec(kind="webp", width=640, quality=82),
)


@dataclass(frozen=True, slots=True)
class Derivative:
    kind: str
    data: bytes
    media_type: str
    width: int
    height: int


def derive(data: bytes, specs: tuple[DerivativeSpec, ...] = DERIVATIVES) -> list[Derivative]:
    """Render the smaller versions the dashboard needs.

    Never upscales: a spec wider than the source produces a rendition at the
    source's own width, because inventing pixels is not this system's habit.
    """
    results: list[Derivative] = []

    with Image.open(io.BytesIO(data)) as image:
        # A mask is greyscale and a frame may carry an ICC profile or EXIF
        # orientation; normalising to RGB first keeps the output predictable.
        source = image.convert("RGB")

        for spec in specs:
            width = min(spec.width, source.width)
            height = max(1, round(source.height * (width / source.width)))

            # LANCZOS: the frames are fisheye discs with a hard edge against
            # black corners, and a cheaper filter leaves that edge visibly
            # ragged at thumbnail size.
            resized = (
                source
                if (width, height) == source.size
                else source.resize((width, height), Image.Resampling.LANCZOS)
            )

            buffer = io.BytesIO()
            resized.save(buffer, format="WEBP", quality=spec.quality, method=4)
            results.append(
                Derivative(
                    kind=spec.kind,
                    data=buffer.getvalue(),
                    media_type="image/webp",
                    width=width,
                    height=height,
                )
            )

    return results
