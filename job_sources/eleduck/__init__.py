"""Public API for the isolated Eleduck RSS source."""

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
from .source import EleduckRssSource

__all__ = [
    "ClassifiedItem",
    "Classification",
    "EleduckCollectionResult",
    "EleduckCursor",
    "EleduckFeedError",
    "EleduckHttpError",
    "EleduckRssSource",
    "EleduckSourceError",
    "FeedItem",
    "PostCategory",
]
