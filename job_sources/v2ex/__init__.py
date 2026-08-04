"""Read-only V2EX “酷工作” JSON Feed source."""

from typing import TYPE_CHECKING

from .source import (
    FEED_URL,
    MAX_RESPONSE_BYTES,
    V2exCursor,
    V2exFetchResult,
    V2exItemOutcome,
    V2exJobSource,
    V2exSignals,
)

if TYPE_CHECKING:
    from .archive import V2exArchiveJobSource
    from .viewer import V2exViewerApp

__all__ = [
    "FEED_URL",
    "MAX_RESPONSE_BYTES",
    "V2exCursor",
    "V2exArchiveJobSource",
    "V2exFetchResult",
    "V2exItemOutcome",
    "V2exJobSource",
    "V2exSignals",
    "V2exViewerApp",
]


def __getattr__(name: str):
    if name == "V2exArchiveJobSource":
        from .archive import V2exArchiveJobSource

        return V2exArchiveJobSource
    if name == "V2exViewerApp":
        from .viewer import V2exViewerApp

        return V2exViewerApp
    raise AttributeError(name)
