import json
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from email.message import Message
from urllib.error import HTTPError
from unittest.mock import patch

from job_sources.contracts import CollectionResult, JobQuery
from job_sources.evaluation import (
    build_job_evaluation_text,
    evaluate_normalized_job,
)
from job_sources.nowcoder import (
    MAX_RESPONSE_BYTES,
    InvalidSourceRequest,
    JobReference,
    JobSearchResult,
    NormalizedJob,
    NowcoderPublicSource,
    UpstreamChangedError,
    UpstreamHttpError,
    SourceAccessRestrictedError,
    VerificationRequiredError,
    NowcoderSourceAdapter,
    _load_public_html,
)
from job_sources.nowcoder.models import BrowserSearchResult
from job_sources.nowcoder.browser import NowcoderPlaywrightSearchCollector
from job_sources.nowcoder.checkpoints import NowcoderCheckpointStore
from job_sources.nowcoder.research import NowcoderResearchSink
from job_sources.nowcoder.tasks import NowcoderTaskManager, TaskSnapshot
from resume_agent.repository import Repository


LIST_HTML = """
<html>
  <body>
    <a href="https://www.nowcoder.com/jobs/detail/101?from=list">
      <span class="job-name">AI 应用开发实习生</span>
      <img src="https://static.nowcoder.com/hot.png">
    </a>
    <a href="/jobs/detail/202">
      <span class="job-name">后端开发实习生</span>
    </a>
    <a href="/jobs/detail/101?duplicate=1">
      <span class="job-name">重复岗位</span>
    </a>
    <a href="/discuss/123">不是岗位</a>
  </body>
</html>
"""

CAMPUS_LIST_HTML = (
    "<html><script>window.__INITIAL_STATE__="
    + json.dumps(
        {
            "store": {},
            "app": {
                "132": {
                    "jobListData": [
                        {
                            "data": {
                                "id": 301,
                                "jobName": "校招后端工程师",
                            }
                        },
                        {
                            "data": {
                                "id": 302,
                                "jobName": "校招测试工程师",
                            }
                        },
                    ]
                }
            },
        },
        ensure_ascii=False,
    )
    + ";(function(){})();</script></html>"
)


def detail_html(detail: dict, company: dict | None = None) -> str:
    state = {
        "store": {
            "jobDetail": {
                "detail": detail,
                "jobCompany": company,
                "jobPublisher": {
                    "name": "不应进入结果",
                    "onlineStatus": "online",
                },
            }
        }
    }
    return (
        "<html><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + ";(function(){})();</script></html>"
    )


DETAIL_HTML = detail_html(
    {
        "id": 101,
        "jobId": "uuid-101",
        "recruitType": 2,
        "jobName": "AI 应用开发实习生",
        "ext": json.dumps(
            {
                "infos": "<p>负责 AI Agent 产品研发</p>",
                "requirements": "<p>熟悉 Python</p><p>每周到岗四天</p>",
                "publisherInfo": {
                    "name": "未知上游联系人",
                    "mobile": "13800000000",
                },
            },
            ensure_ascii=False,
        ),
        "jobCity": "北京",
        "jobCityList": ["北京", "上海"],
        "deliverBegin": 1784217600000,
        "deliverEnd": 1793462399000,
        "graduationYear": "2027届",
        "durationDays": 4,
        "durationMonths": 3,
        "jobOffer": 1,
        "eduLevel": "本科",
        "salaryShow": "300-500元/天",
        "redirectExternalUrl": "https://jobs.example.com/apply/101",
        "bossUid": 999,
        "user": {"name": "招聘者"},
        "apiSimpleBossUser": {"name": "招聘者"},
        "conversationId": "private",
        "greeting": "你好",
        "recruiterMobile": "13800000000",
        "publisherInfo": {"name": "未知上游联系人字段"},
    },
    {
        "companyName": "示例科技",
        "picUrl": "https://example.com/logo.png",
        "scale": "100-499人",
    },
)

ATS_DETAIL_HTML = detail_html(
    {
        "id": "d8576c07-b865-4ad2-a383-f07f32508cf4",
        "jobId": "d8576c07-b865-4ad2-a383-f07f32508cf4",
        "globalJobId": 30300000145061,
        "jobName": "后端开发实习生",
        "companyNameText": "示例外企",
        "jobCity": "上海",
        "commitment": "实习",
        "description": "<p>负责 Python 服务端开发</p><p>熟悉 Docker</p>",
        "salary": "200-300元/天",
        "platform": "moka",
        "jobDetailUrl": "https://example.mokahr.com/jobs/abc",
    }
)


