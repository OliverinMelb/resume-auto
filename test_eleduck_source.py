from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from job_sources.eleduck import (
    EleduckCursor,
    EleduckFeedError,
    EleduckHttpError,
    EleduckRssSource,
    PostCategory,
)
from job_sources.eleduck.rss import FEED_URL, load_public_feed


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


if __name__ == "__main__":
    unittest.main()
