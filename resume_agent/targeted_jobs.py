"""Resolve one targeted JD input into a candidate-owned repository job."""

from __future__ import annotations

import hashlib
import re
from datetime import timezone
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .discovery_schemas import DiscoveredJob
from .discovery_sources import (
    DiscoverySourceAdapter,
    SourceInteractionRequiredError,
)
from .importer import parse_raw_job
from .repository import NotFoundError, Repository
from .rewrite_schemas import StrictModel


_URL_SOURCES = {
    "www.nowcoder.com": "nowcoder",
    "www.zhipin.com": "boss",
}
_COMPANY_HEADINGS = {"公司", "公司名称", "company"}


class JobTarget(StrictModel):
    """Validated internal target; external mappings must use ``from_payload``."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    job_id: str | None = None
    jd_text: str | None = Field(default=None, max_length=100_000)
    jd_url: str | None = Field(default=None, max_length=8192)
    title: str | None = Field(default=None, max_length=500)
    company: str | None = Field(default=None, max_length=500)

    @classmethod
    def from_payload(cls, payload: object) -> Self:
        """Validate untrusted input without exposing raw validation details."""
        try:
            return cls.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            pass
        raise ValueError("INVALID_JOB_TARGET")

    @field_validator("job_id", "jd_text", "jd_url", mode="before")
    @classmethod
    def nonblank_primary(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("targeted JD values must be strings")
        normalized = value.strip()
        if not normalized:
            raise ValueError("targeted JD input must not be blank")
        return normalized

    @field_validator("title", "company", mode="before")
    @classmethod
    def normalize_supplement(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("targeted JD supplements must be strings")
        return value.strip() or None

    @model_validator(mode="after")
    def exactly_one_target(self):
        supplied = [
            self.job_id is not None,
            self.jd_text is not None,
            self.jd_url is not None,
        ]
        if sum(supplied) != 1:
            raise ValueError("exactly one targeted JD input is required")
        if self.jd_text is None and (self.title is not None or self.company is not None):
            raise ValueError("title and company are supported only with jd_text")
        return self


class TargetedJobResolver:
    def __init__(
        self,
        repository: Repository,
        adapters: dict[str, DiscoverySourceAdapter],
    ):
        self.repository = repository
        self.adapters = dict(adapters)

    def resolve(self, target: JobTarget, candidate_id: str) -> str:
        self._validate_candidate(candidate_id)
        if target.job_id is not None:
            return self._reuse_job(target.job_id, candidate_id)
        if target.jd_text is not None:
            return self._resolve_text(target, candidate_id)
        assert target.jd_url is not None
        return self._resolve_url(target.jd_url, candidate_id)

    def _validate_candidate(self, candidate_id: str) -> None:
        try:
            self.repository.get_candidate(candidate_id)
        except (NotFoundError, TypeError, ValueError):
            raise ValueError(
                "TARGET_CANDIDATE_INVALID: candidate is unavailable"
            ) from None

    def _reuse_job(self, job_id: str, candidate_id: str) -> str:
        try:
            job = self.repository.get_job(job_id)
        except NotFoundError:
            raise ValueError("TARGET_JOB_NOT_FOUND") from None
        if job.get("candidate_id") != candidate_id:
            raise ValueError(
                "TARGET_CANDIDATE_MISMATCH: job candidate mismatch"
            )
        return job["id"]

    def _resolve_text(self, target: JobTarget, candidate_id: str) -> str:
        assert target.jd_text is not None
        body = target.jd_text
        parsed_title, salary, parsed_detail = parse_raw_job(body)
        title = _nonempty(parsed_title) or target.title
        if title is None:
            raise ValueError("TARGET_TITLE_REQUIRED")
        detail = _nonempty(parsed_detail) or body
        company = _parsed_company(body) or target.company or ""
        fingerprint = _fingerprint("targeted_text", body, title, company)
        job, _ = self.repository.create_job_if_absent(
            {
                "candidate_id": candidate_id,
                "source": "targeted_text",
                "title": title,
                "salary": _nonempty(salary) or "",
                "detail": detail,
                "company": company,
            },
            fingerprint,
        )
        return job["id"]

    def _resolve_url(self, url: str, candidate_id: str) -> str:
        source = _source_for_url(url)
        adapter = self.adapters.get(source)
        if adapter is None:
            raise ValueError("SOURCE_ADAPTER_UNAVAILABLE")
        if getattr(adapter, "source", None) != source:
            raise ValueError("SOURCE_ADAPTER_MISMATCH")
        try:
            discovered = adapter.resolve_url(url, "targeted")
        except SourceInteractionRequiredError:
            raise ValueError("SOURCE_INTERACTION_REQUIRED") from None
        except Exception:
            raise ValueError("SOURCE_RESOLUTION_FAILED") from None
        if not isinstance(discovered, DiscoveredJob):
            raise ValueError("SOURCE_RESULT_INVALID")
        if discovered.source != source:
            raise ValueError("SOURCE_RESULT_MISMATCH")
        if (
            discovered.detail_status != "complete"
            or not discovered.detail.strip()
        ):
            raise ValueError("PARTIAL_JD")
        if not discovered.source_job_id.strip() or not discovered.title.strip():
            raise ValueError("SOURCE_RESULT_INVALID")

        fingerprint = _fingerprint(discovered.source, discovered.source_job_id)
        job, _ = self.repository.create_job_if_absent(
            _canonical_job_payload(candidate_id, discovered), fingerprint
        )
        return job["id"]


def _source_for_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise ValueError("UNSUPPORTED_JD_SOURCE") from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise ValueError("UNSUPPORTED_JD_SOURCE")
    source = _URL_SOURCES.get((parsed.hostname or "").casefold())
    if source is None:
        raise ValueError("UNSUPPORTED_JD_SOURCE")
    return source


def _canonical_job_payload(candidate_id: str, job: DiscoveredJob) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "external_job_id": job.source_job_id,
        "source": job.source,
        "title": job.title,
        "salary": job.salary_raw or "",
        "detail": job.detail,
        "company": job.company,
        "score": job.score,
        "qualified": job.qualified,
        "search_keyword": job.search_keyword,
        "source_logged_at": job.verified_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
    }


def _parsed_company(body: str) -> str | None:
    for section in re.split(r"\n\s*\n", body):
        lines = section.splitlines()
        if not lines:
            continue
        heading = lines[0].strip().lstrip("#").strip().casefold()
        if heading in _COMPANY_HEADINGS:
            return _nonempty("\n".join(lines[1:]))
    return None


def _fingerprint(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = part.strip().encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _nonempty(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