class NowcoderPublicSourceTests(unittest.TestCase):
    def test_normalizes_nowcoder_ats_aggregate_by_global_job_id(self):
        source = NowcoderPublicSource(load_text=lambda _: ATS_DETAIL_HTML)

        job = source.get_job("30300000145061")

        self.assertEqual(job.source_job_id, "30300000145061")
        self.assertEqual(job.title, "后端开发实习生")
        self.assertEqual(job.company, "示例外企")
        self.assertEqual(job.locations, ("上海",))
        self.assertEqual(job.employment_type, "intern")
        self.assertEqual(job.description, "负责 Python 服务端开发\n熟悉 Docker")
        self.assertEqual(job.salary_text, "200-300元/天")
        self.assertEqual(job.upstream_platform, "moka")
        self.assertEqual(job.detail_status, "partial")
        self.assertEqual(
            job.official_apply_url,
            "https://example.mokahr.com/jobs/abc",
        )

    def test_lists_job_references_for_each_supported_kind(self):
        requested_urls: list[str] = []

        def load_text(url: str) -> str:
            requested_urls.append(url)
            return CAMPUS_LIST_HTML if "/jobs/school/jobs" in url else LIST_HTML

        source = NowcoderPublicSource(load_text=load_text)

        for kind, expected_path in (
            ("campus", "/jobs/school/jobs"),
            ("internship", "/jobs/intern/center"),
            ("social", "/jobs/fulltime/center"),
        ):
            with self.subTest(kind=kind):
                jobs = source.list_jobs(kind=kind, limit=20)
                expected_ids = ["301", "302"] if kind == "campus" else ["101", "202"]
                self.assertEqual(
                    [job.source_job_id for job in jobs],
                    expected_ids,
                )
                expected_title = (
                    "校招后端工程师"
                    if kind == "campus"
                    else "AI 应用开发实习生"
                )
                self.assertEqual(jobs[0].title, expected_title)
                self.assertEqual(jobs[0].source, "nowcoder")
                self.assertEqual(jobs[0].source_section, kind)
                self.assertFalse(jobs[0].type_verified)
                self.assertIn(expected_path, requested_urls[-1])

    def test_list_limit_is_bounded_and_validated(self):
        source = NowcoderPublicSource(load_text=lambda _: LIST_HTML)
        self.assertEqual(len(source.list_jobs("internship", limit=1)), 1)
        with self.assertRaises(InvalidSourceRequest):
            source.list_jobs("unknown", limit=1)
        with self.assertRaises(InvalidSourceRequest):
            source.list_jobs("internship", limit=0)
        with self.assertRaises(InvalidSourceRequest):
            source.list_jobs("internship", limit=21)

    def test_reuses_a_short_lived_page_cache(self):
        calls = 0

        def load_text(_: str) -> str:
            nonlocal calls
            calls += 1
            return LIST_HTML

        source = NowcoderPublicSource(load_text=load_text)
        source.list_jobs("internship", limit=1)
        source.list_jobs("internship", limit=2)

        self.assertEqual(calls, 1)

    def test_searches_all_sections_and_verifies_employment_type(self):
        detail_pages = {
            "101": DETAIL_HTML,
            "202": detail_html(
                {
                    "id": 202,
                    "recruitType": 3,
                    "jobName": "后端开发工程师",
                    "ext": None,
                }
            ),
        }

        def load_text(url: str) -> str:
            if "/jobs/detail/" in url:
                return detail_pages[url.rsplit("/", 1)[-1]]
            return LIST_HTML

        source = NowcoderPublicSource(load_text=load_text)

        internships = source.search_jobs(
            keyword="AI 应用开发",
            employment_type="intern",
            limit=10,
        )
        full_time = source.search_jobs(
            keyword="后端开发",
            employment_type="full_time",
            limit=10,
        )

        self.assertEqual(
            [job.source_job_id for job in internships.jobs],
            ["101"],
        )
        self.assertEqual(
            [job.source_job_id for job in full_time.jobs],
            ["202"],
        )
        self.assertEqual(internships.scanned_references, 2)
        self.assertEqual(internships.checked_details, 1)
        self.assertEqual(internships.failed_details, 0)
        self.assertFalse(internships.detail_check_limit_reached)

    def test_normalizes_public_detail_and_removes_personal_fields(self):
        source = NowcoderPublicSource(load_text=lambda _: DETAIL_HTML)

        job = source.get_job("101")

        self.assertEqual(job.source, "nowcoder")
        self.assertEqual(job.source_job_id, "101")
        self.assertEqual(job.title, "AI 应用开发实习生")
        self.assertEqual(job.company, "示例科技")
        self.assertEqual(job.locations, ("北京", "上海"))
        self.assertEqual(job.recruitment_channel, "campus")
        self.assertEqual(job.employment_type, "intern")
        self.assertEqual(job.salary_text, "300-500元/天")
        self.assertEqual(job.responsibilities, ("负责 AI Agent 产品研发",))
        self.assertEqual(
            job.requirements,
            ("熟悉 Python", "每周到岗四天"),
        )
        self.assertEqual(job.campus, {"graduation_years": [2027]})
        self.assertEqual(
            job.internship,
            {
                "days_per_week": 4,
                "duration_months": 3,
                "conversion_opportunity": True,
            },
        )
        self.assertEqual(job.official_apply_url, "https://jobs.example.com/apply/101")
        sanitized_detail = job.raw_data["detail"]
        self.assertNotIn("bossUid", sanitized_detail)
        self.assertNotIn("user", sanitized_detail)
        self.assertNotIn("apiSimpleBossUser", sanitized_detail)
        self.assertNotIn("conversationId", sanitized_detail)
        self.assertNotIn("greeting", sanitized_detail)
        self.assertNotIn("recruiterMobile", sanitized_detail)
        self.assertNotIn("publisherInfo", sanitized_detail)
        self.assertEqual(sanitized_detail["jobName"], "AI 应用开发实习生")
        self.assertNotIn("publisherInfo", sanitized_detail["ext"])
        self.assertEqual(
            set(sanitized_detail["ext"]),
            {"infos", "requirements"},
        )

    def test_missing_optional_detail_fields_do_not_crash(self):
        html = detail_html(
            {
                "id": 303,
                "recruitType": 3,
                "jobName": "Python 开发工程师",
                "ext": None,
            }
        )
        source = NowcoderPublicSource(load_text=lambda _: html)

        job = source.get_job("303")

        self.assertEqual(job.company, None)
        self.assertEqual(job.locations, ())
        self.assertEqual(job.recruitment_channel, "social")
        self.assertEqual(job.employment_type, "full_time")
        self.assertEqual(job.description, None)
        self.assertEqual(job.responsibilities, ())
        self.assertEqual(job.requirements, ())
        self.assertEqual(job.campus, None)
        self.assertEqual(job.internship, None)

    def test_rejects_changed_or_verification_pages(self):
        source = NowcoderPublicSource(load_text=lambda _: "<html>页面结构已变化</html>")
        with self.assertRaises(UpstreamChangedError):
            source.list_jobs("campus")
        with self.assertRaises(UpstreamChangedError):
            source.get_job("101")

        verification = NowcoderPublicSource(
            load_text=lambda _: "<html><title>安全验证</title>验证码</html>"
        )
        with self.assertRaises(VerificationRequiredError):
            verification.list_jobs("campus")

    def test_rejects_invalid_job_identifier_before_request(self):
        calls = 0

        def load_text(_: str) -> str:
            nonlocal calls
            calls += 1
            return DETAIL_HTML

        source = NowcoderPublicSource(load_text=load_text)
        with self.assertRaises(InvalidSourceRequest):
            source.get_job("../login")
        self.assertEqual(calls, 0)

    def test_health_check_reports_count_without_throwing(self):
        healthy = NowcoderPublicSource(load_text=lambda _: LIST_HTML)
        healthy_status = healthy.health_check()
        self.assertEqual(healthy_status["source"], "nowcoder")
        self.assertTrue(healthy_status["success"])
        self.assertEqual(healthy_status["fetched_count"], 2)
        self.assertIsNone(healthy_status["error_message"])
        self.assertIn("checked_at", healthy_status)

        failing = NowcoderPublicSource(
            load_text=lambda _: "<html><title>安全验证</title></html>"
        )
        status = failing.health_check()
        self.assertFalse(status["success"])
        self.assertEqual(status["fetched_count"], 0)
        self.assertIn("verification", status["error_message"])
        self.assertIn("checked_at", status)

    def test_http_loader_rejects_errors_external_redirects_and_large_pages(self):
        class FakeResponse:
            def __init__(self, body: bytes, final_url: str):
                self.body = body
                self.final_url = final_url
                self.headers = Message()
                self.headers["Content-Type"] = "text/html; charset=utf-8"

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return self.final_url

            def read(self, amount: int):
                return self.body[:amount]

        with patch(
            "job_sources.nowcoder.source.urlopen",
            side_effect=HTTPError(
                "https://www.nowcoder.com/jobs/intern/center",
                429,
                "Too Many Requests",
                Message(),
                None,
            ),
        ):
            with self.assertRaisesRegex(UpstreamHttpError, "HTTP 429"):
                _load_public_html(
                    "https://www.nowcoder.com/jobs/intern/center"
                )

        with patch(
            "job_sources.nowcoder.source.urlopen",
            return_value=FakeResponse(b"redirected", "https://example.com/jobs"),
        ):
            with self.assertRaisesRegex(UpstreamHttpError, "redirected outside"):
                _load_public_html(
                    "https://www.nowcoder.com/jobs/intern/center"
                )

        with patch(
            "job_sources.nowcoder.source.urlopen",
            return_value=FakeResponse(
                b"x" * (MAX_RESPONSE_BYTES + 1),
                "https://www.nowcoder.com/jobs/intern/center",
            ),
        ):
            with self.assertRaisesRegex(UpstreamHttpError, "size limit"):
                _load_public_html(
                    "https://www.nowcoder.com/jobs/intern/center"
                )

    def test_fastapi_exposes_read_only_nowcoder_routes(self):
        from fastapi.testclient import TestClient

        import main

        class FakeSource:
            source = "nowcoder"

            @staticmethod
            def health_check():
                return {
                    "source": "nowcoder",
                    "success": True,
                    "fetched_count": 1,
                    "error_message": None,
                    "checked_at": "2026-07-30T00:00:00+00:00",
                }

            @staticmethod
            def list_jobs(kind: str, limit: int):
                self.assertEqual(kind, "internship")
                self.assertEqual(limit, 1)
                return [
                    JobReference(
                        source="nowcoder",
                        source_job_id="101",
                        source_url="https://www.nowcoder.com/jobs/detail/101",
                        title="AI 应用开发实习生",
                        source_section="internship",
                        type_verified=False,
                    )
                ]

            @staticmethod
            def search_jobs(keyword: str, employment_type: str, limit: int):
                self.assertEqual(keyword, "后端")
                self.assertEqual(employment_type, "intern")
                self.assertEqual(limit, 5)
                return JobSearchResult(
                    jobs=(),
                    scanned_references=20,
                    checked_details=2,
                    failed_details=0,
                    section_errors={},
                    detail_check_limit_reached=False,
                )

            @staticmethod
            def get_job(source_job_id: str):
                self.assertEqual(source_job_id, "101")
                return NormalizedJob(
                    source="nowcoder",
                    source_job_id="101",
                    source_url="https://www.nowcoder.com/jobs/detail/101",
                    official_apply_url=None,
                    title="AI 应用开发实习生",
                    company="示例科技",
                    locations=("北京",),
                    recruitment_channel="campus",
                    employment_type="intern",
                    work_mode=None,
                    description=None,
                    responsibilities=(),
                    requirements=(),
                    salary_text=None,
                    published_at=None,
                    expires_at=None,
                    last_verified_at="2026-07-30T00:00:00+00:00",
                    campus=None,
                    internship=None,
                    raw_data={},
                )

        with patch("job_sources.nowcoder.router._source", FakeSource()):
            client = TestClient(main.app)
            health = client.get("/job-sources/nowcoder/health")
            jobs = client.get(
                "/job-sources/nowcoder/jobs",
                params={"kind": "internship", "limit": 1},
            )
            search = client.get(
                "/job-sources/nowcoder/search",
                params={
                    "keyword": "后端",
                    "employment_type": "intern",
                    "limit": 5,
                },
            )
            detail = client.get("/job-sources/nowcoder/jobs/101")
            ui = client.get("/job-sources/nowcoder/ui")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(jobs.status_code, 200)
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()["scope"], "first_page_of_each_public_section")
        self.assertEqual(search.json()["scanned_references"], 20)
        self.assertEqual(jobs.json()["source_section"], "internship")
        self.assertEqual(jobs.json()["type_verification"], "detail_required")
        self.assertFalse(jobs.json()["jobs"][0]["type_verified"])
        self.assertEqual(jobs.json()["jobs"][0]["source_job_id"], "101")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["company"], "示例科技")
        self.assertEqual(ui.status_code, 200)
        self.assertIn("牛客 JD 调研", ui.text)
        invalid = client.get("/job-sources/nowcoder/jobs/not-a-number")
        self.assertEqual(invalid.status_code, 400)


