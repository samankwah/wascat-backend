"""Public response models.

These describe the API rather than produce it. The routes return dicts built
by presenters.py and serialise them verbatim, because the envelope's key set,
its omission of unmeasured fields and its number rendering are contract that a
Pydantic round-trip would quietly normalise.

So the models are attached with ``responses={200: {"model": ...}}``, which
documents the shape without putting Pydantic in the response path. The
frontend generates its TypeScript from the resulting OpenAPI, which is how two
separate repositories keep one contract without sharing a package.

Keeping them honest is the parity test's job: it compares real responses
against recorded bytes, so a model that drifts from what is served shows up
there rather than being believed.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ArtifactType = Literal["source", "mask"]


class Coordinates(BaseModel):
    latitude: float
    longitude: float


class ArtifactOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: ArtifactType
    url: Annotated[str, Field(description="Where the file is served from.")]
    objectKey: Annotated[str, Field(description="Key within the object store.")]  # noqa: N815
    bytes: int
    checksum: Annotated[str, Field(description="SHA-256 of the delivered file.")]
    mediaType: str  # noqa: N815


class PredictionModelOut(BaseModel):
    """The classifier a probability vector came from."""

    slug: str
    name: str
    version: Annotated[
        str, Field(description="The model's own version string: a tag, a date or a commit.")
    ]
    description: str | None = None


class ClassProbabilityOut(BaseModel):
    skyClass: Annotated[  # noqa: N815
        str, Field(description="A SKY_CLASS vocabulary label, e.g. 'Stratocumulus'.")
    ]
    probability: Annotated[float, Field(ge=0, le=1)]


class PredictionOut(BaseModel):
    """One model's reading of one frame.

    ``classes`` is the whole vector, ranked by probability descending - every
    class the model scored, not just the winner. An all-sky frame routinely
    holds several genera at once, so the runners-up are the point: they are
    what makes the reading comparable with the observer's own.
    """

    model_config = ConfigDict(protected_namespaces=())

    model: PredictionModelOut
    classes: list[ClassProbabilityOut]


class ImageRecordOut(BaseModel):
    """One frame.

    ``cloudFraction`` and ``cloudCoverOktas`` are absent, not null, on a frame
    that carries no mask: cloud cover is measured from the mask, so without one
    there is no measurement to report. They are never defaulted to zero, which
    would invent a clear sky.

    ``observedCloudCoverOktas`` is the separate, human-supplied count that
    travels with the observer's cloud-genus label. It is never reconciled
    against the measured pair: the two can and do differ, and which one a
    reader wants depends on what they are checking.

    ``predictions`` appears on the single-record read only. The list endpoints
    omit it rather than serialise one entry per class per model for every card
    on the page.
    """

    id: str
    collection: str
    release: str
    sequenceId: str  # noqa: N815
    frameIndex: int  # noqa: N815

    cloudFraction: float | None = None  # noqa: N815
    cloudCoverOktas: Annotated[int | None, Field(ge=0, le=8)] = None  # noqa: N815
    observedCloudCoverOktas: Annotated[int | None, Field(ge=0, le=8)] = None  # noqa: N815

    maskScale: Annotated[  # noqa: N815
        float,
        Field(
            description=(
                "Scale at which the mask was delivered relative to its frame. "
                "1 for a correctly registered sequence; greater where the mask "
                "was rendered larger and the viewer must scale it back."
            )
        ),
    ]
    width: int
    height: int

    image: Annotated[str, Field(description="Primary listing image: the frame, else the mask.")]
    sourceUrl: str | None = None  # noqa: N815
    maskUrl: str | None = None  # noqa: N815
    hasSource: bool  # noqa: N815
    hasMask: bool  # noqa: N815

    alt: str
    tags: list[str]
    artifacts: list[ArtifactOut]
    sortKey: Annotated[  # noqa: N815
        str, Field(description="Capture time when known, sequence position otherwise.")
    ]

    capturedAt: str | None = None  # noqa: N815
    location: str | None = None
    coordinates: Coordinates | None = None
    season: str | None = None
    timeOfDay: str | None = None  # noqa: N815
    skyClass: str | None = None  # noqa: N815
    predictions: list[PredictionOut] | None = None
    instrument: str | None = None


class ReleaseOut(BaseModel):
    version: str
    images: int
    size: Annotated[str, Field(description="Human-readable total, e.g. '10.4 MB'.")]
    current: bool
    publishedAt: str | None = None  # noqa: N815


class MaskRegistration(BaseModel):
    sequenceId: str  # noqa: N815
    scale: Annotated[
        float,
        Field(description="Mask size relative to the frame it segments. 1 when registered."),
    ]
    corrected: bool


class CollectionOut(BaseModel):
    slug: str
    title: str
    shortTitle: str  # noqa: N815
    kicker: str
    description: str
    coverage: str
    sequenceIds: list[str]  # noqa: N815
    images: int
    artifacts: int
    withSource: int  # noqa: N815
    segmented: Annotated[int, Field(description="Records carrying a mask, and so a measurement.")]
    image: str
    imageAlt: str  # noqa: N815
    releases: list[ReleaseOut] = []

    meanCloudCoverOktas: Annotated[  # noqa: N815
        float | None,
        Field(description="Mean measured cover across the segmented frames."),
    ] = None
    maskRegistration: list[MaskRegistration] = []  # noqa: N815

    locationName: str | None = None  # noqa: N815
    location: str | None = None
    coordinates: Coordinates | None = None
    instrument: str | None = None
    license: str | None = None
    citation: str | None = None
    doi: str | None = None


class CollectionSummaryOut(CollectionOut):
    """The listing shape: the release history replaced by the current one."""

    releases: list[ReleaseOut] = Field(default=[], exclude=True)
    currentRelease: ReleaseOut | None = None  # noqa: N815


class FacetValue(BaseModel):
    value: str | int
    label: str | None = None
    count: int


class FacetsOut(BaseModel):
    collections: list[FacetValue]
    sequences: list[FacetValue]
    cloudCoverOktas: list[FacetValue]  # noqa: N815
    segmentation: list[FacetValue]
    artifacts: list[FacetValue]
    seasons: list[FacetValue]
    timesOfDay: list[FacetValue]  # noqa: N815
    skyClasses: list[FacetValue]  # noqa: N815
    locations: list[FacetValue]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


class Meta(BaseModel):
    apiVersion: str = "1.0"  # noqa: N815
    generatedAt: str  # noqa: N815


class PageMeta(Meta):
    count: int
    total: int
    limit: int
    nextCursor: str | None = None  # noqa: N815


class CountMeta(Meta):
    count: int


class ReleaseMeta(Meta):
    collection: str
    count: int


class Links(BaseModel):
    self: str


class PageLinks(Links):
    next: str | None = None


class ImagePage(BaseModel):
    data: list[ImageRecordOut]
    meta: PageMeta
    links: PageLinks


class ImageEnvelope(BaseModel):
    data: ImageRecordOut
    meta: Meta
    links: Links


class CollectionListEnvelope(BaseModel):
    data: list[CollectionSummaryOut]
    meta: CountMeta
    links: Links


class CollectionEnvelope(BaseModel):
    data: CollectionOut
    meta: Meta
    links: Links


class ReleaseListEnvelope(BaseModel):
    data: list[ReleaseOut]
    meta: ReleaseMeta
    links: Links


class FacetsEnvelope(BaseModel):
    data: FacetsOut
    meta: Meta
    links: Links


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, list[str]] | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


def response(model: type[BaseModel], description: str) -> dict[int | str, dict[str, Any]]:
    return {200: {"model": model, "description": description}}


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "One or more query parameters are invalid."},
    404: {"model": ErrorEnvelope, "description": "No such record."},
}
