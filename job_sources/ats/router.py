"""Local-only HTTP adapter for bounded ATS detection."""

from __future__ import annotations

from dataclasses import asdict
import json
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from resume_agent.router import require_local_request

from .batch import detect_ats_urls, serialize_ats_batch_report
from .discovery import AtsDiscoveryError, discover_ats_boards
from .listings import (
    InvalidJobCollectionRequest,
    JobListingFetchError,
    UnsupportedJobBoardError,
    collect_ats_jobs,
)
from .search import search_ats_boards


_ALLOWED_ORIGINS = {
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://[::1]:3000",
}
_MAX_BODY_BYTES = 132 * 1_024
_MAX_JOB_BODY_BYTES = 16 * 1_024
_MAX_URL_BYTES = 8_192
_MAX_TOTAL_URL_BYTES = 128 * 1_024
_MAX_URLS = 20
_WEB_BATCH_LOCK = Lock()
_WEB_JOB_LOCK = Lock()
_WEB_DISCOVERY_LOCK = Lock()
_WEB_SEARCH_LOCK = Lock()

router = APIRouter(
    prefix="/job-sources/ats",
    tags=["job-sources"],
    dependencies=[Depends(require_local_request)],
)


def _cors_headers(origin: str | None) -> dict[str, str]:
    if origin not in _ALLOWED_ORIGINS:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Vary": "Origin",
    }


def _error(
    status_code: int,
    detail: str,
    *,
    origin: str | None,
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=detail,
        headers=_cors_headers(origin),
    )


def _require_local_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is not None and origin not in _ALLOWED_ORIGINS:
        raise HTTPException(status_code=403, detail="origin_not_allowed")
    return origin


async def _read_bounded_body(
    request: Request,
    *,
    origin: str | None,
    max_bytes: int = _MAX_BODY_BYTES,
) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise _error(413, "input_too_large", origin=origin)
        body.extend(chunk)
    return bytes(body)


def _validate_urls(
    payload: object,
    *,
    origin: str | None,
) -> tuple[str, ...]:
    if not isinstance(payload, dict) or set(payload) != {"urls"}:
        raise _error(400, "invalid_request", origin=origin)
    values = payload["urls"]
    if not isinstance(values, list) or not 1 <= len(values) <= _MAX_URLS:
        raise _error(400, "invalid_request", origin=origin)

    urls: list[str] = []
    total_bytes = 0
    for value in values:
        if not isinstance(value, str) or not (url := value.strip()):
            raise _error(400, "invalid_request", origin=origin)
        try:
            url_bytes = len(url.encode("utf-8"))
        except UnicodeEncodeError:
            raise _error(400, "invalid_request", origin=origin) from None
        total_bytes += url_bytes
        if url_bytes > _MAX_URL_BYTES or total_bytes > _MAX_TOTAL_URL_BYTES:
            raise _error(413, "input_too_large", origin=origin)
        urls.append(url)
    return tuple(urls)


def _validate_job_request(
    payload: object,
    *,
    origin: str | None,
) -> tuple[str, str | None, str | None, int]:
    if not isinstance(payload, dict) or set(payload) != {
        "url",
        "company",
        "keyword",
        "limit",
    }:
        raise _error(400, "invalid_request", origin=origin)
    url = payload["url"]
    company = payload["company"]
    keyword = payload["keyword"]
    limit = payload["limit"]
    if not isinstance(url, str) or not (url := url.strip()):
        raise _error(400, "invalid_request", origin=origin)
    if len(url) > 8_192:
        raise _error(413, "input_too_large", origin=origin)
    for value, max_length in ((company, 200), (keyword, 100)):
        if value is not None and not isinstance(value, str):
            raise _error(400, "invalid_request", origin=origin)
        if isinstance(value, str) and len(value.strip()) > max_length:
            raise _error(400, "invalid_request", origin=origin)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise _error(400, "invalid_request", origin=origin)
    try:
        url.encode("utf-8")
        if isinstance(company, str):
            company.encode("utf-8")
        if isinstance(keyword, str):
            keyword.encode("utf-8")
    except UnicodeEncodeError:
        raise _error(400, "invalid_request", origin=origin) from None
    return (
        url,
        company.strip() or None if isinstance(company, str) else None,
        keyword.strip() or None if isinstance(keyword, str) else None,
        limit,
    )


def _validate_discovery_request(
    payload: object,
    *,
    origin: str | None,
) -> tuple[int, bool]:
    if not isinstance(payload, dict) or set(payload) != {"limit", "refresh"}:
        raise _error(400, "invalid_request", origin=origin)
    limit = payload["limit"]
    refresh = payload["refresh"]
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 50
        or not isinstance(refresh, bool)
    ):
        raise _error(400, "invalid_request", origin=origin)
    return limit, refresh


def _validate_search_request(
    payload: object,
    *,
    origin: str | None,
) -> tuple[str, int, int, bool]:
    if not isinstance(payload, dict) or set(payload) != {
        "keyword",
        "limit",
        "max_boards",
        "refresh",
    }:
        raise _error(400, "invalid_request", origin=origin)
    keyword = payload["keyword"]
    limit = payload["limit"]
    max_boards = payload["max_boards"]
    refresh = payload["refresh"]
    if not isinstance(keyword, str) or not (keyword := keyword.strip()):
        raise _error(400, "invalid_request", origin=origin)
    if len(keyword) > 100:
        raise _error(400, "invalid_request", origin=origin)
    try:
        keyword.encode("utf-8")
    except UnicodeEncodeError:
        raise _error(400, "invalid_request", origin=origin) from None
    for value, maximum in ((limit, 50), (max_boards, 20)):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise _error(400, "invalid_request", origin=origin)
    if not isinstance(refresh, bool):
        raise _error(400, "invalid_request", origin=origin)
    return keyword, limit, max_boards, refresh


