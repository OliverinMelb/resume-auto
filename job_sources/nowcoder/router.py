"""Local routes for the Nowcoder source."""

import atexit
from dataclasses import asdict
from datetime import datetime, timezone
import logging
from pathlib import Path
from time import monotonic
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from job_sources.contracts import (
    CollectionMode,
    JobQuery,
    JobSort,
    RecruitmentChannel,
)
from resume_agent.router import require_local_request
from resume_agent.repository import Repository
from resume_agent.settings import get_settings

from .browser import NowcoderPlaywrightSearchCollector
from .checkpoints import NowcoderCheckpointStore
from .research import NowcoderResearchSink
from .anonymous import (
    AnonymousUpstreamError,
    NowcoderAnonymousApi,
    UrllibNowcoderTransport,
)
from .source import (
    InvalidSourceRequest,
    EmploymentTypeFilter,
    NowcoderPublicSource,
    NowcoderPublicSearchCollector,
    NowcoderSourceError,
    SourceKind,
    NowcoderSourceAdapter,
)
from .models import NowcoderAcquisitionMode
from .tasks import NowcoderTaskManager


router = APIRouter(
    prefix="/job-sources/nowcoder",
    tags=["job-sources"],
    dependencies=[Depends(require_local_request)],
)
_source = NowcoderPublicSource()
_anonymous_api = NowcoderAnonymousApi(UrllibNowcoderTransport())
_browser_collector = NowcoderPlaywrightSearchCollector()
_repo_root = Path(__file__).resolve().parents[2]
_resume_settings = get_settings()
_tasks = NowcoderTaskManager(
    NowcoderSourceAdapter(
        search_collector=_browser_collector,
        public_search_collector=NowcoderPublicSearchCollector(
            _source,
            anonymous_api=_anonymous_api,
        ),
        detail_source=_source,
    ),
    checkpoint_store=NowcoderCheckpointStore(
        _repo_root / ".runtime" / "nowcoder" / "tasks"
    ),
    research_sink=NowcoderResearchSink(
        Repository(_resume_settings.database_path)
    ),
)
_logger = logging.getLogger(__name__)
_UI_PATH = Path(__file__).with_name("ui.html")
atexit.register(_browser_collector.close)
atexit.register(_tasks.close)


class NowcoderTaskCreateRequest(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    recruitment_channel: RecruitmentChannel
    city: str | None = Field(default=None, max_length=100)
    graduation_year: str | None = Field(default=None, max_length=50)
    sort: JobSort = "recommended"
    mode: CollectionMode = "quick"
    acquisition_mode: NowcoderAcquisitionMode = "public"
    career_job_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^\d+$",
    )
    filtered_url: str | None = Field(default=None, max_length=2048)
    candidate_id: str | None = Field(default=None, max_length=100)
    project_name: str | None = Field(default=None, max_length=200)


def _raise_source_error(error: Exception) -> NoReturn:
    if isinstance(error, InvalidSourceRequest):
        raise HTTPException(status_code=400, detail=str(error)) from error
    if isinstance(error, NowcoderSourceError):
        raise HTTPException(status_code=502, detail=str(error)) from error
    if isinstance(error, AnonymousUpstreamError):
        raise HTTPException(status_code=502, detail=str(error)) from error
    raise error


def _raise_task_error(error: Exception) -> NoReturn:
    if isinstance(error, KeyError):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, (ValueError, InvalidSourceRequest)):
        raise HTTPException(status_code=400, detail=str(error)) from error
    if isinstance(error, (NowcoderSourceError, AnonymousUpstreamError)):
        raise HTTPException(status_code=502, detail=str(error)) from error
    raise error


def _task_filtered_url(payload: NowcoderTaskCreateRequest) -> str | None:
    if payload.acquisition_mode != "public":
        return None
    if payload.career_job_id is not None:
        return _source.build_filtered_url(
            payload.recruitment_channel,
            city=payload.city,
            career_job_id=payload.career_job_id,
            keyword=payload.keyword,
        )
    if payload.filtered_url is not None:
        return _source.validate_filtered_url(
            payload.recruitment_channel,
            payload.filtered_url,
        )
    return _source.build_filtered_url(
        payload.recruitment_channel,
        city=payload.city,
        career_job_id=None,
        keyword=payload.keyword,
    )


@router.get("/health")
def nowcoder_health():
    return _source.health_check()


@router.get("/filters")
def nowcoder_filters():
    try:
        options = _anonymous_api.get_filter_options()
        return {
            "career_jobs": [
                asdict(option) for option in options.career_jobs
            ],
            "hot_cities": list(options.hot_cities),
            "cities": list(options.cities),
        }
    except Exception as error:
        _raise_source_error(error)


@router.get("/ui", response_class=HTMLResponse)
def nowcoder_search_ui():
    return HTMLResponse(
        _UI_PATH.read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; "
                "style-src 'unsafe-inline'; "
                "script-src 'unsafe-inline'; "
                "connect-src 'self'; "
                "img-src 'none'; "
                "frame-ancestors 'none'"
            ),
        },
    )


