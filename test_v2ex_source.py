import json
import importlib
import sqlite3
import threading
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from email.message import Message
from unittest.mock import patch

from job_sources.contracts import NormalizedJob
from job_sources.v2ex import (
    FEED_URL,
    MAX_RESPONSE_BYTES,
    V2exCursor,
    V2exJobSource,
)


def feed(*items: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "version": "https://jsonfeed.org/version/1",
            "title": "酷工作",
            "items": list(items),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def item(
    *,
    item_id: str = "https://www.v2ex.com/t/1001",
    title: str = "[招聘] Python 后端工程师",
    content_html: str = (
        "<p>示例科技招聘 Python 后端工程师</p>"
        "<p>工作地点：上海</p><p>薪资：20k-30k/月</p>"
    ),
) -> dict[str, object]:
    return {
        "id": item_id,
        "url": item_id,
        "title": title,
        "content_html": content_html,
        "date_published": "2026-07-30T09:00:00+00:00",
    }


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        final_url: str = FEED_URL,
        content_type: str = "application/json;charset=utf-8",
        content_length: str | None = None,
    ):
        self._body = body
        self.status = status
        self._final_url = final_url
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if content_length is not None:
            self.headers["Content-Length"] = content_length

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self) -> str:
        return self._final_url

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


class RecordingOpener:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls: list[tuple[object, float]] = []

    def __call__(self, request, *, timeout: float):
        self.calls.append((request, timeout))
        return self.response