class NowcoderTaskManagerTests(unittest.TestCase):
    def test_infinite_scroll_resume_compares_current_dom_growth(self):
        class MissingControl:
            @staticmethod
            def count():
                return 0

        class Links:
            def __init__(self, page):
                self.page = page

            def count(self):
                return 40 if self.page.scrolled else 20

        class FakePage:
            scrolled = False

            def get_by_text(self, *args, **kwargs):
                return MissingControl()

            def locator(self, selector):
                if selector == ".el-pagination .btn-next":
                    return MissingControl()
                return Links(self)

            def evaluate(self, expression):
                self.scrolled = True

            def wait_for_function(
                self,
                expression,
                *,
                arg,
                timeout,
            ):
                if arg["beforeCount"] != 20 or timeout != 10_000:
                    raise AssertionError((arg, timeout))
                self.scrolled = True

        class Collector(NowcoderPlaywrightSearchCollector):
            @staticmethod
            def _raise_for_blocked_page(page):
                return None

        historical_ids = {str(value) for value in range(100)}
        self.assertTrue(
            Collector()._advance_results(
                FakePage(),
                historical_ids,
            )
        )

    def test_unverified_infinite_scroll_growth_pauses_instead_of_completes(self):
        class MissingControl:
            @staticmethod
            def count():
                return 0

        class Links:
            @staticmethod
            def count():
                return 20

        class FakePage:
            def get_by_text(self, *args, **kwargs):
                return MissingControl()

            def locator(self, selector):
                if selector == ".el-pagination .btn-next":
                    return MissingControl()
                return Links()

            @staticmethod
            def evaluate(expression):
                return None

            @staticmethod
            def wait_for_function(*args, **kwargs):
                raise TimeoutError("no verified growth")

        class Collector(NowcoderPlaywrightSearchCollector):
            @staticmethod
            def _raise_for_blocked_page(page):
                return None

        with self.assertRaisesRegex(
            Exception,
            "growth could not be verified",
        ):
            Collector()._advance_results(FakePage(), {"101"})

    def test_browser_rejects_stale_route_filters_before_new_query(self):
        class Keyword:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def input_value():
                return ""

        class FakePage:
            url = (
                "https://www.nowcoder.com/jobs/intern/center"
                "?city=%E4%B8%8A%E6%B5%B7&search=old"
            )

            @staticmethod
            def get_by_placeholder(*args, **kwargs):
                return Keyword()

        with self.assertRaisesRegex(
            Exception,
            "stale search filters",
        ):
            NowcoderPlaywrightSearchCollector._verify_reset_state(
                FakePage()
            )

    def test_browser_accepts_clean_route_before_new_query(self):
        class Keyword:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def input_value():
                return ""

        class FakePage:
            url = (
                "https://www.nowcoder.com/jobs/intern/center"
                "?recruitType=2&city=%E4%B8%8A%E6%B5%B7"
            )

            @staticmethod
            def get_by_placeholder(*args, **kwargs):
                return Keyword()

        NowcoderPlaywrightSearchCollector._verify_reset_state(FakePage())

    def test_research_resume_advances_the_next_hundred_page_batch(self):
        self.assertEqual(
            NowcoderPlaywrightSearchCollector._batch_page_limit(100, 100),
            200,
        )

    def test_research_retries_failed_detail_from_saved_progress(self):
        references = (
            JobReference(
                source="nowcoder",
                source_job_id="101",
                source_url="https://www.nowcoder.com/jobs/detail/101",
                title="后端开发实习生",
                source_section="internship",
                type_verified=False,
            ),
            JobReference(
                source="nowcoder",
                source_job_id="202",
                source_url="https://www.nowcoder.com/jobs/detail/202",
                title="Java后端实习生",
                source_section="internship",
                type_verified=False,
            ),
        )
        browser_result = BrowserSearchResult(
            references=references,
            pages_read=2,
            discovered_count=2,
            complete=True,
            stop_reason="result_end",
        )

        class ResumableCollector:
            @staticmethod
            def collect_resumable(
                query,
                *,
                target,
                max_pages,
                should_stop,
                resume_result,
                on_progress,
            ):
                if resume_result is None:
                    on_progress(
                        BrowserSearchResult(
                            references=references,
                            pages_read=1,
                            discovered_count=2,
                            complete=False,
                            stop_reason="page_checkpoint",
                        )
                    )
                else:
                    self.assertTrue(resume_result.complete)
                return browser_result

        base_job = NowcoderPublicSource(
            load_text=lambda _: DETAIL_HTML
        ).get_job("101")

        class FirstDetailSource:
            @staticmethod
            def get_job(source_job_id):
                if source_job_id == "202":
                    raise UpstreamHttpError("temporary")
                return base_job

        progress = []
        first = NowcoderSourceAdapter(
            search_collector=ResumableCollector(),
            detail_source=FirstDetailSource(),
            evaluate_job=lambda job: job,
        ).collect_resumable(
            JobQuery(
                keyword="后端开发",
                recruitment_channel="internship",
                city="上海",
            ),
            mode="research",
            should_stop=lambda: False,
            resume=None,
            on_progress=progress.append,
        )

        self.assertFalse(first.complete)
        self.assertEqual(first.stop_reason, "detail_failures")
        self.assertEqual(progress[-1].failed_detail_ids, ("202",))
        self.assertEqual(progress[-1].pages_read, 2)

        requested = []

        class RecoveredDetailSource:
            @staticmethod
            def get_job(source_job_id):
                requested.append(source_job_id)
                return replace(
                    base_job,
                    source_job_id=source_job_id,
                    source_url=(
                        f"https://www.nowcoder.com/jobs/detail/{source_job_id}"
                    ),
                )

        resumed_progress = []
        resumed = NowcoderSourceAdapter(
            search_collector=ResumableCollector(),
            detail_source=RecoveredDetailSource(),
            evaluate_job=lambda job: job,
        ).collect_resumable(
            first.query,
            mode="research",
            should_stop=lambda: False,
            resume=progress[-1],
            on_progress=resumed_progress.append,
        )

        self.assertTrue(resumed.complete)
        self.assertEqual(requested, ["202"])
        self.assertEqual(resumed.detail_failure_count, 0)

    def test_access_restriction_does_not_fall_back_to_browser_detail(self):
        fallback_calls = []

        class Collector:
            @staticmethod
            def collect(query, *, target, max_pages, should_stop):
                return BrowserSearchResult(
                    references=(
                        JobReference(
                            source="nowcoder",
                            source_job_id="101",
                            source_url=(
                                "https://www.nowcoder.com/jobs/detail/101"
                            ),
                            title="后端开发实习生",
                            source_section="internship",
                            type_verified=False,
                        ),
                    ),
                    pages_read=1,
                    discovered_count=1,
                    complete=True,
                    stop_reason="result_end",
                )

            @staticmethod
            def load_detail_html(source_job_id):
                fallback_calls.append(source_job_id)
                return DETAIL_HTML

        class RestrictedDetailSource(NowcoderPublicSource):
            def get_job(self, source_job_id):
                raise SourceAccessRestrictedError("HTTP 429")

        adapter = NowcoderSourceAdapter(
            search_collector=Collector(),
            detail_source=RestrictedDetailSource(
                load_text=lambda _: ""
            ),
        )
        with self.assertRaises(SourceAccessRestrictedError):
            adapter.collect(
                JobQuery(
                    keyword="后端开发",
                    recruitment_channel="internship",
                    city="上海",
                ),
                mode="quick",
                should_stop=lambda: False,
            )
        self.assertEqual(fallback_calls, [])

    def test_browser_reports_visible_login_dialog_before_pagination(self):
        class LoginDialog:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def is_visible():
                return True

        class Body:
            @staticmethod
            def inner_text(timeout):
                return "岗位列表"

        class FakePage:
            @staticmethod
            def title():
                return "牛客招聘"

            @staticmethod
            def locator(selector):
                if selector == ".login-dialog":
                    return LoginDialog()
                if selector == "body":
                    return Body()
                raise AssertionError(selector)

        from job_sources.nowcoder.browser import BrowserLoginRequiredError

        with self.assertRaises(BrowserLoginRequiredError):
            NowcoderPlaywrightSearchCollector._raise_for_blocked_page(
                FakePage()
            )

    def test_browser_uses_nowcoder_icon_next_page_button(self):
        calls: list[str] = []

        class MissingTextControl:
            @staticmethod
            def count():
                return 0

        class NextButton:
            @staticmethod
            def count():
                return 1

            @staticmethod
            def is_enabled():
                return True

            @staticmethod
            def click():
                calls.append("clicked")

        class FakePage:
            @staticmethod
            def get_by_text(*args, **kwargs):
                return MissingTextControl()

            @staticmethod
            def locator(selector):
                self.assertEqual(selector, ".el-pagination .btn-next")
                return NextButton()

        class Collector(NowcoderPlaywrightSearchCollector):
            @staticmethod
            def _raise_for_blocked_page(page):
                calls.append("checked")

            @staticmethod
            def _current_result_hrefs(page):
                return ("https://www.nowcoder.com/jobs/detail/101",)

            @staticmethod
            def _wait_for_result_change(page, previous_hrefs):
                self.assertEqual(
                    previous_hrefs,
                    ("https://www.nowcoder.com/jobs/detail/101",),
                )
                calls.append("refreshed")

        advanced = Collector()._advance_results(FakePage(), {"101"})

        self.assertTrue(advanced)
        self.assertEqual(
            calls,
            ["checked", "clicked", "refreshed", "checked"],
        )

    def test_research_mode_has_no_thirty_job_target(self):
        calls: list[tuple[object, int]] = []

        class FakeSearchCollector:
            @staticmethod
            def collect(query, *, target, max_pages, should_stop):
                calls.append((target, max_pages))
                return BrowserSearchResult(
                    references=(),
                    pages_read=1,
                    discovered_count=0,
                    complete=True,
                    stop_reason="result_end",
                )

        source = NowcoderSourceAdapter(
            search_collector=FakeSearchCollector(),
            evaluate_job=lambda job: job,
        )
        source.collect(
            JobQuery(
                keyword="后端开发",
                recruitment_channel="internship",
                city="上海",
            ),
            mode="research",
            should_stop=lambda: False,
        )

        self.assertEqual(calls, [(None, 100)])

    def test_research_checkpoint_reloads_paused_and_resumes_explicitly(self):
        query = JobQuery(
            keyword="后端开发",
            recruitment_channel="internship",
            city="上海",
        )

        class FailingSource:
            @staticmethod
            def collect(query, *, mode, should_stop):
                raise RuntimeError("temporary failure")

        class SuccessfulSource:
            @staticmethod
            def collect(query, *, mode, should_stop):
                return CollectionResult(
                    source="nowcoder",
                    query=query,
                    jobs=(),
                    pages_read=2,
                    discovered_count=10,
                    deduplicated_count=10,
                    detail_success_count=0,
                    partial_detail_count=0,
                    detail_failure_count=0,
                    complete=True,
                    stop_reason="result_end",
                )

        with TemporaryDirectory() as directory:
            store = NowcoderCheckpointStore(Path(directory))
            first_manager = NowcoderTaskManager(
                FailingSource(),
                checkpoint_store=store,
            )
            try:
                created = first_manager.create_task(
                    query,
                    mode="research",
                )
                failed = first_manager.wait_for_task(
                    created.task_id,
                    timeout=2,
                )
                self.assertEqual(failed.status, "failed")
            finally:
                first_manager.close()

            checkpoint_text = (
                Path(directory) / f"{created.task_id}.json"
            ).read_text(encoding="utf-8")
            self.assertNotIn("cookie", checkpoint_text.casefold())
            self.assertNotIn("token", checkpoint_text.casefold())

            second_manager = NowcoderTaskManager(
                SuccessfulSource(),
                checkpoint_store=store,
            )
            try:
                restored = second_manager.get_task(created.task_id)
                self.assertEqual(restored.status, "paused")
                pending = second_manager.resume_task(created.task_id)
                self.assertEqual(pending.status, "pending")
                completed = second_manager.wait_for_task(
                    created.task_id,
                    timeout=2,
                )
                self.assertEqual(completed.status, "completed")
                self.assertEqual(completed.pages_read, 2)
            finally:
                second_manager.close()

    def test_research_sink_deduplicates_and_excludes_intern_salary_stats(self):
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "research.db")
            sink = NowcoderResearchSink(repository)
            query = JobQuery(
                keyword="后端开发",
                recruitment_channel="internship",
                city="上海",
            )
            candidate_id = sink.resolve_project(
                query,
                candidate_id=None,
                project_name="上海后端实习调研",
            )
            intern = NowcoderPublicSource(
                load_text=lambda _: DETAIL_HTML
            ).get_job("101")

            first_ids = sink.persist(candidate_id, query, (intern,))
            second_ids = sink.persist(candidate_id, query, (intern,))
            stored = repository.list_jobs(candidate_id)

            self.assertEqual(first_ids, second_ids)
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["source"], "nowcoder")
            self.assertEqual(stored[0]["external_job_id"], "101")
            self.assertEqual(stored[0]["salary"], "")
            self.assertIn(intern.salary_text, stored[0]["detail"])

    def test_uses_visible_detail_fallback_after_public_http_failure(self):
        class FakeSearchCollector:
            @staticmethod
            def collect(query, *, target, max_pages, should_stop):
                return BrowserSearchResult(
                    references=(
                        JobReference(
                            source="nowcoder",
                            source_job_id="101",
                            source_url=(
                                "https://www.nowcoder.com/jobs/detail/101"
                            ),
                            title="AI 应用开发实习生",
                            source_section="internship",
                            type_verified=False,
                        ),
                    ),
                    pages_read=1,
                    discovered_count=1,
                    complete=True,
                    stop_reason="result_end",
                )

            @staticmethod
            def load_detail_html(source_job_id):
                self.assertEqual(source_job_id, "101")
                return DETAIL_HTML

        class FailingHttpDetailSource(NowcoderPublicSource):
            def get_job(self, source_job_id):
                raise UpstreamHttpError("HTTP detail unavailable")

        source = NowcoderSourceAdapter(
            search_collector=FakeSearchCollector(),
            detail_source=FailingHttpDetailSource(load_text=lambda _: ""),
            evaluate_job=lambda job: job,
        )

        result = source.collect(
            JobQuery(
                keyword="后端开发",
                recruitment_channel="internship",
                city="上海",
            ),
            mode="quick",
            should_stop=lambda: False,
        )

        self.assertEqual(result.detail_failure_count, 0)
        self.assertEqual(result.jobs[0].source_job_id, "101")

    def test_projects_normalized_job_into_existing_evaluator_format(self):
        job = NowcoderPublicSource(
            load_text=lambda _: DETAIL_HTML
        ).get_job("101")

        evaluation_text = build_job_evaluation_text(job)

        self.assertIn("# 职位名称\n", evaluation_text)
        self.assertIn(job.title, evaluation_text)
        self.assertIn("# 薪资范围\n", evaluation_text)
        self.assertIn(job.salary_text, evaluation_text)
        self.assertIn("# 职位描述\n", evaluation_text)
        self.assertIn(job.description, evaluation_text)

    def test_scores_job_without_using_score_as_ingestion_filter(self):
        job = NowcoderPublicSource(
            load_text=lambda _: DETAIL_HTML
        ).get_job("101")
        seen: list[str] = []

        def fake_evaluator(text):
            seen.append(text)
            return {"score": 12}

        scored = evaluate_normalized_job(
            job,
            evaluator=fake_evaluator,
            threshold=50,
        )

        self.assertEqual(scored.score, 12)
        self.assertFalse(scored.qualified)
        self.assertEqual(len(seen), 1)
        self.assertEqual(scored.source_job_id, job.source_job_id)

    def test_browser_waits_for_initial_cards_before_submitting_search(self):
        calls: list[str] = []

        class FakePage:
            @staticmethod
            def goto(*args, **kwargs):
                calls.append("goto")

        class InspectableCollector(NowcoderPlaywrightSearchCollector):
            @staticmethod
            def _search_page():
                return FakePage()

            @staticmethod
            def _raise_for_blocked_page(page):
                calls.append("blocked-check")

            @staticmethod
            def _wait_for_results(page):
                calls.append("wait-results")

            @staticmethod
            def _verify_reset_state(page):
                calls.append("verify-reset")

            @staticmethod
            def _apply_query(page, query):
                calls.append("apply-query")
                raise RuntimeError("inspection complete")

        collector = InspectableCollector()
        with self.assertRaisesRegex(RuntimeError, "inspection complete"):
            collector.collect(
                JobQuery(
                    keyword="后端开发",
                    recruitment_channel="internship",
                    city="上海",
                ),
                target=1,
                max_pages=1,
                should_stop=lambda: False,
            )

        self.assertEqual(
            calls,
            [
                "goto",
                "blocked-check",
                "wait-results",
                "verify-reset",
                "apply-query",
            ],
        )

    def test_browser_does_not_treat_login_sms_field_as_page_verification(self):
        class MissingDialog:
            @staticmethod
            def count():
                return 0

        class FakeBody:
            @staticmethod
            def inner_text(timeout):
                self.assertEqual(timeout, 5_000)
                return "岗位列表\n登录 / 注册\n请输入验证码"

        class FakePage:
            @staticmethod
            def title():
                return "牛客招聘"

            @staticmethod
            def locator(selector):
                if selector == ".login-dialog":
                    return MissingDialog()
                self.assertEqual(selector, "body")
                return FakeBody()

        NowcoderPlaywrightSearchCollector._raise_for_blocked_page(FakePage())

    def test_browser_waits_for_filtered_results_before_collecting(self):
        calls: list[tuple[str, object]] = []

        class FakePage:
            @staticmethod
            def wait_for_url(predicate, timeout):
                calls.append(("url", timeout))
                self.assertTrue(
                    predicate(
                        "https://www.nowcoder.com/jobs/intern/center"
                        "?search=%E5%90%8E%E7%AB%AF%E5%BC%80%E5%8F%91"
                    )
                )

            @staticmethod
            def wait_for_function(expression, *, arg, timeout):
                calls.append(("results", (arg, timeout)))
                self.assertIn("previousHrefs", expression)

        NowcoderPlaywrightSearchCollector._wait_for_search_refresh(
            FakePage(),
            keyword="后端开发",
            previous_hrefs=("https://www.nowcoder.com/jobs/detail/1",),
        )

        self.assertEqual(calls[0], ("url", 20_000))
        result_argument, result_timeout = calls[1][1]
        self.assertEqual(result_argument["previousHrefs"], [
            "https://www.nowcoder.com/jobs/detail/1"
        ])
        self.assertEqual(result_timeout, 20_000)

    def test_keeps_site_search_results_without_exact_title_filter(self):
        query_seen: list[JobQuery] = []

        class FakeSearchCollector:
            @staticmethod
            def collect(query, *, target, max_pages, should_stop):
                query_seen.append(query)
                return BrowserSearchResult(
                    references=(
                        JobReference(
                            source="nowcoder",
                            source_job_id="101",
                            source_url=(
                                "https://www.nowcoder.com/jobs/detail/101"
                            ),
                            title="服务端研发工程师",
                            source_section="internship",
                            type_verified=False,
                        ),
                    ),
                    pages_read=1,
                    discovered_count=1,
                    complete=True,
                    stop_reason="result_end",
                )

        class FakeDetailSource:
            @staticmethod
            def get_job(source_job_id):
                self.assertEqual(source_job_id, "101")
                return NormalizedJob(
                    source="nowcoder",
                    source_job_id="101",
                    source_url="https://www.nowcoder.com/jobs/detail/101",
                    official_apply_url=None,
                    title="服务端研发工程师",
                    company="示例公司",
                    locations=("上海",),
                    recruitment_channel="campus",
                    employment_type="intern",
                    work_mode=None,
                    description="负责服务端系统研发",
                    responsibilities=(),
                    requirements=(),
                    salary_text=None,
                    published_at=None,
                    expires_at=None,
                    last_verified_at="2026-07-30T00:00:00+00:00",
                    campus=None,
                    internship=None,
                    raw_data={},
                )

        source = NowcoderSourceAdapter(
            search_collector=FakeSearchCollector(),
            detail_source=FakeDetailSource(),
            evaluate_job=lambda job: replace(
                job,
                score=10,
                qualified=False,
            ),
        )
        query = JobQuery(
            keyword="后端开发",
            recruitment_channel="internship",
            city="上海",
        )

        result = source.collect(
            query,
            mode="quick",
            should_stop=lambda: False,
        )

        self.assertEqual(query_seen, [query])
        self.assertEqual(
            [job.title for job in result.jobs],
            ["服务端研发工程师"],
        )
        self.assertEqual(result.detail_success_count, 1)
        self.assertEqual(result.stop_reason, "result_end")
        self.assertEqual(result.jobs[0].score, 10)
        self.assertFalse(result.jobs[0].qualified)

    def test_runs_one_browser_backed_task_at_a_time(self):
        first_started = threading.Event()
        release_first = threading.Event()
        calls: list[str] = []

        class BlockingSource:
            @staticmethod
            def collect(query, *, mode, should_stop):
                calls.append(query.keyword)
                if query.keyword == "first":
                    first_started.set()
                    release_first.wait(timeout=2)
                return CollectionResult(
                    source="nowcoder",
                    query=query,
                    jobs=(),
                    pages_read=1,
                    discovered_count=0,
                    deduplicated_count=0,
                    detail_success_count=0,
                    partial_detail_count=0,
                    detail_failure_count=0,
                    complete=True,
                    stop_reason="result_end",
                )

        manager = NowcoderTaskManager(BlockingSource())
        try:
            first = manager.create_task(
                JobQuery(
                    keyword="first",
                    recruitment_channel="internship",
                    city="上海",
                ),
                mode="quick",
            )
            second = manager.create_task(
                JobQuery(
                    keyword="second",
                    recruitment_channel="campus",
                    city="北京",
                ),
                mode="quick",
            )

            self.assertTrue(first_started.wait(timeout=1))
            self.assertEqual(manager.get_task(first.task_id).status, "running")
            self.assertEqual(manager.get_task(second.task_id).status, "pending")

            release_first.set()
            completed = manager.wait_for_task(second.task_id, timeout=2)

            self.assertEqual(completed.status, "completed")
            self.assertEqual(calls, ["first", "second"])
        finally:
            release_first.set()
            manager.close()

    def test_fastapi_creates_and_reads_nowcoder_tasks(self):
        from fastapi.testclient import TestClient

        import main

        query = JobQuery(
            keyword="后端开发",
            recruitment_channel="internship",
            city="上海",
            graduation_year="2027届",
            sort="latest",
        )
        snapshot = TaskSnapshot(
            task_id="task-1",
            query=query,
            mode="quick",
            status="pending",
            pages_read=0,
            discovered_count=0,
            deduplicated_count=0,
            detail_success_count=0,
            partial_detail_count=0,
            detail_failure_count=0,
            error_type=None,
            stop_reason=None,
        )

        class FakeTasks:
            @staticmethod
            def create_task(
                actual_query,
                *,
                mode,
                candidate_id,
                project_name,
            ):
                self.assertEqual(actual_query, query)
                self.assertEqual(mode, "quick")
                self.assertIsNone(candidate_id)
                self.assertIsNone(project_name)
                return snapshot

            @staticmethod
            def get_task(task_id):
                self.assertEqual(task_id, "task-1")
                return snapshot

            @staticmethod
            def list_tasks():
                return (snapshot,)

            @staticmethod
            def list_jobs(task_id):
                self.assertEqual(task_id, "task-1")
                return ()

            @staticmethod
            def resume_task(task_id):
                self.assertEqual(task_id, "task-1")
                return snapshot

        with patch("job_sources.nowcoder.router._tasks", FakeTasks()):
            client = TestClient(main.app)
            created = client.post(
                "/job-sources/nowcoder/tasks",
                json={
                    "keyword": "后端开发",
                    "recruitment_channel": "internship",
                    "city": "上海",
                    "graduation_year": "2027届",
                    "sort": "latest",
                    "mode": "quick",
                },
            )
            fetched = client.get("/job-sources/nowcoder/tasks/task-1")
            listed = client.get("/job-sources/nowcoder/tasks")
            jobs = client.get("/job-sources/nowcoder/tasks/task-1/jobs")
            resumed = client.post(
                "/job-sources/nowcoder/tasks/task-1/resume"
            )

        self.assertEqual(created.status_code, 202)
        self.assertEqual(created.json()["task_id"], "task-1")
        self.assertEqual(
            created.json()["query"]["recruitment_channel"],
            "internship",
        )
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(
            listed.json()["tasks"][0]["task_id"],
            "task-1",
        )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(jobs.json(), {"task_id": "task-1", "jobs": []})


if __name__ == "__main__":
    unittest.main()
