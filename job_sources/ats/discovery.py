"""Bounded ATS tenant discovery from the public Common Crawl URL index."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Literal, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


AtsProvider = Literal[
    "greenhouse",
    "lever",
    "ashby",
    "beisen",
    "smartrecruiters",
    "workable",
]
_INDEX_ORIGIN = ("https", "index.commoncrawl.org", 443)
_COLLECTIONS_URL = "https://index.commoncrawl.org/collinfo.json"
_CACHE_TTL = timedelta(hours=24)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_TENANT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_DNS_LABEL_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_CRAWL_PATTERN = re.compile(r"CC-MAIN-\d{4}-\d{2}\Z")
_INDEX_PATTERNS = (
    "boards.greenhouse.io/*",
    "boards.eu.greenhouse.io/*",
    "job-boards.greenhouse.io/*",
    "jobs.lever.co/*",
    "jobs.eu.lever.co/*",
    "jobs.ashbyhq.com/*",
    "careers.smartrecruiters.com/*",
    "jobs.smartrecruiters.com/*/*",
    "apply.workable.com/*",
    "apply.workable.com/*/j/*",
    "*.zhiye.com/*",
)


class AtsDiscoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AtsBoard:
    provider: AtsProvider
    tenant: str
    career_url: str
    company: str | None = None


@dataclass(frozen=True)
class AtsBoardCatalog:
    boards: tuple[AtsBoard, ...]
    crawl: str
    discovered_at: str
    cached: bool


def boards_from_index_records(
    records: Iterable[Mapping[str, object]],
    *,
    limit: int = 50,
) -> tuple[AtsBoard, ...]:
    """Convert public URL-index records into canonical supported ATS boards."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("invalid_limit")
    boards: dict[tuple[str, str], AtsBoard] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        value = record.get("url")
        if not isinstance(value, str):
            continue
        board = _board_from_url(value)
        if board is None:
            continue
        key = (board.provider, board.tenant.casefold())
        current = boards.get(key)
        if current is None or _board_preference(board) < _board_preference(current):
            boards[key] = board
    return tuple(
        sorted(boards.values(), key=_board_sort_key)[:limit]
    )


