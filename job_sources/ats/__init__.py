"""Pure detection of public ATS page signatures."""

from .batch import (
    AtsBatchItem,
    AtsBatchReport,
    detect_ats_urls,
    serialize_ats_batch_report,
)
from .clustering import AtsCluster, ReviewStatus, cluster_ats_detections
from .detector import AtsDetection, AtsName, detect_ats
from .discovery import (
    AtsBoard,
    AtsBoardCatalog,
    AtsDiscoveryError,
    AtsProvider,
    boards_from_index_records,
    discover_ats_boards,
)
from .http import AtsHttpPolicy, detect_ats_url
from .listings import (
    AtsJobCollection,
    InvalidJobCollectionRequest,
    JobListingFetchError,
    UnsupportedJobBoardError,
    collect_ats_jobs,
)
from .search import AtsBoardFailure, AtsJobSearchReport, search_ats_boards

__all__ = [
    "AtsBatchItem",
    "AtsBatchReport",
    "AtsBoard",
    "AtsBoardCatalog",
    "AtsBoardFailure",
    "AtsCluster",
    "AtsDetection",
    "AtsDiscoveryError",
    "AtsHttpPolicy",
    "AtsJobCollection",
    "AtsJobSearchReport",
    "AtsProvider",
    "AtsName",
    "ReviewStatus",
    "InvalidJobCollectionRequest",
    "JobListingFetchError",
    "UnsupportedJobBoardError",
    "boards_from_index_records",
    "cluster_ats_detections",
    "collect_ats_jobs",
    "detect_ats",
    "detect_ats_url",
    "detect_ats_urls",
    "discover_ats_boards",
    "search_ats_boards",
    "serialize_ats_batch_report",
]
