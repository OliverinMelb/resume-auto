"""Local-only routes for Eleduck public-job preview, research, and review."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from resume_agent.repository import NotFoundError, Repository
from resume_agent.router import require_local_request
from resume_agent.settings import get_settings

from .coordinator import (
    EleduckCoordinator,
    EleduckIncompleteResult,
    EleduckReviewConflict,
    EleduckReviewNotFound,
)
from .models import ClassifiedItem
from .parser import EleduckSourceError
from .research import EleduckResearchSink
from .source import EleduckPublicApiSource
from .state import EleduckStateError, EleduckStateStore, ReviewDecision


class EleduckRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        @wraps(original)
        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError as error:
                raise HTTPException(
                    status_code=400,
                    detail="Invalid Eleduck request",
                ) from error

        return handler


router = APIRouter(
    prefix="/job-sources/eleduck",
    tags=["job-sources"],
    dependencies=[Depends(require_local_request)],
    route_class=EleduckRoute,
)
_logger = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_UI_PATH = Path(__file__).with_name("ui.html")
_settings = get_settings()
_source = EleduckPublicApiSource()
_coordinator: EleduckCoordinator | None = None
_coordinator_lock = threading.Lock()


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EleduckResearchRequest(_StrictRequest):
    candidate_id: str | None = Field(default=None, min_length=1, max_length=100)
    project_name: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("candidate_id", "project_name")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("value cannot be blank")
        return normalized


class EleduckReviewRequest(EleduckResearchRequest):
    identity: str = Field(min_length=1, max_length=500)
    decision: ReviewDecision


def _get_coordinator() -> EleduckCoordinator:
    global _coordinator
    if _coordinator is None:
        with _coordinator_lock:
            if _coordinator is None:
                _coordinator = EleduckCoordinator(
                    source=_source,
                    store=EleduckStateStore(
                        _ROOT / ".runtime" / "eleduck" / "state.json"
                    ),
                    sink=EleduckResearchSink(
                        Repository(_settings.database_path)
                    ),
                )
    return _coordinator


def _classified_payload(value: ClassifiedItem) -> dict[str, object]:
    payload = asdict(value)
    payload["item"]["identity"] = value.item.identity
    return payload


def _decision_payload(value) -> dict[str, object]:
    payload = asdict(value)
    payload["item"] = _classified_payload(value.item)
    return payload


def _raise_eleduck_error(error: Exception) -> NoReturn:
    if isinstance(error, EleduckReviewNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(error, EleduckReviewConflict):
        raise HTTPException(status_code=409, detail=str(error)) from error
    if isinstance(error, NotFoundError):
        raise HTTPException(status_code=400, detail=str(error)) from error
    if isinstance(error, EleduckStateError):
        raise HTTPException(status_code=500, detail="Eleduck state operation failed") from error
    if isinstance(error, (EleduckIncompleteResult, EleduckSourceError)):
        raise HTTPException(status_code=502, detail=str(error)) from error
    _logger.error("Eleduck operation failed; error_type=%s", type(error).__name__)
    raise HTTPException(status_code=500, detail="Eleduck operation failed") from error


@router.get("/ui", response_class=HTMLResponse)
def eleduck_ui():
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


@router.get("/preview")
def preview_eleduck():
    try:
        result = _source.collect()
        return {
            "accepted_count": len(result.accepted_jobs),
            "review_count": len(result.review_items),
            "non_job_count": len(result.non_job_items),
            "discovered_count": result.discovered_count,
            "deduplicated_count": result.deduplicated_count,
            "invalid_item_count": result.invalid_item_count,
            "complete": result.complete,
            "stop_reason": result.stop_reason,
            "next_cursor": list(result.next_cursor.seen_identities),
            "accepted_jobs": [asdict(value) for value in result.accepted_jobs],
            "review_items": [_classified_payload(value) for value in result.review_items],
            "non_job_items": [
                _classified_payload(value) for value in result.non_job_items
            ],
        }
    except Exception as error:
        _raise_eleduck_error(error)


@router.post("/research")
def research_eleduck(payload: EleduckResearchRequest):
    try:
        return asdict(
            _get_coordinator().research(
                candidate_id=payload.candidate_id,
                project_name=payload.project_name,
            )
        )
    except Exception as error:
        _raise_eleduck_error(error)


@router.get("/reviews")
def list_eleduck_reviews():
    try:
        queue = _get_coordinator().list_reviews()
        return {
            "pending": [_classified_payload(value) for value in queue.pending],
            "confirmed_recruitment": [
                _decision_payload(value) for value in queue.confirmed_recruitment
            ],
            "ignored_non_job": [
                _decision_payload(value) for value in queue.ignored_non_job
            ],
        }
    except Exception as error:
        _raise_eleduck_error(error)


@router.post("/reviews/decision")
def decide_eleduck_review(payload: EleduckReviewRequest):
    try:
        return _decision_payload(
            _get_coordinator().decide_review(
                payload.identity,
                decision=payload.decision,
                candidate_id=payload.candidate_id,
                project_name=payload.project_name,
            )
        )
    except Exception as error:
        _raise_eleduck_error(error)
