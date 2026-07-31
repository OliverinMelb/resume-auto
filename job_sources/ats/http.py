"""Controlled HTTP entry point for fetching one approved ATS page."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, replace
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request
from urllib.robotparser import RobotFileParser

from .detector import AtsDetection, detect_ats


@dataclass(frozen=True)
class AtsHttpPolicy:
    """Limits for a single, caller-approved HTTP detection."""

    timeout_seconds: float = 10.0
    max_html_bytes: int = 2_000_000
    max_robots_bytes: int = 256_000
    max_redirects: int = 3
    user_agent: str = "czc-good-job-ats-detector/0.1"

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_html_bytes <= 0 or self.max_robots_bytes <= 0:
            raise ValueError("response byte limits must be positive")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if not self.user_agent.strip():
            raise ValueError("user_agent cannot be empty")


@dataclass(frozen=True)
class _HttpResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class _ValidatedUrl:
    url: str
    scheme: str
    netloc: str
    hostname: str
    port: int
    addresses: tuple[str, ...]

    @property
    def request_target(self) -> str:
        parsed = urlsplit(self.url)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return target


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        address: str,
        *,
        timeout: float,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(
        self,
        hostname: str,
        port: int,
        address: str,
        *,
        timeout: float,
        context=None,
    ) -> None:
        super().__init__(
            hostname,
            port,
            timeout=timeout,
            context=context,
        )
        self._validated_address = address

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._validated_address, self.port),
            self.timeout,
            self.source_address,
        )
        try:
            self.sock = self._context.wrap_socket(
                raw_socket,
                server_hostname=self.host,
            )
        except BaseException:
            raw_socket.close()
            raise


class _DirectResponse:
    def __init__(self, url: str, response, connection) -> None:
        self.status = response.status
        self.headers = response.headers
        self._url = url
        self._response = response
        self._connection = connection

    def read(self, amount: int = -1) -> bytes:
        return self._response.read(amount)

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._connection.close()


class _PinnedOpener:
    def __init__(self, approved: _ValidatedUrl) -> None:
        self._approved = approved

    def open(self, request: Request, timeout: float) -> _DirectResponse:
        validated = _same_origin_url(request.full_url, self._approved)
        if validated is None:
            raise _UnsafeRedirect

        last_error: OSError | None = None
        connection = None
        for address in validated.addresses:
            candidate = _connection_for(
                validated,
                address,
                timeout=timeout,
            )
            try:
                candidate.connect()
            except OSError as error:
                candidate.close()
                last_error = error
                continue
            connection = candidate
            break
        if connection is None:
            raise last_error or OSError("no validated address was reachable")

        headers = dict(request.header_items())
        try:
            connection.request(
                "GET",
                validated.request_target,
                headers=headers,
            )
            response = connection.getresponse()
        except BaseException:
            connection.close()
            raise
        return _DirectResponse(validated.url, response, connection)


def build_opener(approved: _ValidatedUrl) -> _PinnedOpener:
    """Build a direct opener that cannot inherit environment proxy settings."""
    return _PinnedOpener(approved)


class _UnsafeRedirect(Exception):
    pass


class _TooManyRedirects(Exception):
    pass


class _ResponseTooLarge(Exception):
    pass


class _RobotsDisallowed(Exception):
    pass


def detect_ats_url(
    url: str,
    *,
    policy: AtsHttpPolicy = AtsHttpPolicy(),
) -> AtsDetection:
    """Fetch and inspect one public URL without discovering other endpoints."""
    approved_url = urldefrag(url)[0]
    approved = _safe_public_url(approved_url)
    if approved is None:
        return _blocked_result(approved_url, "unsafe_host")

    opener = build_opener(approved)
    robots_url = (
        f"{approved.scheme}://{approved.netloc}/robots.txt"
    )
    try:
        robots = _open_following_redirects(
            opener,
            robots_url,
            policy=policy,
            max_bytes=policy.max_robots_bytes,
            accept="text/plain,*/*;q=0.1",
            approved=approved,
        )
    except _UnsafeRedirect:
        return _blocked_result(approved_url, "unsafe_redirect")
    except _TooManyRedirects:
        return _blocked_result(approved_url, "too_many_redirects")
    except _ResponseTooLarge:
        return _blocked_result(approved_url, "robots_unavailable")
    except (OSError, TimeoutError, HTTPException):
        return _blocked_result(approved_url, "robots_unavailable")

    if robots.status in {401, 403}:
        return _blocked_result(approved_url, "robots_disallowed")
    if robots.status not in {200, 404, 410}:
        return _blocked_result(approved_url, "robots_unavailable")
    robots_parser = None
    if robots.status == 200:
        robots_parser = RobotFileParser()
        robots_parser.set_url(robots_url)
        try:
            robots_text = robots.body.decode("utf-8", errors="replace")
            robots_parser.parse(robots_text.splitlines())
        except ValueError:
            return _blocked_result(approved_url, "robots_unavailable")
        if not robots_parser.can_fetch(policy.user_agent, approved_url):
            return _blocked_result(approved_url, "robots_disallowed")

    try:
        page = _open_following_redirects(
            opener,
            approved_url,
            policy=policy,
            max_bytes=policy.max_html_bytes,
            accept="text/html,application/xhtml+xml",
            approved=approved,
            robots_parser=robots_parser,
        )
    except _RobotsDisallowed:
        return _blocked_result(approved_url, "robots_disallowed")
    except _UnsafeRedirect:
        return _blocked_result(approved_url, "unsafe_redirect")
    except _TooManyRedirects:
        return _blocked_result(approved_url, "too_many_redirects")
    except _ResponseTooLarge:
        return _blocked_result(approved_url, "response_too_large")
    except (OSError, TimeoutError, HTTPException):
        return _blocked_result(approved_url, "http_unavailable")

    if not 200 <= page.status < 300:
        reason = (
            "rate_limited"
            if page.status == 429
            else f"http_{page.status}"
        )
        return _blocked_result(approved_url, reason)
    content_type = page.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return _blocked_result(approved_url, "unsupported_content_type")
    charset = _charset_from_content_type(content_type)
    try:
        html = page.body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return _blocked_result(approved_url, "decode_error")
    return detect_ats(
        page.url,
        html,
        status_code=page.status,
        headers=page.headers,
    )


def _open_following_redirects(
    opener,
    url: str,
    *,
    policy: AtsHttpPolicy,
    max_bytes: int,
    accept: str,
    approved: _ValidatedUrl,
    robots_parser: RobotFileParser | None = None,
) -> _HttpResponse:
    current_url = url
    redirects = 0
    while True:
        response = _open_once(
            opener,
            current_url,
            policy=policy,
            max_bytes=max_bytes,
            accept=accept,
        )
        if response.status not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("location")
        if not location:
            return response
        if redirects >= policy.max_redirects:
            raise _TooManyRedirects

        next_url = urldefrag(urljoin(current_url, location))[0]
        if _same_origin_url(next_url, approved) is None:
            raise _UnsafeRedirect
        if (
            robots_parser is not None
            and not robots_parser.can_fetch(policy.user_agent, next_url)
        ):
            raise _RobotsDisallowed
        current_url = next_url
        redirects += 1


def _open_once(
    opener,
    url: str,
    *,
    policy: AtsHttpPolicy,
    max_bytes: int,
    accept: str,
) -> _HttpResponse:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": policy.user_agent,
        },
    )
    response = opener.open(request, timeout=policy.timeout_seconds)
    try:
        status = int(response.status)
        headers = {
            str(name).casefold(): str(value)
            for name, value in response.headers.items()
        }
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > max_bytes:
                    raise _ResponseTooLarge
            except ValueError:
                pass
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise _ResponseTooLarge
        return _HttpResponse(
            url=response.geturl(),
            status=status,
            headers=headers,
            body=body,
        )
    finally:
        response.close()


def _safe_public_url(url: str) -> _ValidatedUrl | None:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in url)
    ):
        return None
    scheme = parsed.scheme.casefold()
    expected_port = 443 if scheme == "https" else 80
    if port is not None and port != expected_port:
        return None

    try:
        normalized_host = (
            hostname.rstrip(".").casefold().encode("idna").decode("ascii")
        )
    except UnicodeError:
        return None
    if (
        normalized_host == "localhost"
        or normalized_host.endswith(".localhost")
        or normalized_host.endswith(".local")
        or normalized_host.endswith(".internal")
    ):
        return None
    try:
        addresses = socket.getaddrinfo(
            normalized_host,
            expected_port,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return None
    if not addresses:
        return None
    validated_addresses: list[str] = []
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError:
            return None
        if not ip.is_global:
            return None
        validated_addresses.append(str(ip))
    return _ValidatedUrl(
        url=url,
        scheme=scheme,
        netloc=parsed.netloc,
        hostname=normalized_host,
        port=expected_port,
        addresses=tuple(dict.fromkeys(validated_addresses)),
    )


def _same_origin_url(
    url: str,
    approved: _ValidatedUrl,
) -> _ValidatedUrl | None:
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(ord(character) < 32 for character in url)
    ):
        return None
    try:
        normalized_host = (
            hostname.rstrip(".").casefold().encode("idna").decode("ascii")
        )
    except UnicodeError:
        return None
    scheme = parsed.scheme.casefold()
    effective_port = port or (443 if scheme == "https" else 80)
    if (
        scheme != approved.scheme
        or normalized_host != approved.hostname
        or effective_port != approved.port
        or (port is not None and port != approved.port)
    ):
        return None
    return _ValidatedUrl(
        url=url,
        scheme=scheme,
        netloc=parsed.netloc,
        hostname=normalized_host,
        port=effective_port,
        addresses=approved.addresses,
    )


def _connection_for(
    validated: _ValidatedUrl,
    address: str,
    *,
    timeout: float,
):
    connection_type = (
        _PinnedHTTPSConnection
        if validated.scheme == "https"
        else _PinnedHTTPConnection
    )
    return connection_type(
        validated.hostname,
        validated.port,
        address,
        timeout=timeout,
    )


def _charset_from_content_type(content_type: str) -> str:
    for item in content_type.split(";")[1:]:
        name, separator, value = item.partition("=")
        if separator and name.strip().casefold() == "charset":
            return value.strip().strip("\"'") or "utf-8"
    return "utf-8"


def _blocked_result(url: str, reason: str) -> AtsDetection:
    return replace(
        detect_ats(url, ""),
        blocked_reason=reason,
        requires_browser=False,
    )
