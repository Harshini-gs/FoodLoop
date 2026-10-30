"""Shared schema pieces.

`GeoPoint` is the API representation of a PostGIS geography column. Latitude
and longitude are validated here so an out-of-range coordinate is a 422 rather
than a database error.
"""

from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class ORMModel(BaseModel):
    """Base for response models read from SQLAlchemy objects."""

    model_config = ConfigDict(from_attributes=True)


class GeoPoint(BaseModel):
    lat: float = Field(ge=-90, le=90, description="Latitude, WGS84")
    lon: float = Field(ge=-180, le=180, description="Longitude, WGS84")

    def to_wkt(self) -> str:
        """PostGIS expects lon-lat order, which is the opposite of how people
        say it. Getting this backwards puts Bengaluru in the Indian Ocean."""
        return f"SRID=4326;POINT({self.lon} {self.lat})"


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class PageParams(BaseModel):
    limit: int = Field(default=25, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    """Every field is a real check, not a hard-coded "ok"."""

    status: str
    db: str
    postgis: str | None = None
    migration: str | None = None
    app_env: str
    checked_at: datetime
