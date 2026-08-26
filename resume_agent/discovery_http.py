"""Loopback-only JSON client for independent discovery source services."""

from __future__ import annotations

import json
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class JsonTransport(Protocol):
    def request_json(
        self,
        method: str,
        url: str,
        payload: dict | None,
        timeout_seconds: int,
    ) -> dict:
        """Issue one JSON request and return its JSON object response."""


class UrllibJsonTransport:
    """Minimal stdlib transport used only after the endpoint passed loopback checks."""

    def __init__(self, *, opener=None):
        self.proxy_handler = ProxyHandler({})
        self.redirect_handler = _NoRedirectHandler()
        self.opener = opener or build_opener(
            self.proxy_handler, self.redirect_handler
        )

    def request_json(
        self,
        method: str,
        url: str,
        payload: dict | None,
        timeout_seconds: int,
    ) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            url,
            data=data,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        with self.opener.open(request, timeout=timeout_seconds) as response:
            decoded = json.loads(response.read().decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("discovery response must be a JSON object")
        return decoded


class LocalJsonClient:
    """A JSON client that cannot be pointed at anything except loopback HTTP."""

    def __init__(self, base_url: str, *, timeout_seconds: int = 30, transport=None):
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or not _is_loopback_host(parsed.hostname)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("discovery endpoint must be loopback HTTP")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.transport: JsonTransport = transport or UrllibJsonTransport()

    def request_json(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("discovery path must be absolute and local")
        return self.transport.request_json(
            method, self.base_url + path, payload, self.timeout_seconds
        )


def _is_loopback_host(hostname: str | None) -> bool:
    return hostname is not None and (
        hostname.casefold() == "localhost" or hostname in {"127.0.0.1", "::1"}
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject redirects instead of creating a request to a new destination."""

    def http_error_302(self, request, response, code, message, headers):
        raise HTTPError(
            request.full_url,
            code,
            "discovery redirects are not allowed",
            headers,
            response,
        )

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302
