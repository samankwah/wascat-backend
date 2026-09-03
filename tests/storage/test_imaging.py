"""Probing and deriving images.

The archive records a checksum and dimensions against every artifact, so the
probe is not a formality: it is where the bytes stop being a claim and become
a fact. These tests are mostly about what gets *refused*.
"""

from __future__ import annotations

import hashlib
import io
from typing import Any

import pytest
from PIL import Image

from wascat.storage.imaging import (
    ALLOWED_MEDIA_TYPES,
    DERIVATIVES,
    UnsupportedImageError,
    derive,
    probe,
    sniff,
)

FRAME_SIZE = (640, 360)


def encode(size: tuple[int, int] = FRAME_SIZE, fmt: str = "JPEG", **kwargs: Any) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (40, 60, 90)).save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


class TestSniff:
    @pytest.mark.parametrize(
        ("fmt", "expected"),
        [("JPEG", "image/jpeg"), ("PNG", "image/png"), ("WEBP", "image/webp")],
    )
    def test_identifies_accepted_formats(self, fmt: str, expected: str) -> None:
        assert sniff(encode(fmt=fmt)) == expected
        assert expected in ALLOWED_MEDIA_TYPES

    def test_rejects_a_riff_container_that_is_not_webp(self) -> None:
        # WebP lives inside RIFF, but so does WAV. Matching the outer
        # container alone would accept audio as an image.
        assert sniff(b"RIFF\x24\x00\x00\x00WAVEfmt ") is None

    def test_does_not_trust_a_filename_or_declared_type(self) -> None:
        # The whole point: an upload named .jpg is still checked.
        assert sniff(b"not an image, whatever it is called") is None


class TestProbe:
    def test_measures_a_real_frame(self) -> None:
        data = encode()
        result = probe(data)
        assert (result.width, result.height) == FRAME_SIZE
        assert result.media_type == "image/jpeg"
        assert result.bytes == len(data)
        # The checksum is of the bytes as received, so it matches what is
        # ultimately stored.
        assert result.checksum == hashlib.sha256(data).hexdigest()

    @pytest.mark.parametrize(
        ("name", "data"),
        [
            ("empty", b""),
            ("plain text", b"this is not an image at all"),
            ("gif", b"GIF89a" + b"\x00" * 64),
            ("svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'),
        ],
    )
    def test_refuses_what_is_not_an_accepted_image(self, name: str, data: bytes) -> None:
        with pytest.raises(UnsupportedImageError):
            probe(data)

    def test_refuses_a_truncated_file(self) -> None:
        # Half an upload should fail rather than yield an image whose lower
        # rows are grey - which is what Pillow does if truncation is allowed.
        with pytest.raises(UnsupportedImageError, match="truncated or corrupt"):
            probe(encode()[:100])

    def test_explains_itself_in_words_a_curator_can_act_on(self) -> None:
        with pytest.raises(UnsupportedImageError) as caught:
            probe(b"nope")
        message = str(caught.value)
        assert "JPEG" in message
        assert "mask" in message


class TestDerive:
    def test_generates_every_configured_rendition(self) -> None:
        results = derive(encode())
        assert [d.kind for d in results] == [spec.kind for spec in DERIVATIVES]
        assert all(d.media_type == "image/webp" for d in results)

    def test_preserves_aspect_ratio(self) -> None:
        for derivative in derive(encode()):
            source_ratio = FRAME_SIZE[0] / FRAME_SIZE[1]
            assert abs(derivative.width / derivative.height - source_ratio) < 0.01

    def test_never_upscales(self) -> None:
        # A spec wider than the source produces the source's own width.
        # Inventing pixels is not this system's habit.
        for derivative in derive(encode(size=(100, 60), fmt="PNG")):
            assert derivative.width <= 100

    def test_is_smaller_than_the_source(self) -> None:
        # The reason derivatives exist: the dashboard's grids pull a hundred
        # frames at a time.
        source = encode(quality=90)
        for derivative in derive(source):
            assert len(derivative.data) < len(source)

    def test_handles_a_greyscale_mask(self) -> None:
        # Masks are delivered greyscale; normalising to RGB first keeps the
        # output predictable rather than mode-dependent.
        buffer = io.BytesIO()
        Image.new("L", FRAME_SIZE, 200).save(buffer, format="JPEG")
        results = derive(buffer.getvalue())
        assert len(results) == len(DERIVATIVES)
