"""Data classes for the Metalocus crawler."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ImageData:
    url: str
    filename: str
    alt_text: str = ""
    image_order: int = 0


@dataclass
class BuildingData:
    title: str = ""
    architects: str = ""
    location: str = ""
    city: str = ""
    country: str = ""
    year: str = ""
    area_sqm: str = ""
    building_type: str = ""
    description: str = ""
    credits: str = ""  # JSON string
    materials: str = ""
    publication_date: str = ""
    raw_metadata: str = ""  # JSON string
    tags: list[str] = field(default_factory=list)
    images: list[ImageData] = field(default_factory=list)
