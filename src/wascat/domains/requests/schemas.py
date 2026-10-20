"""What a requester and the dashboard may send.

Unlike a sign-in address, the requester's address is a destination: the data,
or the reason it was declined, is mailed to it. So it is ``EmailStr`` here,
deliberately, where ``iam`` avoids it.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field

CollectionSlug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,80}$")]


class DataRequestCreate(BaseModel):
    """A request from the public form."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    organisation: str = Field(min_length=2, max_length=200)
    purpose: str = Field(
        min_length=20,
        max_length=4000,
        description="What the data will be used for. Read by whoever approves the request.",
    )
    collections: list[CollectionSlug] = Field(min_length=1, max_length=50)
    accept_licence: bool = Field(
        alias="acceptLicence",
        description="The requester agrees to cite the archive and follow each collection's licence.",
    )
    # A field people never see. Anything that fills it in is not a person.
    website: str = Field(default="", max_length=200)


class DataRequestApprove(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    note: str | None = Field(default=None, max_length=4000)
    access_days: int | None = Field(default=None, alias="accessDays", ge=1, le=30)


class DataRequestDecline(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, str_strip_whitespace=True)

    # A decline always says why: the requester deserves a reason.
    note: str = Field(min_length=5, max_length=4000)