class V2exJobSourceTests(unittest.TestCase):
    def test_fetches_fixed_anonymous_feed_and_normalizes_clear_recruitment(self):
        opener = RecordingOpener(FakeResponse(feed(item())))
        source = V2exJobSource(
            opener=opener,
            now=lambda: datetime(2026, 7, 30, 10, tzinfo=timezone.utc),
        )

        result = source.fetch()

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.discovered_count, 1)
        self.assertEqual(len(result.outcomes), 1)
        outcome = result.outcomes[0]
        self.assertEqual(outcome.classification, "recruitment")
        self.assertEqual(outcome.confidence, "high")
        self.assertEqual(outcome.disposition, "normalized")
        self.assertIs(type(outcome.job), NormalizedJob)
        self.assertEqual(outcome.job.source, "v2ex")
        self.assertEqual(
            outcome.job.source_job_id,
            "https://www.v2ex.com/t/1001",
        )
        self.assertEqual(
            outcome.job.source_url,
            "https://www.v2ex.com/t/1001",
        )
        self.assertEqual(outcome.job.locations, ("上海",))
        self.assertEqual(outcome.job.salary_text, "20k-30k/月")
        self.assertEqual(outcome.job.detail_status, "partial")
        self.assertEqual(
            outcome.risk_tags,
            ("community_post", "identity_unverified", "missing_deadline"),
        )

        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, FEED_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, 10.0)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("token", headers)

    def test_classifies_every_post_kind_and_only_normalizes_clear_jobs(self):
        samples = (
            item(
                item_id="https://www.v2ex.com/t/2001",
                title="[内推] 后端工程师",
                content_html=(
                    "<p>可内推，团队招聘 Python 后端工程师，地点：北京</p>"
                ),
            ),
            item(
                item_id="https://www.v2ex.com/t/2002",
                title="[求职] Python 开发",
                content_html="<p>本人正在寻找工作机会，这是个人简历概述。</p>",
            ),
            item(
                item_id="https://www.v2ex.com/t/2003",
                title="[外包] 小程序开发需求",
                content_html="<p>项目外包，预算 2 万，找开发接单。</p>",
            ),
            item(
                item_id="https://www.v2ex.com/t/2004",
                title="[推广] 开发者课程",
                content_html="<p>培训课程限时优惠，这是广告推广。</p>",
            ),
            item(
                item_id="https://www.v2ex.com/t/2005",
                title="大家如何看远程办公",
                content_html="<p>请教大家，讨论一下团队协作经验。</p>",
            ),
            item(
                item_id="https://www.v2ex.com/t/2006",
                title="杭州 Python 机会",
                content_html="<p>有个机会，感兴趣可以聊聊。</p>",
            ),
            item(
                item_id="https://www.v2ex.com/t/2007",
                title="[招聘/外包] Python 工程师",
                content_html="<p>招聘开发工程师，也可作为项目外包接单。</p>",
            ),
        )
        source = V2exJobSource(
            opener=RecordingOpener(FakeResponse(feed(*samples)))
        )

        result = source.fetch()

        self.assertEqual(
            [outcome.classification for outcome in result.outcomes],
            [
                "referral",
                "job_seeking",
                "outsourcing",
                "advertisement",
                "discussion",
                "uncertain",
                "uncertain",
            ],
        )
        self.assertIsNotNone(result.outcomes[0].job)
        self.assertTrue(
            all(outcome.job is None for outcome in result.outcomes[1:])
        )
        self.assertEqual(
            [outcome.disposition for outcome in result.outcomes],
            [
                "normalized",
                "review",
                "review",
                "review",
                "review",
                "review",
                "review",
            ],
        )
        self.assertIn(
            "possible_outsourcing",
            result.outcomes[2].risk_tags,
        )

    def test_does_not_normalize_recruitment_without_a_second_job_fact(self):
        generic_role = item(
            item_id="https://www.v2ex.com/t/2011",
            title="[招聘] 岗位信息",
            content_html=(
                "<p>公司：示例科技</p><p>地点：北京</p>"
            ),
        )
        source = V2exJobSource(
            opener=RecordingOpener(
                FakeResponse(
                    feed(
                        item(
                            item_id="https://www.v2ex.com/t/2010",
                            title="[招聘] Java 工程师",
                            content_html="<p>我们正在招聘 Java 开发工程师。</p>",
                        ),
                        generic_role,
                    )
                )
            )
        )

        result = source.fetch()

        self.assertEqual(result.status, "complete")
        for outcome in result.outcomes:
            self.assertEqual(outcome.classification, "uncertain")
            self.assertEqual(outcome.confidence, "low")
            self.assertEqual(outcome.disposition, "review")
            self.assertIsNone(outcome.job)
            self.assertIn("insufficient_job_evidence", outcome.reason_codes)

    def test_extracts_only_explicit_job_features(self):
        samples = (
            item(
                item_id="https://www.v2ex.com/t/3001",
                title="[招聘][远程][实习] AI 工程师",
                content_html=(
                    "<p>海外团队招聘 AI 实习工程师，支持 remote。</p>"
                    "<p>公司：示例科技</p><p>工作地点：远程</p>"
                    "<p>薪资：300-500元/天</p>"
                ),
            ),
            item(
                item_id="https://www.v2ex.com/t/3002",
                title="[内推] 兼职合同工程师",
                content_html=(
                    "<p>团队招聘 Python 工程师，可内推。</p>"
                    "<p>兼职，短期合同，地点：北京</p>"
                ),
            ),
        )
        source = V2exJobSource(
            opener=RecordingOpener(FakeResponse(feed(*samples)))
        )

        result = source.fetch()

        internship, contract = result.outcomes
        self.assertTrue(internship.signals.remote)
        self.assertTrue(internship.signals.internship)
        self.assertTrue(internship.signals.foreign_company_or_team)
        self.assertFalse(internship.signals.part_time)
        self.assertFalse(internship.signals.contract)
        self.assertEqual(internship.signals.locations, ("远程",))
        self.assertEqual(internship.signals.salary_text, "300-500元/天")
        self.assertEqual(internship.job.company, "示例科技")
        self.assertEqual(internship.job.recruitment_channel, "internship")
        self.assertEqual(internship.job.employment_type, "intern")
        self.assertEqual(internship.job.work_mode, "remote")
        self.assertEqual(internship.job.internship, {"explicit": True})

        self.assertTrue(contract.signals.part_time)
        self.assertTrue(contract.signals.contract)
        self.assertEqual(contract.signals.locations, ("北京",))
        self.assertEqual(contract.job.employment_type, "part_time")
        self.assertIsNone(contract.job.company)
        self.assertIsNone(contract.job.salary_text)

    def test_sanitizes_malicious_long_html_and_removes_contacts_everywhere(self):
        contact_values = (
            "hr@example.com",
            "13800138000",
            "wx_hr_2026",
            "123456789",
            "abcde",
            "123456",
        )
        malicious = (
            "<script>steal_cookie()</script>"
            "<style>.secret{display:block}</style>"
            "<template>hidden payload</template>"
            "<img src=x onerror=attack()>"
            "<a href='https://evil.example/track'>招聘 Python 工程师</a>"
            "<p>工作地点：上海</p>"
            "<p>邮箱：hr@example.com 手机：13800138000</p>"
            "<p>微信：wx_hr_2026 微信号：abcde QQ：123456789</p>"
            "<p>vx: 123456</p>"
            f"<p>{'岗位说明' * 6000}</p>"
        )
        malicious_item = item(
            item_id="https://www.v2ex.com/t/4001",
            title=(
                "[招聘] Python 工程师 "
                "hr@example.com"
            ),
            content_html=malicious,
        )
        malicious_item["id"] = "hr@example.com"
        source = V2exJobSource(
            opener=RecordingOpener(
                FakeResponse(
                    feed(malicious_item)
                )
            )
        )

        result = source.fetch()

        self.assertEqual(result.status, "partial")
        outcome = result.outcomes[0]
        self.assertEqual(outcome.classification, "recruitment")
        self.assertIn("body_truncated", outcome.reason_codes)
        self.assertIn("id_from_url", outcome.reason_codes)
        self.assertEqual(
            outcome.identity,
            "https://www.v2ex.com/t/4001",
        )
        self.assertLessEqual(len(outcome.safe_text), 20_000)
        serialized = json.dumps(asdict(outcome), ensure_ascii=False)
        for contact in contact_values:
            self.assertNotIn(contact, serialized)
        for unsafe in (
            "steal_cookie",
            "display:block",
            "hidden payload",
            "onerror",
            "evil.example",
        ):
            self.assertNotIn(unsafe, serialized)

    def test_deduplicates_feed_items_and_uses_caller_owned_cursor(self):
        with self.assertRaises(ValueError):
            V2exCursor(("\x00https://www.v2ex.com/t/5000",))

        first_items = (
            item(item_id="https://www.v2ex.com/t/5001"),
            item(item_id="https://www.v2ex.com/t/5001"),
            item(item_id="https://www.v2ex.com/t/5002"),
        )
        first = V2exJobSource(
            opener=RecordingOpener(FakeResponse(feed(*first_items)))
        ).fetch()

        self.assertEqual(first.discovered_count, 3)
        self.assertEqual(len(first.outcomes), 2)
        self.assertEqual(first.duplicate_count, 1)
        self.assertEqual(first.seen_count, 0)
        self.assertEqual(
            first.next_cursor.seen_identities,
            (
                "https://www.v2ex.com/t/5001",
                "https://www.v2ex.com/t/5002",
            ),
        )

        second_items = (
            item(item_id="https://www.v2ex.com/t/5001"),
            item(item_id="https://www.v2ex.com/t/5002"),
            item(item_id="https://www.v2ex.com/t/5003"),
        )
        second = V2exJobSource(
            opener=RecordingOpener(FakeResponse(feed(*second_items)))
        ).fetch(cursor=first.next_cursor)

        self.assertEqual(second.discovered_count, 3)
        self.assertEqual(second.seen_count, 2)
        self.assertEqual(second.duplicate_count, 0)
        self.assertEqual(
            [outcome.identity for outcome in second.outcomes],
            ["https://www.v2ex.com/t/5003"],
        )
        self.assertEqual(
            second.next_cursor.seen_identities,
            (
                "https://www.v2ex.com/t/5003",
                "https://www.v2ex.com/t/5001",
                "https://www.v2ex.com/t/5002",
            ),
        )

    def test_missing_optional_fields_and_malformed_items_have_partial_semantics(self):
        valid_minimal = {
            "id": "https://www.v2ex.com/t/6001",
            "url": "https://www.v2ex.com/t/6001",
            "title": "[招聘] 测试工程师",
            "content_text": "团队招聘测试工程师，工作地点：深圳",
        }
        id_fallback = {
            "url": "https://www.v2ex.com/t/6002",
            "title": "[招聘] 前端工程师",
            "content_html": "<p>团队招聘前端开发工程师，地点：广州</p>",
        }
        missing_content = {
            "id": "https://www.v2ex.com/t/6003",
            "url": "https://www.v2ex.com/t/6003",
            "title": "缺少正文",
        }
        payload = feed(
            valid_minimal,
            id_fallback,
            missing_content,
            "not-an-object",
        )
        source = V2exJobSource(
            opener=RecordingOpener(FakeResponse(payload))
        )

        result = source.fetch()

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.discovered_count, 4)
        self.assertEqual(len(result.outcomes), 4)
        valid, fallback, missing, non_object = result.outcomes
        self.assertEqual(valid.disposition, "normalized")
        self.assertIsNone(valid.job.published_at)
        self.assertEqual(valid.job.locations, ("深圳",))

        self.assertEqual(
            fallback.identity,
            "https://www.v2ex.com/t/6002",
        )
        self.assertEqual(fallback.disposition, "normalized")
        self.assertIn("id_from_url", fallback.reason_codes)

        self.assertEqual(missing.classification, "uncertain")
        self.assertEqual(missing.disposition, "invalid")
        self.assertIn("missing_content", missing.reason_codes)
        self.assertIsNone(missing.job)

        self.assertIsNone(non_object.identity)
        self.assertIsNone(non_object.source_url)
        self.assertEqual(non_object.disposition, "invalid")
        self.assertEqual(
            non_object.reason_codes,
            ("item_not_object",),
        )

    def test_http_and_feed_failures_do_not_advance_cursor(self):
        oversized = b"x" * (MAX_RESPONSE_BYTES + 1)
        cases = (
            (
                "non_json",
                FakeResponse(b"not-json"),
                "invalid_json",
            ),
            (
                "wrong_content_type",
                FakeResponse(
                    feed(item()),
                    content_type="text/html;charset=utf-8",
                ),
                "invalid_content_type",
            ),
            (
                "body_too_large",
                FakeResponse(oversized),
                "response_too_large",
            ),
            (
                "declared_too_large",
                FakeResponse(
                    b"{}",
                    content_length=str(MAX_RESPONSE_BYTES + 1),
                ),
                "response_too_large",
            ),
            (
                "declared_length_mismatch",
                FakeResponse(
                    feed(item()),
                    content_length="1",
                ),
                "invalid_content_length",
            ),
            (
                "unsafe_redirect",
                FakeResponse(
                    feed(item()),
                    final_url="https://evil.example/feed.json",
                ),
                "unsafe_redirect",
            ),
            (
                "redirect_status",
                FakeResponse(feed(item()), status=302),
                "unsafe_redirect",
            ),
            (
                "forbidden",
                FakeResponse(feed(item()), status=403),
                "http_forbidden",
            ),
            (
                "rate_limited",
                FakeResponse(feed(item()), status=429),
                "rate_limited",
            ),
        )
        cursor = V2exCursor(("https://www.v2ex.com/t/9999",))

        for name, response, error_code in cases:
            with self.subTest(name=name):
                opener = RecordingOpener(response)
                result = V2exJobSource(opener=opener).fetch(cursor=cursor)

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.outcomes, ())
                self.assertEqual(result.next_cursor, cursor)
                self.assertEqual(result.error_code, error_code)
                self.assertEqual(len(opener.calls), 1)

    def test_deep_json_fails_safely_without_advancing_cursor(self):
        prefix = (
            b'{"version":"https://jsonfeed.org/version/1",'
            b'"title":"jobs","items":[],"unknown":'
        )
        body = prefix + (b"[" * 10_000) + b"0" + (b"]" * 10_000) + b"}"
        cursor = V2exCursor(("https://www.v2ex.com/t/9999",))

        result = V2exJobSource(
            opener=RecordingOpener(FakeResponse(body))
        ).fetch(cursor=cursor)

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "invalid_json")
        self.assertEqual(result.next_cursor, cursor)

        nan_body = (
            b'{"version":"https://jsonfeed.org/version/1",'
            b'"title":"jobs","items":[],"unknown":NaN}'
        )
        nan_result = V2exJobSource(
            opener=RecordingOpener(FakeResponse(nan_body))
        ).fetch(cursor=cursor)
        self.assertEqual(nan_result.status, "failed")
        self.assertEqual(nan_result.error_code, "invalid_json")
        self.assertEqual(nan_result.next_cursor, cursor)

    def test_control_characters_cannot_enter_item_identity_or_url(self):
        unsafe_url = "\x00https://www.v2ex.com/t/7100"
        unsafe_item = item(item_id=unsafe_url)
        source = V2exJobSource(
            opener=RecordingOpener(FakeResponse(feed(unsafe_item)))
        )

        result = source.fetch()

        self.assertEqual(result.status, "partial")
        outcome = result.outcomes[0]
        self.assertEqual(outcome.disposition, "invalid")
        self.assertIsNone(outcome.identity)
        self.assertIsNone(outcome.source_url)
        self.assertNotIn("\x00", json.dumps(asdict(outcome)))

        invalid_url_item = item(item_id="https://www.v2ex.com/t/7101")
        invalid_url_item["url"] = "https://evil.example/t/7101"
        fallback = V2exJobSource(
            opener=RecordingOpener(
                FakeResponse(feed(invalid_url_item))
            )
        ).fetch()
        self.assertEqual(fallback.status, "partial")
        fallback_outcome = fallback.outcomes[0]
        self.assertEqual(
            fallback_outcome.source_url,
            "https://www.v2ex.com/t/7101",
        )
        self.assertIn("url_from_id", fallback_outcome.reason_codes)

    def test_validates_feed_v1_shape_and_item_limit(self):
        invalid_payloads = (
            {
                "version": "https://jsonfeed.org/version/1.1",
                "title": "酷工作",
                "items": [],
            },
            {
                "version": "https://jsonfeed.org/version/1",
                "items": [],
            },
            {
                "version": "https://jsonfeed.org/version/1",
                "title": "酷工作",
                "items": {},
            },
            {
                "version": "https://jsonfeed.org/version/1",
                "title": "酷工作",
                "items": [item()] * 201,
            },
        )
        cursor = V2exCursor(("https://www.v2ex.com/t/9999",))

        for payload in invalid_payloads:
            with self.subTest(payload_keys=tuple(payload)):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                result = V2exJobSource(
                    opener=RecordingOpener(FakeResponse(body))
                ).fetch(cursor=cursor)

                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error_code, "invalid_feed")
                self.assertEqual(result.next_cursor, cursor)

    def test_uses_only_valid_rfc3339_dates_and_explicit_deadlines(self):
        with_deadline = item(
            item_id="https://www.v2ex.com/t/7001",
            title="[招聘] 数据工程师",
            content_html=(
                "<p>团队招聘数据工程师。</p>"
                "<p>工作地点：上海</p>"
                "<p>申请截止：2026-08-31</p>"
            ),
        )
        invalid_date = item(
            item_id="https://www.v2ex.com/t/7002",
            title="[招聘] 平台工程师",
            content_html=(
                "<p>团队招聘平台开发工程师。</p>"
                "<p>地点：北京</p>"
            ),
        )
        invalid_date["date_published"] = "not-a-date"
        non_rfc3339_date = item(
            item_id="https://www.v2ex.com/t/7003",
            title="[招聘] 后端工程师",
            content_html=(
                "<p>团队招聘后端开发工程师。</p>"
                "<p>地点：深圳</p>"
            ),
        )
        non_rfc3339_date["date_published"] = (
            "2026-W31-4T09:00:00+00:00"
        )
        source = V2exJobSource(
            opener=RecordingOpener(
                FakeResponse(
                    feed(with_deadline, invalid_date, non_rfc3339_date)
                )
            )
        )

        result = source.fetch()

        self.assertEqual(result.status, "partial")
        deadline, invalid, non_rfc3339 = result.outcomes
        self.assertEqual(deadline.job.published_at, "2026-07-30T09:00:00+00:00")
        self.assertEqual(deadline.signals.deadline_text, "2026-08-31")
        self.assertEqual(deadline.job.expires_at, "2026-08-31")
        self.assertNotIn("missing_deadline", deadline.risk_tags)

        self.assertIsNone(invalid.job.published_at)
        self.assertIn("invalid_date_published", invalid.reason_codes)
        self.assertIn("missing_deadline", invalid.risk_tags)
        self.assertIsNone(non_rfc3339.job.published_at)
        self.assertIn(
            "invalid_date_published",
            non_rfc3339.reason_codes,
        )

    def test_handles_explicit_edge_signals_without_guessing(self):
        bracket_location = item(
            item_id="https://www.v2ex.com/t/7201",
            title="[昆明][招聘] Python 工程师",
            content_html=(
                "<p>团队招聘 Python 开发工程师，20k-30k/月。</p>"
            ),
        )
        training = item(
            item_id="https://www.v2ex.com/t/7202",
            title="Python 培训",
            content_html="<p>提供开发培训，欢迎了解。</p>",
        )
        budget_outsourcing = item(
            item_id="https://www.v2ex.com/t/7203",
            title="小程序项目",
            content_html="<p>预算 2 万，找开发完成需求。</p>",
        )
        invalid_deadline = item(
            item_id="https://www.v2ex.com/t/7204",
            title="[招聘] 安全工程师",
            content_html=(
                "<p>团队招聘安全工程师，地点：北京。</p>"
                "<p>截止：2026-99-99</p>"
            ),
        )
        misleading_bracket = item(
            item_id="https://www.v2ex.com/t/7205",
            title="[高薪][招聘] Python 工程师",
            content_html=(
                "<p>公司：示例团队</p>"
                "<p>招聘 Python 开发工程师。</p>"
            ),
        )
        source = V2exJobSource(
            opener=RecordingOpener(
                FakeResponse(
                    feed(
                        bracket_location,
                        training,
                        budget_outsourcing,
                        invalid_deadline,
                        misleading_bracket,
                    )
                )
            )
        )

        result = source.fetch()

        (
            location,
            advertisement,
            outsourcing,
            deadline,
            misleading,
        ) = result.outcomes
        self.assertEqual(location.classification, "recruitment")
        self.assertEqual(location.signals.locations, ("昆明",))
        self.assertEqual(location.signals.salary_text, "20k-30k/月")
        self.assertEqual(advertisement.classification, "advertisement")
        self.assertEqual(outsourcing.classification, "outsourcing")
        self.assertIn("possible_outsourcing", outsourcing.risk_tags)
        self.assertIsNone(deadline.signals.deadline_text)
        self.assertIsNone(deadline.job.expires_at)
        self.assertIn("missing_deadline", deadline.risk_tags)
        self.assertEqual(misleading.signals.locations, ())

    def test_bounds_title_before_it_enters_structured_results(self):
        oversized_title = (
            "[招聘] Python 工程师 " + ("超长标题" * 8_000)
        )
        source = V2exJobSource(
            opener=RecordingOpener(
                FakeResponse(
                    feed(
                        item(
                            item_id="https://www.v2ex.com/t/7301",
                            title=oversized_title,
                            content_html=(
                                "<p>团队招聘 Python 开发工程师，地点：上海</p>"
                            ),
                        )
                    )
                )
            )
        )

        result = source.fetch()

        self.assertEqual(result.status, "partial")
        outcome = result.outcomes[0]
        self.assertIn("title_truncated", outcome.reason_codes)
        self.assertLessEqual(len(outcome.job.title), 500)
        self.assertLessEqual(len(outcome.safe_text), 20_000)

    def test_import_and_fetch_have_no_hidden_network_thread_or_storage_side_effects(self):
        import job_sources.v2ex.source as source_module

        with (
            patch("urllib.request.urlopen") as network,
            patch.object(threading.Thread, "start") as start_thread,
        ):
            importlib.reload(source_module)

        network.assert_not_called()
        start_thread.assert_not_called()

        source = V2exJobSource(
            opener=RecordingOpener(FakeResponse(feed(item())))
        )
        with (
            patch("builtins.open") as file_open,
            patch.object(sqlite3, "connect") as connect_database,
            patch("pathlib.Path.write_text") as write_text,
            patch.object(threading.Thread, "start") as start_thread,
            patch("logging.Logger._log") as write_log,
        ):
            result = source.fetch()

        self.assertEqual(result.status, "complete")
        file_open.assert_not_called()
        connect_database.assert_not_called()
        write_text.assert_not_called()
        start_thread.assert_not_called()
        write_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
