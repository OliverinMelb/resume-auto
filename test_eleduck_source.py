from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from job_sources.eleduck import (
    EleduckCursor,
    EleduckFeedError,
    EleduckHttpError,
    EleduckPublicApiSource,
    EleduckRssSource,
    PostCategory,
)
from job_sources.eleduck.rss import FEED_URL, load_public_feed
from job_sources.eleduck.public_api import (
    JOBS_API_URL,
    MAX_LISTING_PAGE_BYTES,
    EleduckApiHttpError,
    load_public_job_page,
)
from job_sources.eleduck.coordinator import (
    EleduckCoordinator,
    EleduckIncompleteResult,
    EleduckReviewConflict,
)
from job_sources.eleduck.research import (
    ELEDUCK_PROJECT_NOTE,
    EleduckResearchSink,
    PersistedEleduckJob,
)
from job_sources.eleduck.state import (
    EleduckReviewDecision,
    EleduckSourceState,
    EleduckStateError,
    EleduckStateStore,
)
from resume_agent.repository import Repository


def rss_document(*items: str) -> bytes:
    body = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<rss version=\"2.0\"><channel><title>电鸭社区</title>"
        f"{body}</channel></rss>"
    ).encode()


def rss_item(
    *,
    title: str,
    description: str,
    link: str = "https://eleduck.com/posts/abc123",
    guid: str | None = "eleduck-guid-1",
    pub_date: str | None = "Wed, 30 Jul 2026 08:00:00 GMT",
) -> str:
    guid_xml = f"<guid>{guid}</guid>" if guid is not None else ""
    pub_date_xml = f"<pubDate>{pub_date}</pubDate>" if pub_date is not None else ""
    return (
        "<item>"
        f"<title>{title}</title>"
        f"<description>{description}</description>"
        f"<link>{link}</link>"
        f"{guid_xml}{pub_date_xml}"
        "</item>"
    )


def listing_post(
    *,
    post_id: str,
    title: str = "招聘 Python 工程师",
    summary: str = "岗位职责：维护后端服务；任职要求：熟悉 Python；全职。",
    published_at: str = "2026-08-03T18:08:06.714+08:00",
    closed: bool = False,
) -> dict[str, object]:
    return {
        "id": post_id,
        "title": title,
        "full_title": title,
        "summary": summary,
        "published_at": published_at,
        "closed": closed,
        "deleted": False,
        "hide": False,
        "status": "closed" if closed else "published",
        "visibility": "public",
        "category": {
            "id": 5,
            "name": "社区帖子招聘",
            "code": "jd",
            "kind": "work",
        },
        "tags": [],
    }


