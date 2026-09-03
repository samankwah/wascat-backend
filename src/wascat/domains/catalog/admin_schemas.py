"""What the dashboard may send.

Every field is optional on an update, and `exclude_unset` distinguishes "leave
this alone" from "clear it". That distinction matters more here than in most
CRUD: the archive's rule is that an unknown value is absent rather than
defaulted, so a form that omits a field must not be able to silently blank one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Slug = Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=80)]
Version = Annotated[str, Field(pattern=r"^\d+\.\d+(?:\.\d+)?$", max_length=20)]


class CollectionWrite(BaseModel):
    """Editorial metadata for a sequence.

    These are the fields that are NULL for every sequence today - the gap the
    dashboard exists to close.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    title: str | None = Field(default=None, max_length=200)
    description: str | None = None
    location_name: str | None = Field(default=None, alias="locationName", max_length=200)
    latitude: Annotated[Decimal | None, Field(ge=-90, le=90)] = None
    longitude: Annotated[Decimal | None, Field(ge=-180, le=180)] = None
    instrument: str | None = Field(default=None, max_length=200)
    license: str | None = Field(default=None, max_length=200)
    citation: str | None = None
    doi: str | None = Field(default=None, max_length=200)
    methods_url: str | None = Field(default=None, alias="methodsUrl", max_length=500)
    publication_url: str | None = Field(default=None, alias="publicationUrl", max_length=500)
    position: int | None = None


class CollectionCreate(CollectionWrite):
    slug: Slug


class ImageRecordWrite(BaseModel):
    """Provenance a curator supplies for a frame.

    Nothing here can invent a measurement: cloud cover comes from the mask and
    is not settable. What a person knows and a pipeline does not - where the
    camera was, when it was pointed at the sky, what it was - is what this
    carries.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    captured_at: str | None = Field(default=None, alias="capturedAt")
    latitude: Annotated[Decimal | None, Field(ge=-90, le=90)] = None
    longitude: Annotated[Decimal | None, Field(ge=-180, le=180)] = None
    instrument: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    season: str | None = Field(default=None, max_length=80)
    time_of_day: str | None = Field(default=None, alias="timeOfDay", max_length=80)
    sky_class: str | None = Field(default=None, alias="skyClass", max_length=80)
    condition_tags: list[str] | None = Field(default=None, alias="conditionTags")
    custom: dict[str, object] | None = None


class BulkImageEdit(BaseModel):
    """Apply the same provenance to many frames.

    A capture team supplies a site or an instrument for a whole sequence at
    once, not frame by frame; doing it one request at a time would be both slow
    and a worse audit trail.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ids: Annotated[list[str], Field(min_length=1, max_length=1000)]
    changes: ImageRecordWrite
    #: Tags are additive by default, because a bulk edit that silently
    #: discarded existing tags would be very hard to notice.
    tag_mode: Literal["add", "replace", "remove"] = Field(default="add", alias="tagMode")


class ReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    version: Version
    capture_start: str | None = Field(default=None, alias="captureStart")
    capture_end: str | None = Field(default=None, alias="captureEnd")
    notes: str | None = None


class ReleaseTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["PUBLISHED", "RETIRED"]
    #: Only meaningful when publishing. A collection has at most one current
    #: release, so setting this demotes whichever held it.
    current: bool = True


class VocabularyTermWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    label: str = Field(max_length=120)
    description: str | None = None
    position: int | None = None


class VocabularyMerge(BaseModel):
    """Fold one term into another.

    Nothing is deleted: the losing term is retired, pointed at the winner, and
    its label kept as an alias, so a bookmarked query for the old value keeps
    resolving.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    into_id: str = Field(alias="intoId")


class VocabularyReorder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: Annotated[list[str], Field(min_length=1)]