@router.options("/detect")
@router.options("/jobs")
@router.options("/boards/discover")
@router.options("/jobs/search")
def ats_detection_preflight(request: Request):
    origin = _require_local_origin(request)
    return Response(
        status_code=204,
        headers={
            **_cors_headers(origin),
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "content-type",
        },
    )


@router.post("/boards/discover")
async def discover_public_ats_boards(request: Request):
    origin = _require_local_origin(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise _error(415, "unsupported_media_type", origin=origin)
    body = await _read_bounded_body(
        request,
        origin=origin,
        max_bytes=_MAX_JOB_BODY_BYTES,
    )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error(400, "invalid_request", origin=origin) from None
    limit, refresh = _validate_discovery_request(payload, origin=origin)
    if not _WEB_DISCOVERY_LOCK.acquire(blocking=False):
        raise _error(409, "discovery_in_progress", origin=origin)
    try:
        try:
            catalog = await run_in_threadpool(
                discover_ats_boards,
                limit=limit,
                refresh=refresh,
            )
            content = json.dumps(
                asdict(catalog),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except AtsDiscoveryError:
            raise _error(502, "discovery_failed", origin=origin) from None
        except HTTPException:
            raise
        except Exception:
            raise _error(502, "discovery_failed", origin=origin) from None
    finally:
        _WEB_DISCOVERY_LOCK.release()
    return Response(
        content=content,
        media_type="application/json",
        headers=_cors_headers(origin),
    )


@router.post("/jobs/search")
async def search_public_ats_jobs(request: Request):
    origin = _require_local_origin(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise _error(415, "unsupported_media_type", origin=origin)
    body = await _read_bounded_body(
        request,
        origin=origin,
        max_bytes=_MAX_JOB_BODY_BYTES,
    )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error(400, "invalid_request", origin=origin) from None
    keyword, limit, max_boards, refresh = _validate_search_request(
        payload,
        origin=origin,
    )
    if not _WEB_SEARCH_LOCK.acquire(blocking=False):
        raise _error(409, "search_in_progress", origin=origin)
    try:
        try:
            catalog = await run_in_threadpool(
                discover_ats_boards,
                limit=50,
                refresh=refresh,
            )
            report = await run_in_threadpool(
                search_ats_boards,
                catalog.boards,
                keyword=keyword,
                limit=limit,
                max_boards=max_boards,
                max_workers=4,
            )
            content = json.dumps(
                asdict(report),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except AtsDiscoveryError:
            raise _error(502, "discovery_failed", origin=origin) from None
        except HTTPException:
            raise
        except Exception:
            raise _error(502, "search_failed", origin=origin) from None
    finally:
        _WEB_SEARCH_LOCK.release()
    return Response(
        content=content,
        media_type="application/json",
        headers=_cors_headers(origin),
    )


@router.post("/detect")
async def detect_ats_sites(request: Request):
    origin = _require_local_origin(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise _error(415, "unsupported_media_type", origin=origin)
    body = await _read_bounded_body(request, origin=origin)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error(400, "invalid_request", origin=origin) from None
    urls = _validate_urls(payload, origin=origin)
    if not _WEB_BATCH_LOCK.acquire(blocking=False):
        raise _error(409, "detection_in_progress", origin=origin)
    try:
        try:
            report = await run_in_threadpool(
                detect_ats_urls,
                urls,
                max_workers=4,
            )
            content = serialize_ats_batch_report(report, format="json")
        except Exception:
            raise _error(502, "detection_failed", origin=origin) from None
    finally:
        _WEB_BATCH_LOCK.release()
    return Response(
        content=content,
        media_type="application/json",
        headers=_cors_headers(origin),
    )


@router.post("/jobs")
async def collect_public_ats_jobs(request: Request):
    origin = _require_local_origin(request)
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise _error(415, "unsupported_media_type", origin=origin)
    body = await _read_bounded_body(
        request,
        origin=origin,
        max_bytes=_MAX_JOB_BODY_BYTES,
    )
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _error(400, "invalid_request", origin=origin) from None
    url, company, keyword, limit = _validate_job_request(payload, origin=origin)
    if not _WEB_JOB_LOCK.acquire(blocking=False):
        raise _error(409, "collection_in_progress", origin=origin)
    try:
        try:
            collection = await run_in_threadpool(
                collect_ats_jobs,
                url,
                company=company,
                keyword=keyword,
                limit=limit,
                score=True,
            )
            content = json.dumps(
                asdict(collection),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except InvalidJobCollectionRequest:
            raise _error(400, "invalid_request", origin=origin) from None
        except UnsupportedJobBoardError:
            raise _error(400, "unsupported_job_board", origin=origin) from None
        except JobListingFetchError:
            raise _error(502, "collection_failed", origin=origin) from None
        except HTTPException:
            raise
        except Exception:
            raise _error(502, "collection_failed", origin=origin) from None
    finally:
        _WEB_JOB_LOCK.release()
    return Response(
        content=content,
        media_type="application/json",
        headers=_cors_headers(origin),
    )