def listing_document(
    page: int,
    *posts: dict[str, object],
    total_pages: int = 1,
    next_page: int | None = None,
) -> bytes:
    return json.dumps(
        {
            "posts": list(posts),
            "pager": {
                "total_pages": total_pages,
                "current_page": page,
                "next_page": next_page,
                "prev_page": page - 1 if page > 1 else None,
                "total_count": len(posts),
                "limit_value": 25,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


class EleduckPublicApiSourceTests(unittest.TestCase):
    def test_collects_open_jobs_and_filters_closed_listings(self) -> None:
        requested_pages: list[int] = []

        def load_page(page: int) -> bytes:
            requested_pages.append(page)
            return listing_document(
                1,
                listing_post(post_id="open01"),
                listing_post(post_id="closed01", closed=True),
                total_pages=2,
                next_page=2,
            )

        result = EleduckPublicApiSource(
            load_page=load_page,
            max_pages=1,
            clock=lambda: datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc),
        ).collect()

        self.assertEqual([1], requested_pages)
        self.assertEqual(1, result.discovered_count)
        self.assertEqual(1, len(result.accepted_jobs))
        self.assertEqual("https://eleduck.com/posts/open01", result.accepted_jobs[0].source_url)
        self.assertEqual(
            (
                "guid:https://eleduck.com/posts/open01",
                "guid:https://eleduck.com/posts/closed01",
            ),
            result.next_cursor.seen_identities,
        )
        self.assertEqual("page_limit", result.stop_reason)

    def test_reads_twenty_pages_and_keeps_the_bounded_backfill_cursor(self) -> None:
        requested_pages: list[int] = []

        def load_page(page: int) -> bytes:
            requested_pages.append(page)
            posts = tuple(
                listing_post(post_id=f"p{page:02d}{index:02d}")
                for index in range(25)
            )
            return listing_document(
                page,
                *posts,
                total_pages=21,
                next_page=page + 1,
            )

        result = EleduckPublicApiSource(load_page=load_page).collect()

        self.assertEqual(list(range(1, 21)), requested_pages)
        self.assertEqual(500, result.discovered_count)
        self.assertEqual(500, len(result.next_cursor.seen_identities))
        self.assertEqual("page_limit", result.stop_reason)

    def test_stops_after_a_whole_page_has_already_been_seen(self) -> None:
        posts = tuple(
            listing_post(post_id=f"seen{index:02d}")
            for index in range(25)
        )
        cursor = EleduckCursor(
            tuple(
                f"guid:https://eleduck.com/posts/seen{index:02d}"
                for index in range(25)
            )
        )
        requested_pages: list[int] = []

        def load_page(page: int) -> bytes:
            requested_pages.append(page)
            if page != 1:
                raise AssertionError("source fetched beyond the fully-seen page")
            return listing_document(
                1,
                *posts,
                total_pages=21,
                next_page=2,
            )

        result = EleduckPublicApiSource(load_page=load_page).collect(cursor)

        self.assertEqual([1], requested_pages)
        self.assertEqual(25, result.deduplicated_count)
        self.assertEqual(0, len(result.accepted_jobs))
        self.assertEqual(cursor, result.next_cursor)
        self.assertEqual("seen_page", result.stop_reason)

    def test_deduplicates_a_post_repeated_across_adjacent_pages(self) -> None:
        pages = {
            1: listing_document(
                1,
                listing_post(post_id="repeat01"),
                total_pages=2,
                next_page=2,
            ),
            2: listing_document(
                2,
                listing_post(post_id="repeat01"),
                listing_post(post_id="unique02"),
                total_pages=2,
            ),
        }

        result = EleduckPublicApiSource(
            load_page=pages.__getitem__,
            max_pages=2,
        ).collect()

        self.assertEqual(2, len(result.accepted_jobs))
        self.assertEqual(1, result.deduplicated_count)
        self.assertEqual(2, len(result.next_cursor.seen_identities))

    def test_rejects_a_page_whose_pager_does_not_match_the_request(self) -> None:
        source = EleduckPublicApiSource(
            load_page=lambda _page: listing_document(
                2,
                listing_post(post_id="wrong01"),
                total_pages=2,
            ),
            max_pages=1,
        )

        with self.assertRaisesRegex(EleduckFeedError, "pager"):
            source.collect(EleduckCursor(("guid:existing",)))

    def test_rejects_a_missing_next_page_before_the_reported_end(self) -> None:
        source = EleduckPublicApiSource(
            load_page=lambda _page: listing_document(
                1,
                listing_post(post_id="truncated01"),
                total_pages=2,
                next_page=None,
            ),
            max_pages=1,
        )

        with self.assertRaisesRegex(EleduckFeedError, "next page"):
            source.collect()

    def test_rejects_a_post_id_that_could_escape_the_official_route(self) -> None:
        source = EleduckPublicApiSource(
            load_page=lambda _page: listing_document(
                1,
                listing_post(post_id="../../evil.example"),
            ),
            max_pages=1,
        )

        with self.assertRaisesRegex(EleduckFeedError, "ID"):
            source.collect()


class EleduckPublicApiHttpTests(unittest.TestCase):
    def test_reader_rejects_a_page_outside_the_twenty_page_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed range"):
            load_public_job_page(21)

    def test_reader_uses_only_the_allowlisted_public_json_endpoint(self) -> None:
        expected_url = f"{JOBS_API_URL}?page=3&sort=-published_at"

        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "application/json"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            @staticmethod
            def geturl() -> str:
                return expected_url

            @staticmethod
            def read(_size: int) -> bytes:
                return b'{"posts":[],"pager":{}}'

        captured_request = None

        class Opener:
            @staticmethod
            def open(request, *, timeout):
                nonlocal captured_request
                captured_request = request
                self.assertEqual(15, timeout)
                return Response()

        with patch(
            "job_sources.eleduck.public_api.build_opener",
            return_value=Opener(),
        ):
            payload = load_public_job_page(3)

        self.assertEqual(b'{"posts":[],"pager":{}}', payload)
        self.assertEqual(expected_url, captured_request.full_url)
        headers = {key.lower(): value for key, value in captured_request.header_items()}
        self.assertEqual({"accept", "user-agent"}, set(headers))
        for forbidden in ("cookie", "authorization", "token", "referer"):
            self.assertNotIn(forbidden, headers)

    def test_reader_rejects_a_redirected_response(self) -> None:
        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "application/json"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            @staticmethod
            def geturl() -> str:
                return "https://eleduck.com/verification"

        class Opener:
            @staticmethod
            def open(_request, *, timeout):
                self.assertEqual(15, timeout)
                return Response()

        with patch(
            "job_sources.eleduck.public_api.build_opener",
            return_value=Opener(),
        ):
            with self.assertRaisesRegex(EleduckApiHttpError, "redirected"):
                load_public_job_page(1)

    def test_reader_rejects_non_json_and_oversized_responses(self) -> None:
        expected_url = f"{JOBS_API_URL}?page=1&sort=-published_at"

        class Headers:
            content_type = "text/html"

            @classmethod
            def get_content_type(cls) -> str:
                return cls.content_type

        class Response:
            headers = Headers()
            payload = b"not-json"

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            @staticmethod
            def geturl() -> str:
                return expected_url

            @classmethod
            def read(cls, _size: int) -> bytes:
                return cls.payload

        class Opener:
            @staticmethod
            def open(_request, *, timeout):
                self.assertEqual(15, timeout)
                return Response()

        with patch(
            "job_sources.eleduck.public_api.build_opener",
            return_value=Opener(),
        ):
            with self.assertRaisesRegex(EleduckApiHttpError, "non-JSON"):
                load_public_job_page(1)

            Headers.content_type = "application/json"
            Response.payload = b"x" * (MAX_LISTING_PAGE_BYTES + 1)
            with self.assertRaisesRegex(EleduckApiHttpError, "size limit"):
                load_public_job_page(1)


class EleduckSourceTests(unittest.TestCase):
    def test_parses_recruitment_rss_into_partial_normalized_job(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责：维护后端服务；任职要求：熟悉 Python；全职。",
            )
        )
        fetched_at = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
        source = EleduckRssSource(
            load_feed=lambda: payload,
            clock=lambda: fetched_at,
        )

        result = source.collect()

        self.assertTrue(result.complete)
        self.assertEqual(1, len(result.accepted_jobs))
        job = result.accepted_jobs[0]
        self.assertEqual("eleduck", job.source)
        self.assertEqual("eleduck-guid-1", job.source_job_id)
        self.assertEqual("https://eleduck.com/posts/abc123", job.source_url)
        self.assertEqual("2026-07-30T08:00:00+00:00", job.published_at)
        self.assertEqual("2026-07-30T09:00:00+00:00", job.last_verified_at)
        self.assertEqual("partial", job.detail_status)
        self.assertEqual(PostCategory.RECRUITMENT.value, job.raw_data["category"])

    def test_missing_optional_fields_is_retained_for_review(self) -> None:
        payload = rss_document(
            rss_item(
                title="聊聊远程工作",
                description="一些个人观察",
                guid=None,
                pub_date=None,
            )
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertTrue(result.complete)
        self.assertEqual(1, len(result.review_items))
        item = result.review_items[0].item
        self.assertIsNone(item.guid)
        self.assertIsNone(item.published_at)
        self.assertEqual(
            ("url:https://eleduck.com/posts/abc123",),
            result.next_cursor.seen_identities,
        )

    def test_deduplicates_by_guid_before_link_and_falls_back_to_link(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责和任职要求，全职",
                link="https://eleduck.com/posts/abc123",
                guid="same-guid",
            ),
            rss_item(
                title="招聘 Python 工程师（重复）",
                description="岗位职责和任职要求，全职",
                link="https://eleduck.com/posts/def456",
                guid="same-guid",
            ),
            rss_item(
                title="普通帖子",
                description="内容",
                link="https://eleduck.com/posts/ghi789",
                guid=None,
            ),
            rss_item(
                title="普通帖子（重复）",
                description="内容",
                link="https://eleduck.com/posts/ghi789/",
                guid=None,
            ),
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertEqual(4, result.discovered_count)
        self.assertEqual(2, result.deduplicated_count)
        self.assertEqual(
            (
                "guid:same-guid",
                "url:https://eleduck.com/posts/ghi789",
            ),
            result.next_cursor.seen_identities,
        )

    def test_reusing_cursor_emits_no_duplicate_and_keeps_cursor_stable(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责和任职要求，全职",
            )
        )
        source = EleduckRssSource(load_feed=lambda: payload)

        first = source.collect()
        second = source.collect(first.next_cursor)

        self.assertEqual((), second.accepted_jobs)
        self.assertEqual(1, second.deduplicated_count)
        self.assertEqual(first.next_cursor, second.next_cursor)

    def test_invalid_item_marks_partial_and_does_not_advance_cursor(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责和任职要求，全职",
            ),
            rss_item(
                title="非法链接",
                description="内容",
                link="https://example.com/posts/bad123",
                guid="bad-guid",
            ),
        )
        cursor = EleduckCursor(("guid:already-seen",))

        result = EleduckRssSource(load_feed=lambda: payload).collect(cursor)

        self.assertFalse(result.complete)
        self.assertEqual("invalid_items", result.stop_reason)
        self.assertEqual(1, result.invalid_item_count)
        self.assertEqual(cursor, result.next_cursor)

    def test_job_seeking_is_excluded_from_jobs(self) -> None:
        result = self._collect_one(
            title="本人求职 Python 后端",
            description="五年经验，简历和作品集齐全，可入职。",
        )

        self.assertEqual((), result.accepted_jobs)
        classified = result.non_job_items[0]
        self.assertEqual(PostCategory.JOB_SEEKING, classified.classification.category)
        self.assertIn(
            "job_seeking.candidate",
            classified.classification.matched_rule_ids,
        )

    def test_outsourcing_and_partner_are_classified_separately(self) -> None:
        payload = rss_document(
            rss_item(
                title="外包项目需求：小程序",
                description="预算两万元，寻找开发团队按期交付。",
                link="https://eleduck.com/posts/out123",
                guid="out-guid",
            ),
            rss_item(
                title="寻找技术合伙人共同创业",
                description="以股权合作，共同承担产品和融资工作。",
                link="https://eleduck.com/posts/partner1",
                guid="partner-guid",
            ),
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertEqual(
            [
                PostCategory.OUTSOURCING_REQUEST,
                PostCategory.PARTNER_SEEKING,
            ],
            [item.classification.category for item in result.non_job_items],
        )

    def test_research_sample_title_shapes_are_covered(self) -> None:
        payload = rss_document(
            rss_item(
                title="【香港直招】全远程新媒体助理 在家工作",
                description="负责账号内容维护，欢迎投递。",
                link="https://eleduck.com/posts/direct1",
                guid="direct-guid",
            ),
            rss_item(
                title="【寻技术开发团队】社群智能运营管理系统开发需求",
                description="需要开发团队完成项目交付。",
                link="https://eleduck.com/posts/team123",
                guid="team-guid",
            ),
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertEqual(1, len(result.accepted_jobs))
        self.assertEqual(
            PostCategory.OUTSOURCING_REQUEST,
            result.non_job_items[0].classification.category,
        )

    def test_advertisement_and_discussion_are_classified_separately(self) -> None:
        payload = rss_document(
            rss_item(
                title="产品自荐推广",
                description="本周优惠，欢迎体验。",
                link="https://eleduck.com/posts/ad1234",
                guid="ad-guid",
            ),
            rss_item(
                title="请问远程工作靠谱吗",
                description="大家觉得怎么选择？求交流。",
                link="https://eleduck.com/posts/talk12",
                guid="talk-guid",
            ),
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertEqual(
            [PostCategory.ADVERTISEMENT, PostCategory.DISCUSSION],
            [item.classification.category for item in result.non_job_items],
        )

    def test_insufficient_or_conflicting_evidence_is_retained_for_review(self) -> None:
        payload = rss_document(
            rss_item(
                title="远程办公随想",
                description="记录今天的一点感受。",
                link="https://eleduck.com/posts/unknown1",
                guid="unknown-guid",
            ),
            rss_item(
                title="招聘外包 Python 工程师",
                description="岗位职责明确，项目预算两万元，要求团队按期交付。",
                link="https://eleduck.com/posts/conflict",
                guid="conflict-guid",
            ),
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertEqual(2, len(result.review_items))
        insufficient, conflict = result.review_items
        self.assertEqual(
            ("uncertain.insufficient_evidence",),
            insufficient.classification.matched_rule_ids,
        )
        self.assertIn(
            "uncertain.conflict",
            conflict.classification.matched_rule_ids,
        )
        self.assertEqual(
            (
                PostCategory.RECRUITMENT,
                PostCategory.OUTSOURCING_REQUEST,
            ),
            conflict.classification.candidate_categories,
        )

    def test_html_contacts_and_suspected_secrets_are_safely_redacted(self) -> None:
        payload = rss_document(
            rss_item(
                title="<b>招聘 Python 工程师</b>",
                description=(
                    "<p>岗位职责：维护服务；任职要求：全职。</p>"
                    "<script>alert('x')</script>"
                    " 邮箱 dev@example.com 手机 13800138000"
                    " 备用手机 139 0013 9000"
                    " 微信: wx_secret 微信号: other_secret QQ: 123456"
                    " Cookie: session-private Token=token-private"
                ),
            )
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        rendered = repr(result)
        for secret in (
            "<b>",
            "<script>",
            "alert('x')",
            "dev@example.com",
            "13800138000",
            "139 0013 9000",
            "wx_secret",
            "other_secret",
            "123456",
            "session-private",
            "token-private",
        ):
            self.assertNotIn(secret, rendered)
        job = result.accepted_jobs[0]
        self.assertIn("[redacted-email]", job.description)
        self.assertIn("[redacted-phone]", job.description)
        self.assertIn("[redacted-contact]", job.description)
        self.assertIn("[redacted-secret]", job.description)

    def test_doctype_and_entity_declarations_are_rejected(self) -> None:
        payload = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE rss [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<rss version="2.0"><channel><item>'
            b"<title>&xxe;</title>"
            b"<link>https://eleduck.com/posts/abc123</link>"
            b"</item></channel></rss>"
        )

        with self.assertRaisesRegex(EleduckFeedError, "forbidden XML"):
            EleduckRssSource(load_feed=lambda: payload).collect()

    def test_utf16_doctype_cannot_bypass_entity_rejection(self) -> None:
        payload = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE rss [<!ENTITY xxe "expanded-secret">]>'
            '<rss version="2.0"><channel><item>'
            "<title>&xxe;</title>"
            "<link>https://eleduck.com/posts/abc123</link>"
            "</item></channel></rss>"
        ).encode("utf-16")

        with self.assertRaises(EleduckFeedError) as raised:
            EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertNotIn("expanded-secret", str(raised.exception))

    def test_deep_item_markup_is_counted_invalid_without_recursion_leak(self) -> None:
        nested = "<div>" * 200 + "content" + "</div>" * 200
        payload = rss_document(
            rss_item(
                title="深层内容",
                description=nested,
            )
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertFalse(result.complete)
        self.assertEqual(1, result.invalid_item_count)
        self.assertEqual((), result.review_items)

    def test_missing_title_and_description_do_not_crash(self) -> None:
        payload = rss_document(
            "<item>"
            "<link>https://eleduck.com/posts/empty12</link>"
            "</item>"
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertTrue(result.complete)
        self.assertEqual("", result.review_items[0].item.title)
        self.assertEqual("", result.review_items[0].item.description)

    def test_public_text_is_bounded(self) -> None:
        payload = rss_document(
            rss_item(
                title="随想" * 500,
                description="普通公开内容" * 2_000,
            )
        )

        item = EleduckRssSource(load_feed=lambda: payload).collect().review_items[0].item

        self.assertEqual(300, len(item.title))
        self.assertEqual(4_000, len(item.description))

    def test_bidirectional_and_zero_width_controls_are_removed(self) -> None:
        result = self._collect_one(
            title="普通\u202e标题",
            description="零宽\u200b内容",
        )

        rendered = repr(result)
        self.assertNotIn("\u202e", rendered)
        self.assertNotIn("\u200b", rendered)

    def test_malformed_whole_feed_raises_safe_typed_error(self) -> None:
        payload = b"<rss><channel><item>private-payload"

        with self.assertRaises(EleduckFeedError) as raised:
            EleduckRssSource(load_feed=lambda: payload).collect()

        self.assertNotIn("private-payload", str(raised.exception))

    def test_candidate_looking_for_an_engineer_role_is_not_recruitment(self) -> None:
        result = self._collect_one(
            title="本人求职，寻找 Python 工程师岗位",
            description="五年后端经验，简历齐全，可入职。",
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual((), result.review_items)
        self.assertEqual(
            PostCategory.JOB_SEEKING,
            result.non_job_items[0].classification.category,
        )

    def test_negated_recruitment_is_not_accepted(self) -> None:
        result = self._collect_one(
            title="暂不招聘 Python 工程师",
            description="岗位职责与任职要求仅作历史存档。",
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual(1, len(result.review_items))
        self.assertIn(
            "recruitment.negated",
            result.review_items[0].classification.matched_rule_ids,
        )

    def test_question_about_recruiting_is_discussion_not_recruitment(self) -> None:
        result = self._collect_one(
            title="如何招聘产品经理",
            description="请问大家怎么写岗位职责和任职要求？",
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual(
            PostCategory.DISCUSSION,
            result.non_job_items[0].classification.category,
        )

    def test_reviewer_question_shape_is_discussion_not_recruitment(self) -> None:
        result = self._collect_one(
            title="请问招聘产品经理要注意什么",
            description="岗位职责怎么写，大家觉得呢？",
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual(
            PostCategory.DISCUSSION,
            result.non_job_items[0].classification.category,
        )

    def test_general_negation_before_recruiting_is_not_accepted(self) -> None:
        result = self._collect_one(
            title="不再招聘 Python 工程师",
            description="历史岗位职责与任职要求存档。",
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual(1, len(result.review_items))
        self.assertIn(
            "recruitment.negated",
            result.review_items[0].classification.matched_rule_ids,
        )

    def test_explicit_not_recruiting_interview_request_is_discussion(self) -> None:
        result = self._collect_one(
            title="想找几位最近面过海外/远程技术岗的工程师聊聊",
            description=(
                "最近在研究程序员准备海外面试这件事，想找工程师聊30分钟。"
                "这不是招聘，也不卖课。"
            ),
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual((), result.review_items)
        self.assertEqual(
            PostCategory.DISCUSSION,
            result.non_job_items[0].classification.category,
        )

    def test_recruitment_with_open_experience_requirement_is_accepted(self) -> None:
        result = self._collect_one(
            title="招聘产品经理，有什么经验都欢迎投递",
            description="岗位职责明确，全职。",
        )

        self.assertEqual(1, len(result.accepted_jobs))

    def test_no_experience_required_does_not_negate_recruitment(self) -> None:
        result = self._collect_one(
            title="招聘人事专员；无需招聘经验",
            description="岗位职责明确，全职，欢迎投递。",
        )

        self.assertEqual(1, len(result.accepted_jobs))

    def test_recruiting_domain_experience_is_not_a_negated_action(self) -> None:
        result = self._collect_one(
            title="招聘人事专员；无需招聘方面经验",
            description="岗位职责明确，全职，欢迎投递。",
        )

        self.assertEqual(1, len(result.accepted_jobs))

    def test_paused_recruiting_before_work_experience_is_negated(self) -> None:
        result = self._collect_one(
            title="暂停招聘工作经验丰富的产品经理",
            description="历史岗位职责，全职。",
        )

        self.assertEqual((), result.accepted_jobs)
        self.assertEqual(1, len(result.review_items))
        self.assertIn(
            "recruitment.negated",
            result.review_items[0].classification.matched_rule_ids,
        )

    def test_unrelated_cancellation_before_comma_does_not_negate_recruitment(
        self,
    ) -> None:
        result = self._collect_one(
            title="取消学历限制，招聘产品经理",
            description="岗位职责明确，全职，欢迎投递。",
        )

        self.assertEqual(1, len(result.accepted_jobs))

    def test_advertisement_mixed_with_discussion_is_uncertain(self) -> None:
        result = self._collect_one(
            title="产品自荐推广，大家觉得靠谱吗",
            description="本周优惠，欢迎讨论。",
        )

        self.assertEqual(1, len(result.review_items))
        classification = result.review_items[0].classification
        self.assertEqual(PostCategory.UNCERTAIN, classification.category)
        self.assertEqual(
            (
                PostCategory.ADVERTISEMENT,
                PostCategory.DISCUSSION,
            ),
            classification.candidate_categories,
        )

    def test_current_feed_identity_is_refreshed_in_bounded_cursor(self) -> None:
        cursor = EleduckCursor(tuple(f"guid:old-{index}" for index in range(256)))
        payload = rss_document(
            rss_item(
                title="普通内容",
                description="没有明确分类",
                guid="old-255",
            )
        )

        result = EleduckRssSource(load_feed=lambda: payload).collect(cursor)

        self.assertEqual(256, len(result.next_cursor.seen_identities))
        self.assertEqual(
            "guid:old-255",
            result.next_cursor.seen_identities[0],
        )
        self.assertEqual(1, result.deduplicated_count)

    def test_source_failure_does_not_mutate_cursor_or_block_independent_source(self) -> None:
        cursor = EleduckCursor(("guid:existing",))

        def fail() -> bytes:
            raise EleduckHttpError("safe transport failure")

        other_source_called = False

        def collect_other_source() -> str:
            nonlocal other_source_called
            other_source_called = True
            return "other-source-ok"

        with self.assertRaisesRegex(EleduckHttpError, "safe transport failure"):
            EleduckRssSource(load_feed=fail).collect(cursor)
        other_result = collect_other_source()

        self.assertEqual(("guid:existing",), cursor.seen_identities)
        self.assertTrue(other_source_called)
        self.assertEqual("other-source-ok", other_result)

    def test_http_reader_sends_only_public_non_secret_headers(self) -> None:
        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "application/rss+xml"

        class Response:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            @staticmethod
            def geturl() -> str:
                return FEED_URL

            @staticmethod
            def read(_size: int) -> bytes:
                return b"<rss/>"

        captured_request = None

        def fake_urlopen(request, *, timeout):
            nonlocal captured_request
            captured_request = request
            self.assertEqual(15, timeout)
            return Response()

        class Opener:
            @staticmethod
            def open(request, *, timeout):
                return fake_urlopen(request, timeout=timeout)

        captured_handler = None

        def fake_build_opener(handler):
            nonlocal captured_handler
            captured_handler = handler
            return Opener()

        with patch("job_sources.eleduck.rss.build_opener", fake_build_opener):
            payload = load_public_feed()

        self.assertEqual(b"<rss/>", payload)
        self.assertIsNone(
            captured_handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://eleduck.com/verification",
            )
        )
        headers = {key.lower(): value for key, value in captured_request.header_items()}
        self.assertEqual(
            {"accept", "user-agent"},
            set(headers),
        )
        for forbidden in ("cookie", "authorization", "token", "referer"):
            self.assertNotIn(forbidden, headers)

    def test_http_reader_rejects_redirected_response(self) -> None:
        class Headers:
            @staticmethod
            def get_content_type() -> str:
                return "application/rss+xml"

        class RedirectedResponse:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                return None

            @staticmethod
            def geturl() -> str:
                return "https://eleduck.com/verification"

        class Opener:
            @staticmethod
            def open(_request, *, timeout):
                self.assertEqual(15, timeout)
                return RedirectedResponse()

        with patch(
            "job_sources.eleduck.rss.build_opener",
            return_value=Opener(),
        ):
            with self.assertRaisesRegex(EleduckHttpError, "redirected"):
                load_public_feed()

    @staticmethod
    def _collect_one(*, title: str, description: str):
        return EleduckRssSource(
            load_feed=lambda: rss_document(
                rss_item(title=title, description=description)
            )
        ).collect()


class EleduckStateStoreTests(unittest.TestCase):
    def test_public_backfill_cursor_survives_store_reload(self) -> None:
        cursor = EleduckCursor(tuple(f"guid:listing-{index}" for index in range(500)))
        with TemporaryDirectory() as directory:
            store = EleduckStateStore(Path(directory) / "state.json")
            store.save(EleduckSourceState(cursor=cursor))

            reloaded = store.load()

        self.assertEqual(cursor, reloaded.cursor)

    def test_pending_review_survives_store_reload(self) -> None:
        payload = rss_document(
            rss_item(
                title="远程办公随想",
                description="记录今天的一点感受。",
            )
        )
        result = EleduckRssSource(load_feed=lambda: payload).collect()

        with TemporaryDirectory() as directory:
            store = EleduckStateStore(Path(directory) / "state.json")
            store.save(
                EleduckSourceState(
                    cursor=result.next_cursor,
                    pending_reviews=result.review_items,
                    last_research_at="2026-08-03T09:00:00+00:00",
                )
            )

            reloaded = store.load()

        self.assertEqual(result.next_cursor, reloaded.cursor)
        self.assertEqual(result.review_items, reloaded.pending_reviews)
        self.assertEqual("2026-08-03T09:00:00+00:00", reloaded.last_research_at)

    def test_unknown_state_fields_fail_without_echoing_file_contents(self) -> None:
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(
                '{"schema_version":1,"cursor":{"seen_identities":[]},'
                '"pending_reviews":[],"non_job_items":[],"secret":"do-not-leak"}',
                encoding="utf-8",
            )

            with self.assertRaises(EleduckStateError) as raised:
                EleduckStateStore(state_path).load()

        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_candidate_category_list_is_bounded(self) -> None:
        payload = rss_document(
            rss_item(title="远程办公随想", description="记录今天的一点感受。")
        )
        review = EleduckRssSource(load_feed=lambda: payload).collect().review_items[0]
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            store = EleduckStateStore(state_path)
            store.save(EleduckSourceState(pending_reviews=(review,)))
            state_payload = json.loads(state_path.read_text(encoding="utf-8"))
            state_payload["pending_reviews"][0]["candidate_categories"] = [
                "discussion"
            ] * 8
            state_path.write_text(
                json.dumps(state_payload, ensure_ascii=False),
                encoding="utf-8",
            )

            with self.assertRaises(EleduckStateError):
                store.load()


class EleduckCoordinatorTests(unittest.TestCase):
    def test_research_persists_new_recruitment_and_advances_cursor_once(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责：维护服务；任职要求：熟悉 Python；全职。",
            ),
            rss_item(
                title="远程办公随想",
                description="记录今天的一点感受。",
                link="https://eleduck.com/posts/review1",
                guid="review-guid",
            ),
        )
        sink = _FakeEleduckSink()
        with TemporaryDirectory() as directory:
            store = EleduckStateStore(Path(directory) / "state.json")
            coordinator = EleduckCoordinator(
                source=EleduckRssSource(load_feed=lambda: payload),
                store=store,
                sink=sink,
                evaluator=lambda job: replace(job, score=88.0, qualified=True),
                clock=lambda: datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            )

            first = coordinator.research(candidate_id="candidate-1", project_name=None)
            second = coordinator.research(candidate_id="candidate-1", project_name=None)
            state = store.load()

        self.assertEqual(("job-1",), first.job_ids)
        self.assertEqual(1, first.inserted_count)
        self.assertEqual(0, second.inserted_count)
        self.assertEqual(1, len(state.pending_reviews))
        self.assertEqual(2, len(state.cursor.seen_identities))
        self.assertEqual(1, sink.resolve_calls)
        self.assertEqual(1, sink.persist_calls)
        self.assertEqual(88.0, sink.persisted_jobs[0].score)

    def test_incomplete_feed_does_not_score_write_or_advance_state(self) -> None:
        nested = "<div>" * 200 + "content" + "</div>" * 200
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责明确；任职要求明确；全职。",
            ),
            rss_item(
                title="损坏条目",
                description=nested,
                link="https://eleduck.com/posts/bad123",
                guid="bad-guid",
            ),
        )
        sink = _FakeEleduckSink()
        evaluated = []
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            coordinator = EleduckCoordinator(
                source=EleduckRssSource(load_feed=lambda: payload),
                store=EleduckStateStore(state_path),
                sink=sink,
                evaluator=lambda job: evaluated.append(job) or job,
            )

            with self.assertRaises(EleduckIncompleteResult):
                coordinator.research(candidate_id="candidate-1", project_name=None)

            self.assertFalse(state_path.exists())
        self.assertEqual([], evaluated)
        self.assertEqual(0, sink.resolve_calls)
        self.assertEqual(0, sink.persist_calls)

    def test_invalid_public_listing_does_not_score_write_or_advance_state(self) -> None:
        sink = _FakeEleduckSink()
        evaluated = []
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            coordinator = EleduckCoordinator(
                source=EleduckPublicApiSource(
                    load_page=lambda _page: b'{"posts":[],"pager":{}}',
                    max_pages=1,
                ),
                store=EleduckStateStore(state_path),
                sink=sink,
                evaluator=lambda job: evaluated.append(job) or job,
            )

            with self.assertRaises(EleduckFeedError):
                coordinator.research(candidate_id="candidate-1", project_name=None)

            self.assertFalse(state_path.exists())
        self.assertEqual([], evaluated)
        self.assertEqual(0, sink.resolve_calls)
        self.assertEqual(0, sink.persist_calls)

    def test_empty_detail_does_not_create_an_empty_project(self) -> None:
        payload = rss_document(
            rss_item(title="招聘 Python 工程师", description="")
        )
        sink = _FakeEleduckSink()
        with TemporaryDirectory() as directory:
            coordinator = EleduckCoordinator(
                source=EleduckRssSource(load_feed=lambda: payload),
                store=EleduckStateStore(Path(directory) / "state.json"),
                sink=sink,
                evaluator=lambda job: replace(job, score=80.0, qualified=True),
            )

            outcome = coordinator.research(candidate_id=None, project_name=None)

        self.assertEqual(1, outcome.accepted_count)
        self.assertEqual(0, outcome.inserted_count)
        self.assertIsNone(outcome.candidate_id)
        self.assertEqual(0, sink.resolve_calls)
        self.assertEqual(0, sink.persist_calls)


class EleduckRouteTests(unittest.TestCase):
    def test_local_page_preview_and_research_routes_share_coordinator(self) -> None:
        from fastapi.testclient import TestClient
        import main

        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责：维护服务；任职要求：熟悉 Python；全职。",
            ),
            rss_item(
                title="远程办公随想",
                description="记录今天的一点感受。",
                link="https://eleduck.com/posts/review1",
                guid="review-guid",
            ),
        )
        sink = _FakeEleduckSink()
        with TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            source = EleduckRssSource(load_feed=lambda: payload)
            coordinator = EleduckCoordinator(
                source=source,
                store=EleduckStateStore(state_path),
                sink=sink,
                evaluator=lambda job: replace(job, score=80.0, qualified=True),
            )
            with (
                patch("job_sources.eleduck.router._coordinator", coordinator),
                patch("job_sources.eleduck.router._source", source),
            ):
                client = TestClient(main.app)
                page = client.get("/job-sources/eleduck/ui")
                preview = client.get("/job-sources/eleduck/preview")
                self.assertFalse(state_path.exists())
                research = client.post(
                    "/job-sources/eleduck/research",
                    json={"candidate_id": "candidate-1"},
                )
                rejected = client.post(
                    "/job-sources/eleduck/research",
                    json={"candidate_id": "candidate-1", "jobs": []},
                )
                whitespace_name = client.post(
                    "/job-sources/eleduck/research",
                    json={"project_name": "   "},
                )
                reviews = client.get("/job-sources/eleduck/reviews")
                identity = reviews.json()["pending"][0]["item"]["identity"]
                ignored = client.post(
                    "/job-sources/eleduck/reviews/decision",
                    json={"identity": identity, "decision": "non_job"},
                )
                conflict = client.post(
                    "/job-sources/eleduck/reviews/decision",
                    json={
                        "identity": identity,
                        "decision": "recruitment",
                        "candidate_id": "candidate-1",
                    },
                )

        self.assertEqual(200, page.status_code)
        self.assertEqual("no-store", page.headers["cache-control"])
        self.assertIn("default-src 'self'", page.headers["content-security-policy"])
        self.assertNotIn("innerHTML", page.text)
        self.assertIn('id="project-mode-new"', page.text)
        self.assertIn('id="project-mode-existing"', page.text)
        self.assertIn("新建 JD 调研项目", page.text)
        self.assertIn("使用已有 JD 调研项目", page.text)
        self.assertIn("最近 20 页", page.text)
        self.assertIn("最多 500 条", page.text)
        self.assertEqual(200, preview.status_code)
        self.assertEqual(1, preview.json()["accepted_count"])
        self.assertEqual(200, research.status_code)
        self.assertEqual(1, research.json()["inserted_count"])
        self.assertEqual(400, rejected.status_code)
        self.assertEqual(400, whitespace_name.status_code)
        self.assertEqual(1, len(reviews.json()["pending"]))
        self.assertEqual(200, ignored.status_code)
        self.assertEqual(409, conflict.status_code)

    def test_preview_does_not_touch_persistent_coordinator(self) -> None:
        from fastapi.testclient import TestClient
        import main

        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责明确；任职要求明确；全职。",
            )
        )

        class ForbiddenCoordinator:
            def preview(self):
                raise AssertionError("preview touched persistent coordinator")

        with (
            patch("job_sources.eleduck.router._coordinator", ForbiddenCoordinator()),
            patch(
                "job_sources.eleduck.router._source",
                EleduckRssSource(load_feed=lambda: payload),
                create=True,
            ),
        ):
            response = TestClient(main.app).get("/job-sources/eleduck/preview")

        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()["accepted_count"])

    def test_scoring_failure_is_reported_as_safe_server_error(self) -> None:
        from fastapi.testclient import TestClient
        import main

        class FailingCoordinator:
            def research(self, **_kwargs):
                raise ValueError("scorer internals must not leak")

        with patch("job_sources.eleduck.router._coordinator", FailingCoordinator()):
            response = TestClient(main.app).post(
                "/job-sources/eleduck/research",
                json={"candidate_id": "candidate-1"},
            )

        self.assertEqual(500, response.status_code)
        self.assertEqual("Eleduck operation failed", response.json()["detail"])
        self.assertNotIn("scorer internals", response.text)


class EleduckResearchSinkTests(unittest.TestCase):
    def test_repository_projection_is_lossy_and_idempotent_per_project(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责：维护服务；任职要求：熟悉 Python；全职。",
            )
        )
        job = replace(
            EleduckRssSource(load_feed=lambda: payload).collect().accepted_jobs[0],
            score=91.0,
            qualified=True,
        )
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "research.db")
            candidate = repository.create_candidate("电鸭调研", [], "")
            sink = EleduckResearchSink(repository)

            first = sink.persist(str(candidate["id"]), (job,))
            second = sink.persist(str(candidate["id"]), (job,))
            stored = repository.list_jobs(str(candidate["id"]))

        self.assertTrue(first[0].created)
        self.assertFalse(second[0].created)
        self.assertEqual(first[0].job_id, second[0].job_id)
        self.assertEqual(1, len(stored))
        self.assertEqual("eleduck", stored[0]["source"])
        self.assertEqual("电鸭公开职位", stored[0]["search_keyword"])
        self.assertEqual("", stored[0]["company"])
        self.assertEqual("", stored[0]["salary"])
        self.assertNotIn("https://", stored[0]["detail"])

    def test_state_save_retry_reuses_generated_project_and_job(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责：维护服务；任职要求：熟悉 Python；全职。",
            )
        )
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "research.db")
            delegate = EleduckStateStore(Path(directory) / "state.json")
            store = _FailOnceStateStore(delegate)
            current_time = [datetime(2026, 8, 3, 15, 59, tzinfo=timezone.utc)]
            coordinator = EleduckCoordinator(
                source=EleduckRssSource(load_feed=lambda: payload),
                store=store,
                sink=EleduckResearchSink(repository),
                evaluator=lambda job: replace(job, score=90.0, qualified=True),
                clock=lambda: current_time[0],
            )

            with self.assertRaises(EleduckStateError):
                coordinator.research(candidate_id=None, project_name=None)
            current_time[0] = datetime(2026, 8, 3, 16, 1, tzinfo=timezone.utc)
            replay = coordinator.research(candidate_id=None, project_name=None)
            candidates = repository.list_candidates()
            jobs = repository.list_jobs(str(candidates[0]["id"]))

        self.assertEqual(1, len(candidates))
        self.assertEqual(1, len(jobs))
        self.assertEqual(0, replay.inserted_count)
        self.assertEqual(1, replay.deduplicated_count)

    def test_new_listing_source_reuses_a_legacy_scoped_rss_project(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责明确；任职要求明确；全职。",
            )
        )
        job = EleduckRssSource(load_feed=lambda: payload).collect().accepted_jobs[0]
        legacy_note = (
            "由电鸭公开 RSS 的 JD 调研创建；未修改任何简历。\n"
            "恢复范围：named\n恢复键：" + "a" * 64
        )
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "research.db")
            existing = repository.create_candidate(
                "兼容项目",
                ["电鸭 RSS"],
                legacy_note,
            )
            sink = EleduckResearchSink(repository)

            resolved = sink.resolve_project(
                candidate_id=None,
                project_name="兼容项目",
                default_name="电鸭 JD 调研：2026-08-03",
                jobs=(job,),
            )

        self.assertEqual(existing["id"], resolved)

    def test_named_run_does_not_reuse_unrelated_same_name_project(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责明确；任职要求明确；全职。",
            )
        )
        job = EleduckRssSource(load_feed=lambda: payload).collect().accepted_jobs[0]
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "research.db")
            existing = repository.create_candidate(
                "同名项目",
                ["电鸭公开职位"],
                ELEDUCK_PROJECT_NOTE,
            )
            sink = EleduckResearchSink(repository)

            resolved = sink.resolve_project(
                candidate_id=None,
                project_name="同名项目",
                default_name="电鸭 JD 调研：2026-08-03",
                jobs=(job,),
            )

        self.assertNotEqual(existing["id"], resolved)

    def test_default_run_does_not_reuse_arbitrary_empty_eleduck_project(self) -> None:
        payload = rss_document(
            rss_item(
                title="招聘 Python 工程师",
                description="岗位职责明确；任职要求明确；全职。",
            )
        )
        job = EleduckRssSource(load_feed=lambda: payload).collect().accepted_jobs[0]
        with TemporaryDirectory() as directory:
            repository = Repository(Path(directory) / "research.db")
            existing = repository.create_candidate(
                "用户自建电鸭项目",
                ["电鸭公开职位"],
                ELEDUCK_PROJECT_NOTE,
            )
            sink = EleduckResearchSink(repository)

            resolved = sink.resolve_project(
                candidate_id=None,
                project_name=None,
                default_name="电鸭 JD 调研：2026-08-03",
                jobs=(job,),
            )

        self.assertNotEqual(existing["id"], resolved)


