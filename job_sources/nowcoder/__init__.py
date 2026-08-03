"""Nowcoder source adapter package."""

from .campaigns import CampaignPage, NowcoderCampaignApi, RecruitmentCampaign
from .source import (
    MAX_RESPONSE_BYTES,
    InvalidSourceRequest,
    JobReference,
    JobSearchResult,
    NowcoderSourceAdapter,
    NowcoderPublicSource,
    NowcoderPublicSearchCollector,
    NowcoderSourceError,
    SourceAccessRestrictedError,
    UpstreamChangedError,
    UpstreamHttpError,
    VerificationRequiredError,
    _load_public_html,
)
from job_sources.contracts import NormalizedJob

__all__ = [
    "CampaignPage",
    "MAX_RESPONSE_BYTES",
    "InvalidSourceRequest",
    "JobReference",
    "JobSearchResult",
    "NormalizedJob",
    "NowcoderCampaignApi",
    "NowcoderPublicSource",
    "NowcoderPublicSearchCollector",
    "NowcoderSourceAdapter",
    "NowcoderSourceError",
    "RecruitmentCampaign",
    "SourceAccessRestrictedError",
    "UpstreamChangedError",
    "UpstreamHttpError",
    "VerificationRequiredError",
    "_load_public_html",
]
