"""Strict parser and anonymous reader for Eleduck's public jobs listing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .models import FeedItem
from .parser import (
    MAX_DESCRIPTION_CHARS,
    MAX_TITLE_CHARS,
    EleduckFeedError,
    canonicalize_post_url,
    sanitize_public_text,
)


MAX_LISTING_PAGE_BYTES = 2_000_000
LISTING_PAGE_SIZE = 25
MAX_PUBLIC_API_PAGES = 20
JOBS_API_URL = "https://svc.eleduck.com/api/v1/jobs_channel/posts"
USER_AGENT = "czc-job-eleduck-listing/1.0 (+public-list-only)"
TIMEOUT_SECONDS = 15
_POST_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")


class EleduckApiHttpError(EleduckFeedError):
    """Safe transport failure for the fixed public jobs endpoint."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True)
class ParsedListingPage:
    items: tuple[FeedItem, ...]
    identities: tuple[str, ...]
    current_page: int
    next_page: int | None


def load_public_job_page(page: int) -> bytes:
    if (
        not isinstance(page, int)
        or isinstance(page, bool)
        or not 1 <= page <= MAX_PUBLIC_API_PAGES
    ):
        raise ValueError("Eleduck listing page is outside the allowed range")
    query = urlencode({"page": page, "sort": "-published_at"})
    expected_url = f"{JOBS_API_URL}?{query}"
    request = Request(expected_url, headers={"Accept": "application/json"})
    request.add_header("User-Agent", USER_AGENT)
    try:
        opener = build_opener(_RejectRedirects())
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.geturl() != expected_url:
                raise EleduckApiHttpError(
                    "Eleduck listing redirected outside its boundary"
                )
            if response.headers.get_content_type() != "application/json":
                raise EleduckApiHttpError(
                    "Eleduck listing returned a non-JSON response"
                )
            payload = response.read(MAX_LISTING_PAGE_BYTES + 1)
    except EleduckApiHttpError:
        raise
    except Exception as exc:
        raise EleduckApiHttpError("Eleduck listing request failed") from exc
    if len(payload) > MAX_LISTING_PAGE_BYTES:
        raise EleduckApiHttpError("Eleduck listing page exceeds the size limit")
    return payload


def parse_public_listing_page(
    payload: bytes,
    *,
    expected_page: int,
) -> ParsedListingPage:
    if len(payload) > MAX_LISTING_PAGE_BYTES:
        raise EleduckFeedError("Eleduck listing page exceeds the size limit")
    try:
        document = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EleduckFeedError("Eleduck listing page is not valid JSON") from exc
    if not isinstance(document, dict):
        raise EleduckFeedError("Eleduck listing response is invalid")
    posts = document.get("posts")
    pager = document.get("pager")
    if not isinstance(posts, list) or len(posts) > LISTING_PAGE_SIZE:
        raise EleduckFeedError("Eleduck listing posts are invalid")
    if not isinstance(pager, dict):
        raise EleduckFeedError("Eleduck listing pager is invalid")
    current_page = _positive_int(pager.get("current_page"), "current page")
    total_pages = _positive_int(pager.get("total_pages"), "total pages")
    limit_value = _positive_int(pager.get("limit_value"), "page limit")
    if (
        current_page != expected_page
        or total_pages < current_page
        or limit_value != LISTING_PAGE_SIZE
    ):
        raise EleduckFeedError("Eleduck listing pager does not match the request")
    next_page_value = pager.get("next_page")
    if next_page_value is None:
        if current_page < total_pages:
            raise EleduckFeedError("Eleduck listing next page is missing")
        next_page = None
    else:
        next_page = _positive_int(next_page_value, "next page")
        if next_page != current_page + 1 or next_page > total_pages:
            raise EleduckFeedError("Eleduck listing next page is invalid")

    items: list[FeedItem] = []
    identities: list[str] = []
    for post in posts:
        item, active = _parse_listing_post(post)
        identities.append(item.identity)
        if active:
            items.append(item)
    if len(set(identities)) != len(identities):
        raise EleduckFeedError("Eleduck listing page contains duplicate posts")
    return ParsedListingPage(
        items=tuple(items),
        identities=tuple(identities),
        current_page=current_page,
        next_page=next_page,
    )


def _parse_listing_post(value: object) -> tuple[FeedItem, bool]:
    if not isinstance(value, dict):
        raise EleduckFeedError("Eleduck listing post is invalid")
    post_id = value.get("id")
    if not isinstance(post_id, str) or not _POST_ID.fullmatch(post_id):
        raise EleduckFeedError("Eleduck listing post ID is invalid")
    for key in ("closed", "deleted", "hide"):
        if not isinstance(value.get(key), bool):
            raise EleduckFeedError("Eleduck listing status is invalid")
    status = value.get("status")
    visibility = value.get("visibility")
    if not isinstance(status, str) or not isinstance(visibility, str):
        raise EleduckFeedError("Eleduck listing status is invalid")
    category = value.get("category")
    tags = value.get("tags")
    if not isinstance(category, dict) or not isinstance(tags, list):
        raise EleduckFeedError("Eleduck listing classification is invalid")
    for key in ("code", "kind"):
        if not isinstance(category.get(key), str):
            raise EleduckFeedError("Eleduck listing category is invalid")

    canonical_url = canonicalize_post_url(f"https://eleduck.com/posts/{post_id}")
    full_title = value.get("full_title")
    title = (
        full_title
        if isinstance(full_title, str) and full_title.strip()
        else value.get("title")
    )
    if not isinstance(title, str) or not isinstance(value.get("summary"), str):
        raise EleduckFeedError("Eleduck listing text is invalid")
    published_raw = value.get("published_at")
    if not isinstance(published_raw, str):
        raise EleduckFeedError("Eleduck listing published time is invalid")
    published_at = _parse_published_at(published_raw)
    item = FeedItem(
        guid=canonical_url,
        canonical_url=canonical_url,
        title=sanitize_public_text(title, max_chars=MAX_TITLE_CHARS),
        description=sanitize_public_text(
            value.get("summary"),
            max_chars=MAX_DESCRIPTION_CHARS,
        ),
        published_at=published_at,
        pub_date_raw=sanitize_public_text(published_raw, max_chars=120),
    )
    active = (
        not value["closed"]
        and not value["deleted"]
        and not value["hide"]
        and status == "published"
        and visibility == "public"
    )
    return item, active


def _parse_published_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EleduckFeedError("Eleduck listing published time is invalid") from exc
    if parsed.tzinfo is None:
        raise EleduckFeedError("Eleduck listing published time lacks a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _positive_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise EleduckFeedError(f"Eleduck listing {field} is invalid")
    return value