class EleduckCoordinatorReviewTests(unittest.TestCase):
    def test_review_decision_is_persisted_and_cannot_be_reversed(self) -> None:
        payload = rss_document(
            rss_item(
                title="远程办公随想",
                description="记录今天的一点感受。",
            )
        )
        sink = _FakeEleduckSink()
        with TemporaryDirectory() as directory:
            coordinator = EleduckCoordinator(
                source=EleduckRssSource(load_feed=lambda: payload),
                store=EleduckStateStore(Path(directory) / "state.json"),
                sink=sink,
                evaluator=lambda job: replace(job, score=75.0, qualified=True),
                clock=lambda: datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc),
            )
            coordinator.research(candidate_id=None, project_name=None)
            pending = coordinator.list_reviews().pending[0]

            first = coordinator.decide_review(
                pending.item.identity,
                decision="recruitment",
                candidate_id="candidate-1",
                project_name=None,
            )
            repeated = coordinator.decide_review(
                pending.item.identity,
                decision="recruitment",
                candidate_id="candidate-1",
                project_name=None,
            )
            queue = coordinator.list_reviews()
            with self.assertRaises(EleduckReviewConflict):
                coordinator.decide_review(
                    pending.item.identity,
                    decision="non_job",
                    candidate_id=None,
                    project_name=None,
                )

        self.assertEqual(first, repeated)
        self.assertEqual("job-1", first.job_id)
        self.assertEqual((), queue.pending)
        self.assertEqual((first,), queue.confirmed_recruitment)
        self.assertEqual(1, sink.resolve_calls)
        self.assertEqual(1, sink.persist_calls)

    def test_non_job_decision_never_resolves_project_or_persists_job(self) -> None:
        payload = rss_document(
            rss_item(title="远程办公随想", description="记录今天的一点感受。")
        )
        sink = _FakeEleduckSink()
        with TemporaryDirectory() as directory:
            coordinator = EleduckCoordinator(
                source=EleduckRssSource(load_feed=lambda: payload),
                store=EleduckStateStore(Path(directory) / "state.json"),
                sink=sink,
            )
            coordinator.research(candidate_id=None, project_name=None)
            identity = coordinator.list_reviews().pending[0].item.identity

            decision = coordinator.decide_review(
                identity,
                decision="non_job",
                candidate_id=None,
                project_name=None,
            )
            queue = coordinator.list_reviews()

        self.assertEqual("non_job", decision.decision)
        self.assertEqual((decision,), queue.ignored_non_job)
        self.assertEqual(0, sink.resolve_calls)
        self.assertEqual(0, sink.persist_calls)

    def test_decision_limit_fails_before_any_project_or_job_write(self) -> None:
        payload = rss_document(
            rss_item(title="远程办公随想", description="记录今天的一点感受。")
        )
        source = EleduckRssSource(load_feed=lambda: payload)
        current = source.collect().review_items[0]
        decisions = []
        for index in range(512):
            item = replace(
                current.item,
                guid=f"old-{index}",
                canonical_url=f"https://eleduck.com/posts/old{index:04d}",
            )
            classified = replace(current, item=item)
            decisions.append(
                EleduckReviewDecision(
                    identity=item.identity,
                    decision="non_job",
                    decided_at="2026-08-03T09:00:00+00:00",
                    item=classified,
                )
            )
        sink = _FakeEleduckSink()
        with TemporaryDirectory() as directory:
            store = EleduckStateStore(Path(directory) / "state.json")
            store.save(
                EleduckSourceState(
                    pending_reviews=(current,),
                    decisions=tuple(decisions),
                )
            )
            coordinator = EleduckCoordinator(source=source, store=store, sink=sink)

            with self.assertRaises(EleduckStateError):
                coordinator.decide_review(
                    current.item.identity,
                    decision="recruitment",
                    candidate_id="candidate-1",
                    project_name=None,
                )

        self.assertEqual(0, sink.resolve_calls)
        self.assertEqual(0, sink.persist_calls)


class _FakeEleduckSink:
    def __init__(self) -> None:
        self.resolve_calls = 0
        self.persist_calls = 0
        self.persisted_jobs = []

    def resolve_project(
        self,
        *,
        candidate_id: str | None,
        project_name: str | None,
        default_name: str,
        jobs,
    ) -> str:
        del project_name, default_name, jobs
        self.resolve_calls += 1
        return candidate_id or "candidate-created"

    def persist(self, candidate_id, jobs):
        del candidate_id
        self.persist_calls += 1
        self.persisted_jobs.extend(jobs)
        return tuple(
            PersistedEleduckJob(job_id=f"job-{index + 1}", created=True)
            for index, _job in enumerate(jobs)
        )


class _FailOnceStateStore:
    def __init__(self, delegate: EleduckStateStore) -> None:
        self._delegate = delegate
        self._failed = False

    def load(self):
        return self._delegate.load()

    def save(self, state):
        if not self._failed:
            self._failed = True
            raise EleduckStateError("simulated state save failure")
        self._delegate.save(state)


if __name__ == "__main__":
    unittest.main()