@router.post("/tasks", status_code=202)
def create_nowcoder_task(payload: NowcoderTaskCreateRequest):
    try:
        filtered_url = _task_filtered_url(payload)
        query = JobQuery(
            keyword=payload.keyword,
            recruitment_channel=payload.recruitment_channel,
            city=payload.city,
            graduation_year=payload.graduation_year,
            sort=payload.sort,
        )
        snapshot = _tasks.create_task(
            query,
            mode=payload.mode,
            acquisition_mode=payload.acquisition_mode,
            filtered_url=filtered_url,
            candidate_id=payload.candidate_id,
            project_name=payload.project_name,
        )
        return asdict(snapshot)
    except Exception as error:
        _raise_task_error(error)


@router.get("/tasks")
def list_nowcoder_tasks():
    return {
        "tasks": [
            asdict(snapshot) for snapshot in _tasks.list_tasks()
        ]
    }


@router.get("/tasks/{task_id}")
def get_nowcoder_task(task_id: str):
    try:
        return asdict(_tasks.get_task(task_id))
    except Exception as error:
        _raise_task_error(error)


@router.get("/tasks/{task_id}/jobs")
def get_nowcoder_task_jobs(task_id: str):
    try:
        return {
            "task_id": task_id,
            "jobs": [asdict(job) for job in _tasks.list_jobs(task_id)],
        }
    except Exception as error:
        _raise_task_error(error)


@router.post("/tasks/{task_id}/stop")
def stop_nowcoder_task(task_id: str):
    try:
        return asdict(_tasks.stop_task(task_id))
    except Exception as error:
        _raise_task_error(error)


@router.post("/tasks/{task_id}/resume")
def resume_nowcoder_task(task_id: str):
    try:
        return asdict(_tasks.resume_task(task_id))
    except Exception as error:
        _raise_task_error(error)


@router.get("/search")
def search_nowcoder_jobs(
    keyword: str = Query(min_length=1, max_length=50),
    employment_type: EmploymentTypeFilter = "intern",
    limit: int = Query(default=10, ge=1, le=20),
):
    started = monotonic()
    _logger.info(
        "source=nowcoder operation=search started_at=%s "
        "employment_type=%s limit=%s",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        employment_type,
        limit,
    )
    try:
        result = _source.search_jobs(
            keyword=keyword,
            employment_type=employment_type,
            limit=limit,
        )
    except Exception as error:
        _logger.warning(
            "source=nowcoder operation=search finished_at=%s success=false "
            "error_type=%s elapsed_ms=%d",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            type(error).__name__,
            int((monotonic() - started) * 1000),
        )
        _raise_source_error(error)
    _logger.info(
        "source=nowcoder operation=search finished_at=%s success=true "
        "fetched_count=%d failed_count=%d elapsed_ms=%d",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        len(result.jobs),
        result.failed_details,
        int((monotonic() - started) * 1000),
    )
    return {
        "source": _source.source,
        "keyword": keyword,
        "employment_type": employment_type,
        "scope": "first_page_of_each_public_section",
        **asdict(result),
    }


@router.get("/jobs")
def list_nowcoder_jobs(
    kind: SourceKind = "internship",
    limit: int = Query(default=20, ge=1, le=20),
):
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = monotonic()
    _logger.info(
        "source=nowcoder operation=list started_at=%s kind=%s limit=%s",
        started_at,
        kind,
        limit,
    )
    try:
        jobs = _source.list_jobs(kind=kind, limit=limit)
    except Exception as error:
        _logger.warning(
            "source=nowcoder operation=list finished_at=%s success=false "
            "error_type=%s elapsed_ms=%d",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            type(error).__name__,
            int((monotonic() - started) * 1000),
        )
        _raise_source_error(error)
    _logger.info(
        "source=nowcoder operation=list finished_at=%s success=true "
        "fetched_count=%d elapsed_ms=%d",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        len(jobs),
        int((monotonic() - started) * 1000),
    )
    return {
        "source": _source.source,
        "source_section": kind,
        "type_verification": "detail_required",
        "count": len(jobs),
        "jobs": [asdict(job) for job in jobs],
    }


@router.get("/jobs/{source_job_id}")
def get_nowcoder_job(source_job_id: str):
    started = monotonic()
    _logger.info(
        "source=nowcoder operation=detail started_at=%s source_job_id=%s",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_job_id,
    )
    try:
        job = _source.get_job(source_job_id)
    except Exception as error:
        _logger.warning(
            "source=nowcoder operation=detail finished_at=%s success=false "
            "error_type=%s elapsed_ms=%d",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            type(error).__name__,
            int((monotonic() - started) * 1000),
        )
        _raise_source_error(error)
    _logger.info(
        "source=nowcoder operation=detail finished_at=%s success=true "
        "fetched_count=1 elapsed_ms=%d",
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        int((monotonic() - started) * 1000),
    )
    return asdict(job)
