"""Visible Playwright implementation for Nowcoder's own job search UI."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from job_sources.contracts import JobQuery

from .models import NowcoderSearchResult
from .source import BASE_URL, JobReference, SourceKind

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page, Playwright


ROUTE_BY_CHANNEL: dict[str, tuple[SourceKind, str]] = {
    "internship": ("internship", "/jobs/intern/center"),
    "campus": ("campus", "/jobs/school/jobs"),
    "social": ("social", "/jobs/fulltime/center"),
}
KEYWORD_PLACEHOLDER = "请输入公司名或职位名搜索"
DETAIL_LINK_SELECTOR = "a[href*='/jobs/detail/']"
MAIN_DETAIL_LINK_SELECTOR = (
    "a[href*='/jobs/detail/'][href*='pageSource=5001'],"
    "a[href*='/jobs/detail/'][href*='channel=npJobTab']"
)
_DETAIL_PATH_PATTERN = re.compile(r"^/jobs/detail/(?P<job_id>\d+)(?:/)?$")
_DETAIL_ID_PATTERN = re.compile(r"^\d+$")
_VERIFICATION_TEXT = (
    "安全验证",
    "访问验证",
    "请完成验证",
    "拖动滑块",
)
_LOGIN_TEXT = ("登录后查看更多", "请先登录")


class NowcoderBrowserError(RuntimeError):
    """Base error for visible Nowcoder browser collection."""


class BrowserUnavailableError(NowcoderBrowserError):
    """Playwright or the configured Chrome channel cannot start."""


class BrowserPageChangedError(NowcoderBrowserError):
    """The visible search page no longer exposes the expected controls."""


class BrowserLoginRequiredError(NowcoderBrowserError):
    """The visible page requires the user to log in."""


class BrowserVerificationRequiredError(NowcoderBrowserError):
    """The visible page requires a user-completed verification step."""


class NowcoderPlaywrightSearchCollector:
    """Collects filtered public job references from one persistent browser."""

    def __init__(
        self,
        profile_dir: Path | None = None,
        *,
        chrome_channel: str = "chrome",
    ):
        repo_root = Path(__file__).resolve().parents[2]
        self._profile_dir = profile_dir or (
            repo_root / ".runtime" / "nowcoder" / "browser-profile"
        )
        self._chrome_channel = chrome_channel
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None

    def collect(
        self,
        query: JobQuery,
        *,
        target: int | None,
        max_pages: int,
        should_stop: Callable[[], bool],
    ) -> NowcoderSearchResult:
        return self._collect(
            query,
            target=target,
            max_pages=max_pages,
            should_stop=should_stop,
            resume_result=None,
            on_progress=None,
        )

    def collect_resumable(
        self,
        query: JobQuery,
        *,
        target: int | None,
        max_pages: int,
        should_stop: Callable[[], bool],
        resume_result: NowcoderSearchResult | None,
        on_progress: Callable[[NowcoderSearchResult], None],
    ) -> NowcoderSearchResult:
        return self._collect(
            query,
            target=target,
            max_pages=max_pages,
            should_stop=should_stop,
            resume_result=resume_result,
            on_progress=on_progress,
        )

    def _collect(
        self,
        query: JobQuery,
        *,
        target: int | None,
        max_pages: int,
        should_stop: Callable[[], bool],
        resume_result: NowcoderSearchResult | None,
        on_progress: Callable[[NowcoderSearchResult], None] | None,
    ) -> NowcoderSearchResult:
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        if target is not None and target < 1:
            raise ValueError("target must be positive when provided")
        source_section, route = ROUTE_BY_CHANNEL[query.recruitment_channel]
        page = self._search_page()
        page.goto(
            self._route_url(route, query),
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        self._raise_for_blocked_page(page)
        self._wait_for_results(page)
        self._verify_reset_state(page)
        self._apply_query(page, query)

        references = list(
            resume_result.references if resume_result is not None else ()
        )
        seen_ids = {item.source_job_id for item in references}
        pages_read = resume_result.pages_read if resume_result else 0
        stop_reason = "result_end"
        complete = True

        if resume_result is not None and resume_result.complete:
            return resume_result
        for _ in range(pages_read):
            if should_stop() or not self._advance_results(page, seen_ids):
                return NowcoderSearchResult(
                    references=tuple(references),
                    pages_read=pages_read,
                    discovered_count=len(seen_ids),
                    complete=False,
                    stop_reason="resume_cursor_unavailable",
                )

        page_limit = self._batch_page_limit(pages_read, max_pages)
        while pages_read < page_limit and not should_stop():
            pages_read += 1
            self._wait_for_results(page)
            new_count = self._append_references(
                page,
                source_section,
                references,
                seen_ids,
            )
            progress_result = NowcoderSearchResult(
                references=tuple(references),
                pages_read=pages_read,
                discovered_count=len(seen_ids),
                complete=False,
                stop_reason="page_checkpoint",
            )
            if on_progress is not None:
                on_progress(progress_result)
            if target is not None and len(references) >= target:
                references = references[:target]
                stop_reason = "target_reached"
                complete = False
                break
            if new_count == 0 and pages_read > 1:
                stop_reason = "no_new_jobs"
                break
            if not self._advance_results(page, seen_ids):
                stop_reason = "result_end"
                break
        else:
            if should_stop():
                stop_reason = "user_stop"
                complete = False
            elif pages_read >= page_limit:
                stop_reason = "page_limit"
                complete = False

        return NowcoderSearchResult(
            references=tuple(references),
            pages_read=pages_read,
            discovered_count=len(seen_ids),
            complete=complete,
            stop_reason=stop_reason,
        )

    @staticmethod
    def _batch_page_limit(
        completed_pages: int,
        batch_pages: int,
    ) -> int:
        return completed_pages + batch_pages

    def close(self):
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def load_detail_html(self, source_job_id: str) -> str:
        """Load one public detail in the same visible, user-controlled profile."""
        if not _DETAIL_ID_PATTERN.fullmatch(source_job_id):
            raise ValueError("Nowcoder detail identifier must contain digits")
        page = self._search_page()
        page.goto(
            f"{BASE_URL}/jobs/detail/{source_job_id}",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        self._raise_for_blocked_page(page)
        try:
            page.wait_for_function(
                """() => Array.from(document.scripts).some(
                    script => script.textContent.includes(
                        "window.__INITIAL_STATE__="
                    )
                )""",
                timeout=20_000,
            )
        except Exception as error:
            self._raise_for_blocked_page(page)
            raise BrowserPageChangedError(
                "Nowcoder detail page contained no readable public state"
            ) from error
        return page.content()

    def _search_page(self) -> Page:
        if self._context is None:
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            try:
                from playwright.sync_api import sync_playwright

                self._playwright = sync_playwright().start()
                self._context = (
                    self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self._profile_dir),
                        channel=self._chrome_channel,
                        headless=False,
                        viewport={"width": 1440, "height": 1000},
                    )
                )
            except Exception as error:
                self.close()
                raise BrowserUnavailableError(
                    "unable to start the dedicated visible Chrome profile"
                ) from error
        pages = self._context.pages
        return pages[0] if pages else self._context.new_page()

    @staticmethod
    def _route_url(route: str, query: JobQuery) -> str:
        params: dict[str, str] = {}
        if query.city:
            params["city"] = query.city
        suffix = f"?{urlencode(params)}" if params else ""
        return f"{urljoin(BASE_URL, route)}{suffix}"

    def _apply_query(self, page: Page, query: JobQuery):
        keyword = page.get_by_placeholder(KEYWORD_PLACEHOLDER, exact=True)
        try:
            keyword.wait_for(state="visible", timeout=15_000)
        except Exception as error:
            self._raise_for_blocked_page(page)
            raise BrowserPageChangedError(
                "Nowcoder job keyword field was not visible"
            ) from error
        previous_hrefs = self._current_result_hrefs(page)
        keyword.fill(query.keyword)
        keyword.press("Enter")
        self._wait_for_search_refresh(
            page,
            keyword=query.keyword,
            previous_hrefs=previous_hrefs,
        )

        if query.graduation_year:
            year_control = page.get_by_role(
                "textbox",
                name="毕业年份",
                exact=True,
            )
            if year_control.count() != 1:
                raise BrowserPageChangedError(
                    "Nowcoder graduation-year filter was not available"
                )
            year_control.click()
            year_option = page.get_by_text(
                query.graduation_year,
                exact=True,
            )
            if year_option.count() != 1:
                raise BrowserPageChangedError(
                    "requested graduation year was not available"
                )
            previous_hrefs = self._current_result_hrefs(page)
            year_option.click()
            self._wait_for_result_change(page, previous_hrefs)

        if query.sort == "latest":
            latest = page.get_by_text("最新", exact=True)
            if latest.count() != 1:
                raise BrowserPageChangedError(
                    "Nowcoder latest-sort control was not available"
                )
            previous_hrefs = self._current_result_hrefs(page)
            latest.click()
            self._wait_for_result_change(page, previous_hrefs)

    @staticmethod
    def _verify_reset_state(page: Page):
        parsed_query = parse_qs(urlsplit(page.url).query)
        stale_keys = {
            "search",
            "careerJob",
            "graduationYear",
            "sort",
            "sortType",
        }.intersection(parsed_query)
        if stale_keys:
            raise BrowserPageChangedError(
                "Nowcoder route retained stale search filters"
            )
        keyword = page.get_by_placeholder(
            KEYWORD_PLACEHOLDER,
            exact=True,
        )
        if keyword.count() != 1 or keyword.input_value().strip():
            raise BrowserPageChangedError(
                "Nowcoder keyword filter did not reset"
            )

    @staticmethod
    def _current_result_hrefs(page: Page) -> tuple[str, ...]:
        selector = (
            MAIN_DETAIL_LINK_SELECTOR
            if page.locator(MAIN_DETAIL_LINK_SELECTOR).count() > 0
            else DETAIL_LINK_SELECTOR
        )
        hrefs = page.locator(selector).evaluate_all(
            "elements => elements.slice(0, 20).map(element => element.href || '')"
        )
        return tuple(href for href in hrefs if isinstance(href, str))

    @staticmethod
    def _wait_for_search_refresh(
        page: Page,
        *,
        keyword: str,
        previous_hrefs: tuple[str, ...],
    ):
        try:
            page.wait_for_url(
                lambda url: parse_qs(urlsplit(str(url)).query).get(
                    "search"
                )
                == [keyword],
                timeout=20_000,
            )
            NowcoderPlaywrightSearchCollector._wait_for_result_change(
                page,
                previous_hrefs,
            )
        except Exception as error:
            NowcoderPlaywrightSearchCollector._raise_for_blocked_page(page)
            raise BrowserPageChangedError(
                "Nowcoder search results did not refresh after submission"
            ) from error

    @staticmethod
    def _wait_for_result_change(
        page: Page,
        previous_hrefs: tuple[str, ...],
    ):
        try:
            page.wait_for_function(
                """({detailSelector, mainSelector, previousHrefs}) => {
                    const main = Array.from(
                        document.querySelectorAll(mainSelector)
                    );
                    const elements = main.length
                        ? main
                        : Array.from(
                            document.querySelectorAll(detailSelector)
                        );
                    const currentHrefs = elements
                        .slice(0, 20)
                        .map(element => element.href || "");
                    const noJobs = document.body.innerText.includes(
                        "暂无职位"
                    );
                    return noJobs
                        || JSON.stringify(currentHrefs)
                            !== JSON.stringify(previousHrefs);
                }""",
                arg={
                    "detailSelector": DETAIL_LINK_SELECTOR,
                    "mainSelector": MAIN_DETAIL_LINK_SELECTOR,
                    "previousHrefs": list(previous_hrefs),
                },
                timeout=20_000,
            )
        except Exception as error:
            NowcoderPlaywrightSearchCollector._raise_for_blocked_page(page)
            raise BrowserPageChangedError(
                "Nowcoder filtered results did not refresh"
            ) from error

    def _wait_for_results(self, page: Page):
        try:
            page.locator(DETAIL_LINK_SELECTOR).first.wait_for(
                state="attached",
                timeout=20_000,
            )
        except Exception as error:
            self._raise_for_blocked_page(page)
            empty_text = page.get_by_text("暂无职位", exact=False)
            if empty_text.count() > 0:
                return
            raise BrowserPageChangedError(
                "Nowcoder search returned no readable job cards"
            ) from error

    @staticmethod
    def _append_references(
        page: Page,
        source_section: SourceKind,
        references: list[JobReference],
        seen_ids: set[str],
    ) -> int:
        selector = (
            MAIN_DETAIL_LINK_SELECTOR
            if page.locator(MAIN_DETAIL_LINK_SELECTOR).count() > 0
            else DETAIL_LINK_SELECTOR
        )
        records = page.locator(selector).evaluate_all(
            """elements => elements.slice(0, 500).map(element => {
                const titleNode = element.querySelector(
                    ".job-name, [class*='job-name'], [class*='job-title']"
                );
                const fallback = (element.innerText || "")
                    .split("\\n")
                    .map(value => value.trim())
                    .find(Boolean) || "";
                return {
                    href: element.href || "",
                    title: (titleNode?.textContent || fallback).trim(),
                };
            })"""
        )
        new_count = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            href = record.get("href")
            title = record.get("title")
            if not isinstance(href, str) or not isinstance(title, str):
                continue
            parsed = urlsplit(href)
            match = _DETAIL_PATH_PATTERN.fullmatch(parsed.path)
            if match is None:
                continue
            source_job_id = match.group("job_id")
            if source_job_id in seen_ids:
                continue
            seen_ids.add(source_job_id)
            new_count += 1
            references.append(
                JobReference(
                    source="nowcoder",
                    source_job_id=source_job_id,
                    source_url=(
                        f"{BASE_URL}/jobs/detail/{source_job_id}"
                    ),
                    title=title.strip(),
                    source_section=source_section,
                    type_verified=False,
                )
            )
        return new_count

    def _advance_results(self, page: Page, seen_ids: set[str]) -> bool:
        next_control = page.get_by_text("下一页", exact=True)
        if next_control.count() != 1:
            next_control = page.locator(".el-pagination .btn-next")
        if next_control.count() == 1:
            if not next_control.is_enabled():
                return False
            self._raise_for_blocked_page(page)
            previous_hrefs = self._current_result_hrefs(page)
            next_control.click()
            self._wait_for_result_change(page, previous_hrefs)
            self._raise_for_blocked_page(page)
            return True

        selector = (
            MAIN_DETAIL_LINK_SELECTOR
            if page.locator(MAIN_DETAIL_LINK_SELECTOR).count() > 0
            else DETAIL_LINK_SELECTOR
        )
        before_count = page.locator(selector).count()
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            page.wait_for_function(
                """({selector, beforeCount}) =>
                    document.querySelectorAll(selector).length > beforeCount
                """,
                arg={
                    "selector": selector,
                    "beforeCount": before_count,
                },
                timeout=10_000,
            )
        except Exception as error:
            self._raise_for_blocked_page(page)
            raise BrowserPageChangedError(
                "Nowcoder infinite-scroll growth could not be verified"
            ) from error
        self._raise_for_blocked_page(page)
        return True

    @staticmethod
    def _raise_for_blocked_page(page: Page):
        login_dialog = page.locator(".login-dialog")
        if login_dialog.count() > 0 and login_dialog.is_visible():
            raise BrowserLoginRequiredError(
                "Nowcoder requires the user to log in"
            )
        title = page.title()
        visible_text = page.locator("body").inner_text(timeout=5_000)[:5_000]
        combined = f"{title}\n{visible_text}"
        if any(marker in combined for marker in _VERIFICATION_TEXT):
            raise BrowserVerificationRequiredError(
                "Nowcoder requires user verification"
            )
        if any(marker in combined for marker in _LOGIN_TEXT):
            raise BrowserLoginRequiredError(
                "Nowcoder requires the user to log in"
            )
