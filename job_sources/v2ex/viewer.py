"""Local-only HTML viewer for the V2EX jobs source."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from html import escape
from threading import Lock
from time import monotonic
from typing import Callable, Iterable, Protocol, Sequence
from urllib.parse import parse_qs
from wsgiref.simple_server import (
    WSGIRequestHandler,
    make_server,
)

from .archive import V2exArchiveJobSource
from .source import V2exFetchResult, V2exItemOutcome


StartResponse = Callable[
    [str, list[tuple[str, str]]],
    object,
]


class _ViewerSource(Protocol):
    def fetch(self) -> V2exFetchResult: ...
_CLASSIFICATIONS = (
    "recruitment",
    "referral",
    "job_seeking",
    "outsourcing",
    "advertisement",
    "discussion",
    "uncertain",
)
_CLASSIFICATION_LABELS = {
    "recruitment": "正式招聘",
    "referral": "内推岗位",
    "job_seeking": "求职帖",
    "outsourcing": "外包需求",
    "advertisement": "广告",
    "discussion": "普通讨论",
    "uncertain": "证据不足",
}
_RISK_LABELS = {
    "community_post": "社区帖子",
    "identity_unverified": "身份未验证",
    "possible_outsourcing": "可能是外包",
    "missing_deadline": "未提供截止日期",
}
_REASON_LABELS = {
    "conflicting_category_signals": "分类信号存在冲突",
    "insufficient_job_evidence": "岗位证据不足，建议人工确认",
    "body_truncated": "正文过长，摘要已截断",
    "title_truncated": "标题过长，已安全截断",
    "id_from_url": "缺少有效 ID，已使用原文链接",
    "url_from_id": "原文链接无效，已使用 Feed ID",
}
_CACHE_TTL_SECONDS = 300.0
_MIN_REFRESH_SECONDS = 60.0
_MAX_QUERY_STRING_CHARS = 2_048
_MAX_SEARCH_CHARS = 100
_SECURITY_HEADERS = (
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'unsafe-inline'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)
_STYLES = """
:root {
  color-scheme: light;
  --ink: #17202a;
  --muted: #69727d;
  --line: #e5e7e8;
  --paper: #ffffff;
  --canvas: #f4f5f2;
  --accent: #f15a36;
  --accent-dark: #c93e20;
  --green: #18794e;
  --green-soft: #e9f6ef;
  --amber: #9a5b08;
  --amber-soft: #fff4d8;
  --blue: #245c92;
  --blue-soft: #eaf3fb;
  font-family: Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont,
    "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(circle at 92% 0%, #fee9df 0, transparent 25rem),
    var(--canvas);
}
a { color: inherit; }
button, input, select { font: inherit; }
button, .button-link {
  min-height: 42px;
  border: 0;
  border-radius: 12px;
  padding: 0 17px;
  font-weight: 720;
  cursor: pointer;
}
.shell { width: min(1180px, calc(100% - 32px)); margin: 0 auto; }
.hero { padding: 52px 0 28px; }
.eyebrow {
  display: flex;
  align-items: center;
  gap: 9px;
  color: var(--muted);
  font-size: 13px;
  font-weight: 750;
  letter-spacing: .08em;
  text-transform: uppercase;
}
.feed-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--green);
  box-shadow: 0 0 0 5px #dff2e8;
}
.hero-row {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 28px;
  margin-top: 15px;
}
h1 {
  max-width: 760px;
  margin: 0;
  font-size: clamp(34px, 6vw, 64px);
  line-height: .98;
  letter-spacing: -.052em;
}
.hero-copy {
  max-width: 680px;
  margin: 17px 0 0;
  color: var(--muted);
  font-size: 16px;
  line-height: 1.75;
}
.refresh-button {
  color: #fff;
  background: var(--ink);
  white-space: nowrap;
  box-shadow: 0 8px 24px #17202a24;
}
.refresh-button:hover { background: #2d3945; }
.stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-top: 34px;
}
.stat {
  display: flex;
  min-height: 92px;
  flex-direction: column;
  justify-content: space-between;
  padding: 17px 19px;
  border: 1px solid #ffffffb8;
  border-radius: 18px;
  background: #ffffffb8;
  box-shadow: 0 10px 35px #25313b0a;
  backdrop-filter: blur(12px);
}
.stat span { color: var(--muted); font-size: 13px; font-weight: 650; }
.stat strong { font-size: 30px; letter-spacing: -.04em; }
.filter-panel {
  position: sticky;
  z-index: 5;
  top: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 19px;
  background: #ffffffed;
  box-shadow: 0 14px 40px #17202a12;
  backdrop-filter: blur(16px);
}
.filters {
  display: grid;
  grid-template-columns: minmax(220px, 1.6fr) 1fr 1fr auto auto auto;
  gap: 10px;
  align-items: center;
}
.control {
  width: 100%;
  min-height: 44px;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0 13px;
  color: var(--ink);
  background: #fafaf8;
  outline: none;
}
.control:focus { border-color: var(--accent); box-shadow: 0 0 0 3px #f15a361a; }
.check {
  display: flex;
  min-height: 44px;
  align-items: center;
  gap: 7px;
  padding: 0 11px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: #fafaf8;
  font-size: 14px;
  font-weight: 650;
  white-space: nowrap;
}
.check input { accent-color: var(--accent); }
.filter-button { color: #fff; background: var(--accent); }
.filter-button:hover { background: var(--accent-dark); }
.reset-link {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 42px;
  padding: 0 8px;
  color: var(--muted);
  font-size: 14px;
  text-decoration: none;
}
.notice {
  margin: 18px 0 0;
  padding: 14px 17px;
  border: 1px solid #f0d8a6;
  border-radius: 14px;
  color: #73420a;
  background: var(--amber-soft);
}
.failure { border-color: #f0c6b9; color: #8c321b; background: #fff0eb; }
.results-head {
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 20px;
  padding: 32px 2px 16px;
}
.results-head h2 { margin: 0; font-size: 22px; letter-spacing: -.02em; }
.results-head p { margin: 6px 0 0; color: var(--muted); font-size: 14px; }
.result-count { color: var(--muted); font-size: 14px; white-space: nowrap; }
.job-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  padding-bottom: 48px;
}
.job-card {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 15px;
  padding: 22px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: var(--paper);
  box-shadow: 0 12px 36px #17202a0a;
}
.job-card:hover { border-color: #cfd3d5; box-shadow: 0 16px 42px #17202a12; }
.card-top, .tag-row, .meta-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 7px;
}
.badge, .tag {
  display: inline-flex;
  align-items: center;
  min-height: 27px;
  border-radius: 999px;
  padding: 4px 9px;
  font-size: 12px;
  font-weight: 720;
}
.badge-normalized { color: var(--green); background: var(--green-soft); }
.badge-review { color: var(--amber); background: var(--amber-soft); }
.badge-invalid { color: #963d3d; background: #faeaea; }
.classification { color: var(--blue); background: var(--blue-soft); }
.job-card h3 {
  margin: 0;
  font-size: 20px;
  line-height: 1.35;
  letter-spacing: -.018em;
  overflow-wrap: anywhere;
}
.company { margin: -7px 0 0; color: var(--muted); font-size: 14px; }
.meta {
  display: inline-flex;
  align-items: center;
  min-height: 30px;
  padding: 5px 9px;
  border: 1px solid var(--line);
  border-radius: 9px;
  color: #46515c;
  background: #fafaf8;
  font-size: 13px;
}
.summary {
  display: -webkit-box;
  margin: 0;
  overflow: hidden;
  color: #535e69;
  font-size: 14px;
  line-height: 1.72;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 4;
}
.tag { color: #56616c; background: #f1f2ef; }
.tag-risk { color: #87510b; background: #fff4d8; }
.review-note {
  margin: 0;
  padding-left: 11px;
  border-left: 3px solid #edc26f;
  color: #795019;
  font-size: 13px;
}
.card-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: auto;
  padding-top: 3px;
}
.source-link { color: var(--accent-dark); font-size: 14px; font-weight: 760; }
.source-link:hover { color: var(--accent); }
.confidence { color: var(--muted); font-size: 12px; }
.empty {
  grid-column: 1 / -1;
  padding: 54px 24px;
  border: 1px dashed #cbd0d2;
  border-radius: 20px;
  color: var(--muted);
  text-align: center;
  background: #ffffff8f;
}
.footer {
  padding: 4px 0 36px;
  color: var(--muted);
  font-size: 12px;
  text-align: center;
}
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
@media (max-width: 920px) {
  .filters { grid-template-columns: 1fr 1fr 1fr; }
  .search-control { grid-column: 1 / -1; }
}
@media (max-width: 720px) {
  .shell { width: min(100% - 22px, 1180px); }
  .hero { padding-top: 32px; }
  .hero-row { align-items: start; flex-direction: column; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .filter-panel { position: static; }
  .filters { grid-template-columns: 1fr 1fr; }
  .search-control, .wide-control { grid-column: 1 / -1; }
  .job-grid { grid-template-columns: 1fr; }
}
@media (max-width: 430px) {
  .filters { grid-template-columns: 1fr; }
  .search-control, .wide-control { grid-column: auto; }
  .stats { gap: 8px; }
  .stat { min-height: 82px; padding: 14px; }
  .job-card { padding: 18px; }
}
"""


class _QuietRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """Suppress request targets so queries never enter access logs."""


@dataclass(frozen=True)
class _ViewerFilters:
    query: str = ""
    classification: str = ""
    disposition: str = ""
    remote: bool = False
    internship: bool = False
    refresh: bool = False


class V2exViewerApp:
    """Render one V2EX source snapshot through a small WSGI interface."""

    def __init__(
        self,
        *,
        source: _ViewerSource | None = None,
        now: Callable[[], float] = monotonic,
        wall_now: Callable[[], datetime] | None = None,
    ):
        self._source = source or V2exArchiveJobSource()
        self._now = now
        self._wall_now = wall_now or (
            lambda: datetime.now().astimezone()
        )
        self._snapshot: V2exFetchResult | None = None
        self._snapshot_at: float | None = None
        self._snapshot_updated_at: str | None = None
        self._last_attempt_at: float | None = None
        self._refresh_error: str | None = None
        self._lock = Lock()

    def __call__(
        self,
        environ: dict[str, object],
        start_response: StartResponse,
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        if method not in {"GET", "HEAD"}:
            return _respond(
                start_response,
                status="405 Method Not Allowed",
                text="Method not allowed",
                method=method,
                extra_headers=(("Allow", "GET, HEAD"),),
            )
        if str(environ.get("PATH_INFO", "/")) != "/":
            return _respond(
                start_response,
                status="404 Not Found",
                text="Not found",
                method=method,
            )
        query_string = str(environ.get("QUERY_STRING", ""))
        if len(query_string) > _MAX_QUERY_STRING_CHARS:
            return _respond(
                start_response,
                status="400 Bad Request",
                text="Invalid query",
                method=method,
            )
        try:
            filters = _parse_filters(query_string)
        except ValueError:
            return _respond(
                start_response,
                status="400 Bad Request",
                text="Invalid query",
                method=method,
            )
        snapshot, refresh_error, updated_at = self._load_snapshot(
            force=filters.refresh
        )
        outcomes = tuple(
            outcome
            for outcome in snapshot.outcomes
            if _matches(outcome, filters)
        )
        body = _render_page(
            snapshot,
            outcomes=outcomes,
            filters=filters,
            refresh_error=refresh_error,
            updated_at=updated_at,
        ).encode("utf-8")
        return _respond_bytes(
            start_response,
            status=(
                "503 Service Unavailable"
                if snapshot.error_code is not None and not snapshot.stale
                else "200 OK"
            ),
            body=body,
            content_type="text/html; charset=utf-8",
            method=method,
        )

    def _load_snapshot(
        self,
        *,
        force: bool,
    ) -> tuple[V2exFetchResult, str | None, str | None]:
        with self._lock:
            current_time = self._now()
            since_attempt = (
                None
                if self._last_attempt_at is None
                else current_time - self._last_attempt_at
            )
            retry_due = (
                since_attempt is None
                or since_attempt >= _MIN_REFRESH_SECONDS
            )
            snapshot_age = (
                None
                if self._snapshot_at is None
                else current_time - self._snapshot_at
            )
            should_fetch = (
                self._snapshot is None
                or (
                    self._snapshot.error_code is not None
                    and retry_due
                )
                or (
                    snapshot_age is not None
                    and snapshot_age >= _CACHE_TTL_SECONDS
                    and retry_due
                )
                or (force and retry_due)
            )
            if should_fetch:
                self._last_attempt_at = current_time
                candidate = self._source.fetch()
                if candidate.error_code is None:
                    self._snapshot = candidate
                    self._snapshot_at = current_time
                    self._snapshot_updated_at = (
                        self._wall_now().strftime("%m-%d %H:%M")
                    )
                    self._refresh_error = None
                elif candidate.stale and candidate.outcomes:
                    self._snapshot = candidate
                    self._snapshot_at = current_time
                    self._snapshot_updated_at = (
                        _latest_archive_update(candidate)
                        or self._snapshot_updated_at
                    )
                    self._refresh_error = candidate.error_code
                else:
                    self._refresh_error = candidate.error_code
                    if (
                        self._snapshot is None
                        or self._snapshot.error_code is not None
                    ):
                        self._snapshot = candidate
                        self._snapshot_at = None
            if self._snapshot is None:
                raise RuntimeError("viewer snapshot was not initialized")
            return (
                self._snapshot,
                self._refresh_error,
                self._snapshot_updated_at,
            )


def _parse_filters(query_string: str) -> _ViewerFilters:
    values = parse_qs(
        query_string,
        keep_blank_values=False,
        max_num_fields=20,
    )
    classification = values.get("classification", [""])[0]
    if classification not in _CLASSIFICATIONS:
        classification = ""
    disposition = values.get("disposition", [""])[0]
    if disposition not in {"normalized", "review", "invalid"}:
        disposition = ""
    return _ViewerFilters(
        query=values.get("q", [""])[0].strip()[:_MAX_SEARCH_CHARS],
        classification=classification,
        disposition=disposition,
        remote=values.get("remote", [""])[0] == "1",
        internship=values.get("internship", [""])[0] == "1",
        refresh=values.get("refresh", [""])[0] == "1",
    )


def _latest_archive_update(result: V2exFetchResult) -> str | None:
    timestamps = tuple(
        outcome.last_seen_at
        for outcome in result.outcomes
        if outcome.last_seen_at is not None
    )
    if not timestamps:
        return None
    try:
        return datetime.fromisoformat(max(timestamps)).astimezone().strftime(
            "%m-%d %H:%M"
        )
    except ValueError:
        return None


def _respond(
    start_response: StartResponse,
    *,
    status: str,
    text: str,
    method: str,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> tuple[bytes, ...]:
    return _respond_bytes(
        start_response,
        status=status,
        body=text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
        method=method,
        extra_headers=extra_headers,
    )


def _respond_bytes(
    start_response: StartResponse,
    *,
    status: str,
    body: bytes,
    content_type: str,
    method: str,
    extra_headers: tuple[tuple[str, str], ...] = (),
) -> tuple[bytes, ...]:
    start_response(
        status,
        [
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            *_SECURITY_HEADERS,
            *extra_headers,
        ],
    )
    return () if method == "HEAD" else (body,)


def _matches(outcome: V2exItemOutcome, filters: _ViewerFilters) -> bool:
    if (
        filters.classification
        and outcome.classification != filters.classification
    ):
        return False
    if filters.disposition and outcome.disposition != filters.disposition:
        return False
    if filters.remote and not outcome.signals.remote:
        return False
    if filters.internship and not outcome.signals.internship:
        return False
    if filters.query and filters.query.casefold() not in outcome.safe_text.casefold():
        return False
    return True


def _render_page(
    result: V2exFetchResult,
    *,
    outcomes: tuple[V2exItemOutcome, ...],
    filters: _ViewerFilters,
    refresh_error: str | None,
    updated_at: str | None,
) -> str:
    cards = (
        "".join(_render_outcome(outcome) for outcome in outcomes)
        if outcomes
        else (
            '<div class="empty"><strong>没有符合条件的帖子</strong>'
            "<p>试试清除部分筛选条件。</p></div>"
        )
    )
    form = _render_filters(filters)
    normalized_count = sum(
        outcome.disposition == "normalized"
        for outcome in result.outcomes
    )
    review_count = sum(
        outcome.disposition != "normalized"
        for outcome in result.outcomes
    )
    status_label = {
        "complete": "完整快照",
        "partial": "部分结果",
        "failed": "读取失败",
    }.get(result.status, "未知状态")
    warning = ""
    if refresh_error is not None:
        warning_text = (
            "刷新失败，正在显示本机归档历史"
            if result.stale
            else "刷新失败，正在显示上次快照"
        )
        warning = (
            '<p class="notice" role="status">'
            f"{warning_text}</p>"
        )
    failure = (
        '<section class="notice failure" role="alert">'
        "<h2>暂时无法读取 V2EX Feed</h2>"
        "<p>请稍后再试；当前没有可显示的缓存快照。</p></section>"
        if result.error_code is not None and not result.stale
        else ""
    )
    return (
        "<!doctype html><html lang=\"zh-CN\"><head>"
        "<meta charset=\"utf-8\"><meta name=\"viewport\" "
        "content=\"width=device-width,initial-scale=1\">"
        '<meta name="color-scheme" content="light">'
        "<title>V2EX 酷工作观察台</title>"
        f"<style>{_STYLES}</style></head><body>"
        '<main class="shell">'
        '<header class="hero">'
        '<div class="eyebrow"><span class="feed-dot"></span>'
        "V2EX 官方 JSON Feed · 本机只读 · 缓存更新："
        f"{escape(updated_at or '尚未成功')}</div>"
        '<div class="hero-row"><div>'
        "<h1>V2EX 酷工作观察台</h1>"
        '<p class="hero-copy">把社区招聘与待审核帖子放在同一张桌面上。'
        "数据来自官方公开 Feed，联系方式已在进入页面前移除。"
        "较早记录可能已离开当前 50 条 Feed，不代表岗位仍开放。</p>"
        "</div>"
        '<form method="get" action="/">'
        '<input type="hidden" name="refresh" value="1">'
        '<button class="refresh-button" type="submit">刷新数据</button>'
        "</form></div>"
        '<section class="stats" aria-label="快照统计">'
        '<article class="stat"><span>归档帖子</span>'
        f"<strong>{len(result.outcomes)}</strong></article>"
        '<article class="stat"><span>规范化岗位</span>'
        f"<strong>{normalized_count}</strong></article>"
        '<article class="stat"><span>待审核</span>'
        f"<strong>{review_count}</strong></article>"
        '<article class="stat"><span>抓取状态</span>'
        f"<strong>{escape(status_label)}</strong></article>"
        "</section>"
        f"{warning}{failure}</header>"
        f'<section class="filter-panel">{form}</section>'
        '<section class="results-head"><div>'
        "<h2>帖子列表</h2>"
        "<p>高置信招聘可直接阅读，其余结果保留审核原因。</p>"
        "</div>"
        f'<span class="result-count">显示 {len(outcomes)} 条'
        f" / 共 {len(result.outcomes)} 条</span></section>"
        f'<section class="job-grid">{cards}</section>'
        '<footer class="footer">仅监听 127.0.0.1 · '
        "本机归档 · 不自动投递</footer>"
        "</main></body></html>"
    )


def _render_filters(filters: _ViewerFilters) -> str:
    options = ['<option value="">全部分类</option>']
    options.extend(
        (
            f'<option value="{value}"'
            f'{" selected" if filters.classification == value else ""}>'
            f"{label}</option>"
        )
        for value, label in _CLASSIFICATION_LABELS.items()
    )
    disposition_options = [
        '<option value="">全部结果</option>',
        (
            '<option value="normalized"'
            f'{" selected" if filters.disposition == "normalized" else ""}>'
            "已规范化</option>"
        ),
        (
            '<option value="review"'
            f'{" selected" if filters.disposition == "review" else ""}>'
            "待审核</option>"
        ),
        (
            '<option value="invalid"'
            f'{" selected" if filters.disposition == "invalid" else ""}>'
            "无效条目</option>"
        ),
    ]
    return (
        '<form class="filters" method="get" action="/" '
        'role="search" aria-label="岗位筛选">'
        '<label class="sr-only" for="viewer-q">搜索岗位</label>'
        f'<input class="control search-control" id="viewer-q" name="q" '
        f'value="{escape(filters.query, quote=True)}" '
        'maxlength="100" placeholder="搜索岗位、团队或正文关键词">'
        '<label class="sr-only" for="viewer-classification">分类</label>'
        '<select class="control" id="viewer-classification" '
        f'name="classification">{"".join(options)}</select>'
        '<label class="sr-only" for="viewer-disposition">结果类型</label>'
        '<select class="control" id="viewer-disposition" '
        'name="disposition">'
        f'{"".join(disposition_options)}</select>'
        '<label class="check"><input type="checkbox" '
        'name="remote" value="1"'
        f'{" checked" if filters.remote else ""}>远程</label>'
        '<label class="check"><input type="checkbox" '
        'name="internship" value="1"'
        f'{" checked" if filters.internship else ""}>实习</label>'
        '<button class="filter-button" type="submit">应用筛选</button>'
        '<a class="reset-link" href="/">清除</a>'
        "</form>"
    )


def _render_outcome(outcome: V2exItemOutcome) -> str:
    title = (
        outcome.job.title
        if outcome.job is not None
        else outcome.safe_text.splitlines()[0]
        if outcome.safe_text
        else "未命名帖子"
    )
    disposition = {
        "normalized": "已规范化",
        "review": "待审核",
        "invalid": "无效条目",
    }.get(outcome.disposition, "待审核")
    disposition_class = {
        "normalized": "badge-normalized",
        "review": "badge-review",
        "invalid": "badge-invalid",
    }.get(outcome.disposition, "badge-review")
    classification = _CLASSIFICATION_LABELS.get(
        outcome.classification,
        "未分类",
    )
    company = (
        outcome.job.company
        if outcome.job is not None and outcome.job.company
        else None
    )
    details: list[str] = []
    if outcome.signals.locations:
        details.append(" / ".join(outcome.signals.locations))
    if outcome.signals.salary_text:
        details.append(outcome.signals.salary_text)
    signal_labels = []
    if outcome.signals.remote:
        signal_labels.append("远程")
    if outcome.signals.internship:
        signal_labels.append("实习")
    if outcome.signals.part_time:
        signal_labels.append("兼职")
    if outcome.signals.contract:
        signal_labels.append("合同")
    if outcome.signals.foreign_company_or_team:
        signal_labels.append("国外团队")
    safe_lines = [
        line.strip()
        for line in outcome.safe_text.splitlines()
        if line.strip()
    ]
    summary_lines = (
        safe_lines[1:]
        if safe_lines and safe_lines[0] == title.strip()
        else safe_lines
    )
    summary = " · ".join(summary_lines).strip()[:520]
    detail_html = "".join(
        f'<span class="meta">{escape(detail)}</span>'
        for detail in details
    )
    signal_html = "".join(
        f'<span class="tag">{escape(label)}</span>'
        for label in signal_labels
    )
    risk_html = "".join(
        '<span class="tag tag-risk">'
        f'{escape(_RISK_LABELS.get(tag, tag))}</span>'
        for tag in outcome.risk_tags
    )
    reason_labels = [
        _REASON_LABELS.get(
            code,
            "日期字段无效"
            if code.startswith("invalid_date_")
            else "需要人工确认",
        )
        for code in outcome.reason_codes
    ]
    reason = (
        f'<p class="review-note">{escape("；".join(reason_labels))}</p>'
        if reason_labels
        else ""
    )
    link = (
        '<a class="source-link" '
        f'href="{escape(outcome.source_url, quote=True)}" '
        'target="_blank" rel="noopener noreferrer">'
        "查看原帖 ↗</a>"
        if outcome.source_url is not None
        else ""
    )
    company_html = (
        f'<p class="company">{escape(company)}</p>'
        if company is not None
        else ""
    )
    return (
        f'<article class="job-card {escape(outcome.disposition)}">'
        '<div class="card-top">'
        f'<span class="badge {disposition_class}">{disposition}</span>'
        f'<span class="badge classification">{classification}</span>'
        "</div>"
        f"<h3>{escape(title)}</h3>"
        f"{company_html}"
    ) + (
        f'<div class="meta-row">{detail_html}</div>'
        if detail_html
        else ""
    ) + (
        f'<p class="summary">{escape(summary)}</p>' if summary else ""
    ) + (
        f'<div class="tag-row">{signal_html}{risk_html}</div>'
        f"{reason}"
        '<div class="card-footer">'
        f"{link}"
        f'<span class="confidence">置信度：{escape(outcome.confidence)}</span>'
        "</div>"
        "</article>"
    )


def _valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the local-only V2EX jobs viewer.",
    )
    parser.add_argument(
        "--port",
        type=_valid_port,
        default=8765,
        help="loopback port (default: 8765)",
    )
    arguments = parser.parse_args(argv)
    app = V2exViewerApp()
    with make_server(
        "127.0.0.1",
        arguments.port,
        app,
        handler_class=_QuietRequestHandler,
    ) as server:
        print(
            f"V2EX viewer: http://127.0.0.1:{arguments.port}/",
            flush=True,
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