def discover_ats_boards(
    *,
    limit: int = 50,
    refresh: bool = False,
    cache_path: str | Path = Path(".runtime") / "ats-boards.json",
    fetch_text: Callable[[str], str] | None = None,
    now: datetime | None = None,
) -> AtsBoardCatalog:
    """Discover a bounded tenant catalog, reusing a fresh local cache."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("invalid_limit")
    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        raise ValueError("invalid_time")
    destination = Path(cache_path)
    if not refresh:
        cached = _read_cache(destination, now=current_time, limit=limit)
        if cached is not None:
            return cached

    load_text = fetch_text or _fetch_index_text
    try:
        collections = json.loads(load_text(_COLLECTIONS_URL))
        if not isinstance(collections, list) or not collections:
            raise ValueError("invalid collections")
        crawl = collections[0].get("id") if isinstance(collections[0], dict) else None
        if not isinstance(crawl, str) or not _CRAWL_PATTERN.fullmatch(crawl):
            raise ValueError("invalid crawl")
    except Exception:
        raise AtsDiscoveryError("discovery_failed") from None

    records: list[dict[str, object]] = []
    successful_queries = 0
    for pattern in _INDEX_PATTERNS:
        query = urlencode(
            (
                ("url", pattern),
                ("output", "json"),
                ("fl", "url,status,mime"),
                ("filter", "status:200"),
                ("filter", "mime:text/html"),
                ("limit", str(limit)),
            )
        )
        url = f"https://index.commoncrawl.org/{crawl}-index?{query}"
        try:
            text = load_text(url)
            successful_queries += 1
        except Exception:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
    discovered = boards_from_index_records(records, limit=limit)
    if not successful_queries:
        raise AtsDiscoveryError("discovery_failed")
    return _store_catalog(
        destination,
        boards=discovered,
        crawl=crawl,
        discovered_at=current_time,
    )


def _board_from_url(value: str) -> AtsBoard | None:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]

    if hostname in {
        "boards.greenhouse.io",
        "boards.eu.greenhouse.io",
        "job-boards.greenhouse.io",
    }:
        if not segments or not _TENANT_PATTERN.fullmatch(segments[0]):
            return None
        tenant = segments[0].casefold()
        return AtsBoard(
            "greenhouse",
            tenant,
            f"https://job-boards.greenhouse.io/{tenant}",
        )
    if hostname in {"jobs.lever.co", "jobs.eu.lever.co"}:
        if not segments or not _TENANT_PATTERN.fullmatch(segments[0]):
            return None
        tenant = segments[0]
        return AtsBoard("lever", tenant, f"https://{hostname}/{tenant}")
    if hostname == "jobs.ashbyhq.com":
        if not segments or not _TENANT_PATTERN.fullmatch(segments[0]):
            return None
        tenant = segments[0]
        return AtsBoard("ashby", tenant, f"https://jobs.ashbyhq.com/{tenant}")
    if hostname in {
        "careers.smartrecruiters.com",
        "jobs.smartrecruiters.com",
    }:
        if not segments or not _TENANT_PATTERN.fullmatch(segments[0]):
            return None
        tenant = segments[0].casefold()
        return AtsBoard(
            "smartrecruiters",
            tenant,
            f"https://careers.smartrecruiters.com/{tenant}",
        )
    if hostname == "apply.workable.com":
        if (
            not segments
            or segments[0].casefold() == "j"
            or not _TENANT_PATTERN.fullmatch(segments[0])
        ):
            return None
        tenant = segments[0].casefold()
        return AtsBoard(
            "workable",
            tenant,
            f"https://apply.workable.com/{tenant}",
        )
    if _is_beisen_host(hostname):
        return AtsBoard("beisen", hostname, f"https://{hostname}/jobs")
    return None


def _board_preference(board: AtsBoard) -> tuple[int, str]:
    host = urlsplit(board.career_url).hostname
    preferred = host in {
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "careers.smartrecruiters.com",
        "apply.workable.com",
    }
    return (0 if preferred else 1, board.career_url.casefold())


def _board_sort_key(board: AtsBoard) -> tuple[int, str, str]:
    provider_order = {
        "beisen": 0,
        "ashby": 1,
        "greenhouse": 2,
        "lever": 3,
        "smartrecruiters": 4,
        "workable": 5,
    }
    return (
        provider_order[board.provider],
        board.tenant.casefold(),
        board.career_url.casefold(),
    )


def _is_beisen_host(hostname: str) -> bool:
    if not hostname.endswith(".zhiye.com") or len(hostname) > 253:
        return False
    labels = hostname.removesuffix(".zhiye.com").split(".")
    return bool(labels) and all(_DNS_LABEL_PATTERN.fullmatch(label) for label in labels)


def _store_catalog(
    path: Path,
    *,
    boards: Iterable[AtsBoard],
    crawl: str,
    discovered_at: datetime,
) -> AtsBoardCatalog:
    catalog = AtsBoardCatalog(
        boards=tuple(boards),
        crawl=crawl,
        discovered_at=discovered_at.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ),
        cached=False,
    )
    _write_cache(path, catalog)
    return catalog


def _read_cache(path: Path, *, now: datetime, limit: int) -> AtsBoardCatalog | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        discovered_at = datetime.fromisoformat(payload["discovered_at"])
        if discovered_at.tzinfo is None:
            return None
        age = now.astimezone(timezone.utc) - discovered_at.astimezone(timezone.utc)
        if age < timedelta(0) or age > _CACHE_TTL:
            return None
        crawl = payload["crawl"]
        values = payload["boards"]
        if not isinstance(crawl, str) or not _CRAWL_PATTERN.fullmatch(crawl):
            return None
        if not isinstance(values, list):
            return None
        boards: list[AtsBoard] = []
        for value in values:
            if not isinstance(value, dict):
                return None
            career_url = value.get("career_url")
            if not isinstance(career_url, str):
                return None
            board = _board_from_url(career_url)
            if board is None:
                return None
            if value.get("provider") != board.provider:
                return None
            tenant = value.get("tenant")
            if not isinstance(tenant, str) or tenant.casefold() != board.tenant.casefold():
                return None
            company = value.get("company")
            if company is not None and (
                not isinstance(company, str) or not company.strip() or len(company) > 200
            ):
                return None
            boards.append(
                AtsBoard(
                    board.provider,
                    board.tenant,
                    board.career_url,
                    company.strip() if isinstance(company, str) else None,
                )
            )
        return AtsBoardCatalog(
            boards=tuple(boards[:limit]),
            crawl=crawl,
            discovered_at=discovered_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
            cached=True,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _write_cache(path: Path, catalog: AtsBoardCatalog) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        payload = {
            "version": 1,
            "crawl": catalog.crawl,
            "discovered_at": catalog.discovered_at,
            "boards": [asdict(board) for board in catalog.boards],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        return


def _fetch_index_text(url: str) -> str:
    if _origin(url) != _INDEX_ORIGIN:
        raise AtsDiscoveryError("discovery_failed")
    try:
        request = Request(
            url,
            headers={
                "Accept": "application/json, application/x-ndjson, text/plain",
                "User-Agent": "czc-good-job-ats-discovery/1.0",
            },
            method="GET",
        )
        opener = build_opener(_SameOriginRedirectHandler(_INDEX_ORIGIN))
        with opener.open(request, timeout=10.0) as response:
            if response.status != 200 or _origin(response.geturl()) != _INDEX_ORIGIN:
                raise ValueError("unexpected response")
            body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        return body.decode("utf-8")
    except Exception:
        raise AtsDiscoveryError("discovery_failed") from None


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    return scheme, hostname, parsed.port or (443 if scheme == "https" else 80)


class _SameOriginRedirectHandler(HTTPRedirectHandler):
    max_redirections = 2
    max_repeats = 2

    def __init__(self, expected_origin: tuple[str, str, int]) -> None:
        super().__init__()
        self.expected_origin = expected_origin

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved_url = urljoin(req.full_url, newurl)
        if _origin(resolved_url) != self.expected_origin:
            raise HTTPError(resolved_url, code, "redirect rejected", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, resolved_url)


__all__ = [
    "AtsBoard",
    "AtsBoardCatalog",
    "AtsDiscoveryError",
    "AtsProvider",
    "boards_from_index_records",
    "discover_ats_boards",
]
