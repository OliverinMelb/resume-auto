"""Public interface for the isolated Eleduck source adapters."""

from .models import (
    ClassifiedItem,
    Classification,
    EleduckCollectionResult,
    EleduckCursor,
    FeedItem,
    PostCategory,
)
from .parser import EleduckFeedError, EleduckSourceError
from .rss import EleduckHttpError
from .source import EleduckPublicApiSource, EleduckRssSource

__all__ = [
    "ClassifiedItem",
    "Classification",
    "EleduckCollectionResult",
    "EleduckCursor",
    "EleduckFeedError",
    "EleduckHttpError",
    "EleduckPublicApiSource",
    "EleduckRssSource",
    "EleduckSourceError",
    "FeedItem",
    "PostCategory",
]
