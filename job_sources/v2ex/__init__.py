"""Read-only V2EX “酷工作” JSON Feed source."""

from .source import (
    FEED_URL,
    MAX_RESPONSE_BYTES,
    V2exCursor,
    V2exFetchResult,
    V2exItemOutcome,
    V2exJobSource,
    V2exSignals,
)

__all__ = [
    "FEED_URL",
    "MAX_RESPONSE_BYTES",
    "V2exCursor",
    "V2exFetchResult",
    "V2exItemOutcome",
    "V2exJobSource",
    "V2exSignals",
]
