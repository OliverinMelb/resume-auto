"""Safe, bounded parsing for Eleduck's public RSS document."""

from __future__ import annotations

import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from .models import FeedItem


MAX_FEED_BYTES = 1_000_000
MAX_TITLE_CHARS = 300
MAX_DESCRIPTION_CHARS = 4_000
_POST_PATH = re.compile(r"^/posts/[A-Za-z0-9_-]{4,64}/?$")
_UNSAFE_XML = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_EMAIL = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])",
    re.IGNORECASE,
)
_MOBILE = re.compile(r"(?<!\d)1[3-9]\d(?:[ \t-]?\d){8}(?!\d)")
_LABELED_CONTACT = re.compile(
    r"(?i)\b(?:wechat|weixin|wx|qq)\b\s*[:：=]?\s*[A-Za-z0-9_-]{4,32}"
    r"|(?:微信(?:号)?|微\s*信(?:号)?|QQ(?:号)?)"
    r"\s*[:：=]?\s*[A-Za-z0-9_-]{4,32}"
)
_SECRET = re.compile(
    r"(?i)\b(?:authorization|cookie|token|api[_-]?key|secret)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;，；]+"
)
MAX_ELEMENT_DEPTH = 64


class EleduckSourceError(RuntimeError):
    """Safe base error for this source; messages never contain feed payloads."""


class EleduckFeedError(EleduckSourceError):
    """The fetched public feed is invalid or outside the accepted boundary."""


@dataclass(frozen=True)
class ParsedFeed:
    items: tuple[FeedItem, ...]
    invalid_item_count: int


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.lower() in {"script", "style"}:
            self._suppressed_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed_depth:
            self.parts.append(data)


def sanitize_public_text(value: str | None, *, max_chars: int) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(value or "")
        extractor.close()
    except (ValueError, RecursionError):
        return ""
    text = " ".join(extractor.parts)
    text = _CONTROL.sub(" ", text)
    text = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in text
    )
    text = _EMAIL.sub("[redacted-email]", text)
    text = _MOBILE.sub("[redacted-phone]", text)
    text = _LABELED_CONTACT.sub("[redacted-contact]", text)
    text = _SECRET.sub("[redacted-secret]", text)
    text = _SPACE.sub(" ", text).strip()
    return text[:max_chars]


def canonicalize_post_url(value: str | None) -> str:
    raw = (value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("invalid Eleduck post URL") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "eleduck.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _POST_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("invalid Eleduck post URL")
    path = parsed.path.rstrip("/")
    return urlunsplit(("https", "eleduck.com", path, "", ""))


def parse_feed(payload: bytes) -> ParsedFeed:
    if len(payload) > MAX_FEED_BYTES:
        raise EleduckFeedError("Eleduck feed exceeds the size limit")
    try:
        document = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EleduckFeedError("Eleduck feed is not valid UTF-8") from exc
    if _UNSAFE_XML.search(document):
        raise EleduckFeedError("Eleduck feed contains a forbidden XML declaration")
    try:
        root = ET.fromstring(document)
    except (ET.ParseError, ValueError) as exc:
        raise EleduckFeedError("Eleduck feed is not valid XML") from exc
    if _local_name(root.tag) != "rss":
        raise EleduckFeedError("Eleduck feed is not RSS")
    channel = next(
        (child for child in root if _local_name(child.tag) == "channel"),
        None,
    )
    if channel is None:
        raise EleduckFeedError("Eleduck RSS channel is missing")

    items: list[FeedItem] = []
    invalid_item_count = 0
    for element in channel:
        if _local_name(element.tag) != "item":
            continue
        try:
            items.append(_parse_item(element))
        except (ValueError, OverflowError):
            invalid_item_count += 1
    return ParsedFeed(tuple(items), invalid_item_count)


def _parse_item(element: ET.Element) -> FeedItem:
    fields: dict[str, str] = {}
    for child in element:
        name = _local_name(child.tag)
        if name in {"title", "description", "link", "guid", "pubDate"}:
            fields[name] = _safe_element_text(child)

    guid = sanitize_public_text(fields.get("guid"), max_chars=300) or None
    pub_date_raw = sanitize_public_text(fields.get("pubDate"), max_chars=120) or None
    published_at = _parse_pub_date(pub_date_raw)
    return FeedItem(
        guid=guid,
        canonical_url=canonicalize_post_url(fields.get("link")),
        title=sanitize_public_text(fields.get("title"), max_chars=MAX_TITLE_CHARS),
        description=sanitize_public_text(
            fields.get("description"),
            max_chars=MAX_DESCRIPTION_CHARS,
        ),
        published_at=published_at,
        pub_date_raw=pub_date_raw,
    )


def _parse_pub_date(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _safe_element_text(element: ET.Element) -> str:
    parts: list[str] = []
    stack: list[tuple[str, ET.Element | str, int]] = [
        ("element", element, 0)
    ]
    while stack:
        kind, value, depth = stack.pop()
        if kind == "text":
            parts.append(str(value))
            continue
        node = value
        if not isinstance(node, ET.Element):
            raise ValueError("invalid RSS element")
        if depth > MAX_ELEMENT_DEPTH:
            raise ValueError("RSS item markup is too deeply nested")
        if _local_name(node.tag).lower() in {"script", "style"}:
            continue
        if node.text:
            parts.append(node.text)
        for child in reversed(node):
            if child.tail:
                stack.append(("text", child.tail, depth))
            stack.append(("element", child, depth + 1))
    return "".join(parts)
