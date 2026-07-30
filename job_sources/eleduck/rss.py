"""Anonymous HTTP reader for Eleduck's one allowlisted public RSS URL."""

from __future__ import annotations

from urllib.request import HTTPRedirectHandler, Request, build_opener

from .parser import EleduckFeedError, EleduckSourceError, MAX_FEED_BYTES


FEED_URL = "https://eleduck.com/feed/latest.xml"
USER_AGENT = "czc-job-eleduck-rss/1.0 (+public-feed-only)"
TIMEOUT_SECONDS = 15
_XML_CONTENT_TYPES = {
    "application/rss+xml",
    "application/xml",
    "text/xml",
}


class EleduckHttpError(EleduckSourceError):
    """A safe transport-level failure for the allowlisted public feed."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ):
        del req, fp, code, msg, headers, newurl
        return None


def load_public_feed() -> bytes:
    request = Request(
        FEED_URL,
        headers={"Accept": "application/rss+xml, application/xml;q=0.9"},
    )
    request.add_header("User-Agent", USER_AGENT)
    try:
        opener = build_opener(_RejectRedirects())
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.geturl() != FEED_URL:
                raise EleduckHttpError("Eleduck feed redirected outside its boundary")
            content_type = response.headers.get_content_type()
            if content_type not in _XML_CONTENT_TYPES:
                raise EleduckHttpError("Eleduck feed returned a non-XML response")
            payload = response.read(MAX_FEED_BYTES + 1)
    except EleduckSourceError:
        raise
    except Exception as exc:
        raise EleduckHttpError("Eleduck feed request failed") from exc
    if len(payload) > MAX_FEED_BYTES:
        raise EleduckFeedError("Eleduck feed exceeds the size limit")
    return payload
