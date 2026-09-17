"""Response schemas for the locations API."""

from pydantic import BaseModel, ConfigDict


class LocationOut(BaseModel):
    """IATA dictionary entry."""

    model_config = ConfigDict(from_attributes=True)

    code: str
    name: str
    city: str
    country: str
    country_code: str | None = None
    type: str = "AIRPORT"
