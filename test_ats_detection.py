import json
import socket
import unittest
from dataclasses import asdict
from email.message import Message
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from job_sources.ats import (
    AtsBatchItem,
    AtsBatchReport,
    AtsCluster,
    AtsDetection,
    AtsHttpPolicy,
    cluster_ats_detections,
    detect_ats,
    detect_ats_url,
    detect_ats_urls,
    serialize_ats_batch_report,
)


FIXTURE_DIR = Path(__file__).parent / "job_sources" / "ats" / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8")


class FakeHttpResponse:
    def __init__(
        self,
        url: str,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._url = url
        self._body = body
        self.closed = False
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def geturl(self) -> str:
        return self._url

    def close(self) -> None:
        self.closed = True


class FakeHttpOpener:
    def __init__(self, responses: list[FakeHttpResponse]) -> None:
        self._responses = iter(responses)
        self.requested_urls: list[str] = []

    def open(self, request, timeout: float):
        self.requested_urls.append(request.full_url)
        return next(self._responses)


class BarrierHttpOpener(FakeHttpOpener):
    def __init__(
        self,
        responses: list[FakeHttpResponse],
        barrier: Barrier,
    ) -> None:
        super().__init__(responses)
        self._barrier = barrier

    def open(self, request, timeout: float):
        if not self.requested_urls:
            self._barrier.wait(timeout=5.0)
        return super().open(request, timeout)


def public_dns_result(host: str, port: int, *args, **kwargs):
    return [
        (
            socket.AF_INET,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            "",
            ("93.184.216.34", port),
        )
    ]


class AtsDetectionTests(unittest.TestCase):
    def test_returns_the_required_public_result_shape(self):
        result = detect_ats(
            "https://boards.greenhouse.io/example/jobs/123",
            fixture("greenhouse"),
        )

        self.assertIsInstance(result, AtsDetection)
        self.assertEqual(
            set(asdict(result)),
            {
                "ats",
                "confidence",
                "evidence",
                "version_hint",
                "supports_json_ld",
                "requires_browser",
                "blocked_reason",
            },
        )
        self.assertEqual(result.ats, "greenhouse")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertTrue(
            any(item.startswith("hostname:") for item in result.evidence)
        )
        self.assertEqual(result.version_hint, "job_board")
        self.assertFalse(result.supports_json_ld)
        self.assertFalse(result.requires_browser)
        self.assertIsNone(result.blocked_reason)

    def test_detects_each_vendor_from_fixed_public_signatures(self):
        cases = (
            (
                "greenhouse",
                "https://boards.greenhouse.io/example",
                "greenhouse",
            ),
            ("lever", "https://jobs.lever.co/example", "lever"),
            ("moka", "https://example.mokahr.com/campus", "moka"),
            ("beisen", "https://example.beisen.com/career", "beisen"),
            ("hotjob", "https://example.hotjob.cn/wt/example", "hotjob"),
            (
                "workday",
                "https://example.wd5.myworkdayjobs.com/External",
                "workday",
            ),
            (
                "sap_successfactors",
                "https://career5.successfactors.eu/example",
                "sap_successfactors",
            ),
        )

        for expected, url, fixture_name in cases:
            with self.subTest(ats=expected):
                result = detect_ats(url, fixture(fixture_name))
                self.assertEqual(result.ats, expected)
                self.assertGreaterEqual(result.confidence, 0.9)
                self.assertTrue(result.evidence)

    def test_detects_nested_jobposting_json_ld(self):
        result = detect_ats(
            "https://careers.example.com/jobs/123",
            fixture("json_ld"),
        )

        self.assertEqual(result.ats, "json_ld")
        self.assertEqual(result.confidence, 0.9)
        self.assertEqual(result.evidence, ("json_ld:@type=JobPosting",))
        self.assertTrue(result.supports_json_ld)
        self.assertFalse(result.requires_browser)

    def test_specific_vendor_wins_over_generic_json_ld(self):
        job_posting = json.dumps(
            {"@context": "https://schema.org", "@type": "JobPosting"}
        )
        html = fixture("lever").replace(
            "</head>",
            f'<script type="application/ld+json">{job_posting}</script></head>',
        )

        result = detect_ats("https://jobs.lever.co/example/123", html)

        self.assertEqual(result.ats, "lever")
        self.assertTrue(result.supports_json_ld)
        self.assertIn("json_ld:@type=JobPosting", result.evidence)

    def test_custom_domain_vendor_resource_is_only_low_confidence(self):
        result = detect_ats(
            "https://careers.example.com/jobs",
            fixture("moka"),
        )

        self.assertEqual(result.ats, "moka")
        self.assertLessEqual(result.confidence, 0.65)
        self.assertTrue(
            any(item.startswith("resource_host:") for item in result.evidence)
        )

    def test_beisen_shared_host_requires_corroborating_public_resources(self):
        corroborated = detect_ats(
            "https://example.zhiye.com/jobs",
            fixture("beisen_live"),
        )
        hostname_only = detect_ats(
            "https://example.zhiye.com/jobs",
            "<main>Open positions</main>",
        )

        self.assertEqual(corroborated.ats, "beisen")
        self.assertEqual(corroborated.confidence, 0.75)
        self.assertIn(
            "candidate_hostname:example.zhiye.com",
            corroborated.evidence,
        )
        self.assertIn(
            "resource_host:stc.beisen.com",
            corroborated.evidence,
        )
        self.assertIsNone(corroborated.version_hint)
        self.assertEqual(hostname_only.ats, "unknown")
        self.assertEqual(hostname_only.confidence, 0.0)

    def test_detects_documented_successfactors_tenant_domains(self):
        for url in (
            "https://tenant.jobs.hr.cloud.sap.com/careers",
            "https://tenant.jobs.hr.sapcloud.cn/careers",
        ):
            with self.subTest(url=url):
                result = detect_ats(url, fixture("unknown"))
                self.assertEqual(result.ats, "sap_successfactors")
                self.assertGreaterEqual(result.confidence, 0.9)
                self.assertTrue(
                    any(
                        item.startswith("hostname:")
                        for item in result.evidence
                    )
                )
                self.assertIsNone(result.version_hint)

    def test_reports_browser_need_from_public_dynamic_shell_signatures(self):
        workday = detect_ats(
            "https://example.wd5.myworkdayjobs.com/Careers",
            fixture("workday_live"),
        )
        successfactors = detect_ats(
            "https://jobs.example.com/careers",
            fixture("sap_successfactors_live"),
        )
        moka = detect_ats(
            "https://hire-r1.mokahr.com/careers",
            fixture("moka_live"),
        )

        self.assertEqual(workday.ats, "workday")
        self.assertTrue(workday.requires_browser)
        self.assertIsNone(workday.version_hint)
        self.assertEqual(successfactors.ats, "sap_successfactors")
        self.assertLessEqual(successfactors.confidence, 0.65)
        self.assertTrue(successfactors.requires_browser)
        self.assertIsNone(successfactors.version_hint)
        self.assertEqual(moka.ats, "moka")
        self.assertFalse(moka.requires_browser)
        self.assertIsNone(moka.version_hint)

    def test_dynamic_shell_domains_in_resource_paths_do_not_trigger(self):
        html = """
        <script src="https://evil.example/assets/myworkdaycdn.com/cx-jobs.min.js"></script>
        <script src="https://evil.example/successfactors.com/platform/js/j2w/careers.js"></script>
        <main>Careers</main>
        """

        result = detect_ats("https://careers.example.com", html)

        self.assertEqual(result.ats, "unknown")
        self.assertFalse(result.requires_browser)

    def test_live_host_signatures_do_not_invent_product_versions(self):
        cases = (
            (
                "greenhouse",
                "https://job-boards.greenhouse.io/example",
                "greenhouse_live",
            ),
            (
                "lever",
                "https://jobs.lever.co/example",
                "lever_live",
            ),
            (
                "hotjob",
                "https://example.hotjob.cn/wt/example/web/index",
                "hotjob_live",
            ),
        )

        for expected, url, fixture_name in cases:
            with self.subTest(ats=expected):
                result = detect_ats(url, fixture(fixture_name))
                self.assertEqual(result.ats, expected)
                self.assertGreaterEqual(result.confidence, 0.9)
                self.assertIsNone(result.version_hint)
                self.assertFalse(result.requires_browser)
                self.assertFalse(result.supports_json_ld)

    def test_plain_company_or_product_words_do_not_identify_an_ats(self):
        for text in (
            "<main>Greenhouse team careers</main>",
            "<main>We leverage talent and use Moka coffee.</main>",
            "<main>Workday opportunities at SAP</main>",
        ):
            with self.subTest(text=text):
                result = detect_ats(
                    "https://careers.example.com/jobs",
                    text,
                )
                self.assertEqual(result.ats, "unknown")
                self.assertLess(result.confidence, 0.5)

    def test_conflicting_custom_domain_signatures_return_unknown(self):
        html = """
        <script src="https://static.mokahr.com/career-site/app.js"></script>
        <a href="https://jobs.lever.co/example">Jobs</a>
        """

        result = detect_ats("https://careers.example.com", html)

        self.assertEqual(result.ats, "unknown")
        self.assertLess(result.confidence, 0.5)
        self.assertIn("conflict:moka,lever", result.evidence)
        self.assertIsNone(result.blocked_reason)

    def test_malformed_json_ld_does_not_claim_support(self):
        html = """
        <script type="application/ld+json">
          {"@type": "JobPosting",
        </script>
        """

        result = detect_ats("https://careers.example.com/job", html)

        self.assertEqual(result.ats, "unknown")
        self.assertFalse(result.supports_json_ld)
        self.assertIn("json_ld:malformed", result.evidence)

    def test_reports_access_restrictions_without_bypassing_them(self):
        cases = (
            (
                "<meta name='robots' content='noindex, nofollow'>",
                200,
                None,
                "noindex",
            ),
            (
                "<html><title>Sign in required</title>"
                "<input type='password'></html>",
                200,
                None,
                "login_required",
            ),
            (
                "<html><title>安全验证</title><div>请输入验证码</div></html>",
                200,
                None,
                "captcha_or_verification",
            ),
            ("", 403, None, "http_403"),
            ("", 429, None, "rate_limited"),
            (
                "<main>Jobs</main>",
                200,
                {"X-Robots-Tag": "noindex"},
                "noindex",
            ),
        )

        for html, status, headers, reason in cases:
            with self.subTest(reason=reason):
                result = detect_ats(
                    "https://careers.example.com",
                    html,
                    status_code=status,
                    headers=headers,
                )
                self.assertEqual(result.blocked_reason, reason)
                self.assertFalse(result.requires_browser)

    def test_restricted_custom_domain_does_not_use_body_ats_signatures(self):
        restricted_pages = (
            (
                fixture("moka").replace(
                    "</head>",
                    "<meta name='robots' content='noindex'></head>",
                ),
                "noindex",
            ),
            (
                fixture("lever").replace(
                    "</head>",
                    "<title>Sign in required</title></head>",
                ).replace(
                    "<body>",
                    "<body><input type='password'>",
                ),
                "login_required",
            ),
            (
                fixture("greenhouse").replace(
                    "<body>",
                    "<body><div>CAPTCHA verification required</div>",
                ),
                "captcha_or_verification",
            ),
        )

        for html, reason in restricted_pages:
            with self.subTest(reason=reason):
                result = detect_ats(
                    "https://careers.example.com/jobs",
                    html,
                )
                self.assertEqual(result.ats, "unknown")
                self.assertEqual(result.confidence, 0.0)
                self.assertEqual(result.evidence, ())
                self.assertFalse(result.supports_json_ld)
                self.assertFalse(result.requires_browser)
                self.assertEqual(result.blocked_reason, reason)

    def test_vendor_hostname_can_be_reported_even_when_access_is_blocked(self):
        result = detect_ats(
            "https://jobs.lever.co/example",
            fixture("lever"),
            status_code=403,
        )

        self.assertEqual(result.ats, "lever")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(result.evidence, ("hostname:jobs.lever.co",))
        self.assertIsNone(result.version_hint)
        self.assertFalse(result.supports_json_ld)
        self.assertEqual(result.blocked_reason, "http_403")

    def test_explicit_javascript_shell_reports_browser_requirement_only(self):
        html = """
        <html><body>
          <div id="root"></div>
          <noscript>Please enable JavaScript to view jobs.</noscript>
          <script src="/assets/app.js"></script>
        </body></html>
        """

        result = detect_ats("https://careers.example.com/jobs", html)

        self.assertEqual(result.ats, "unknown")
        self.assertTrue(result.requires_browser)
        self.assertIsNone(result.blocked_reason)

    def test_generic_or_invalid_inputs_return_safe_unknown(self):
        generic = detect_ats(
            "https://careers.example.com",
            fixture("unknown"),
        )
        invalid = detect_ats("file:///tmp/jobs.html", fixture("greenhouse"))

        self.assertEqual(generic.ats, "unknown")
        self.assertEqual(generic.confidence, 0.0)
        self.assertEqual(generic.evidence, ())
        self.assertIsNone(generic.blocked_reason)
        self.assertEqual(invalid.ats, "unknown")
        self.assertEqual(invalid.blocked_reason, "invalid_url")


class AtsHttpDetectionTests(unittest.TestCase):
    def run_with_http(
        self,
        responses: list[FakeHttpResponse],
        url: str,
        *,
        policy: AtsHttpPolicy = AtsHttpPolicy(),
    ) -> tuple[AtsDetection, FakeHttpOpener]:
        opener = FakeHttpOpener(responses)
        with (
            patch(
                "job_sources.ats.http.build_opener",
                return_value=opener,
            ),
            patch(
                "job_sources.ats.http.socket.getaddrinfo",
                side_effect=public_dns_result,
            ),
        ):
            result = detect_ats_url(url, policy=policy)
        return result, opener

    def test_fetches_only_robots_and_approved_page_before_detection(self):
        url = "https://boards.greenhouse.io/example"
        robots = FakeHttpResponse(
            "https://boards.greenhouse.io/robots.txt",
            404,
        )
        page = FakeHttpResponse(
            url,
            200,
            fixture("greenhouse").encode(),
            {"Content-Type": "text/html; charset=utf-8"},
        )
        result, opener = self.run_with_http(
            [robots, page],
            url,
        )

        self.assertEqual(result.ats, "greenhouse")
        self.assertIsNone(result.blocked_reason)
        self.assertEqual(
            opener.requested_urls,
            [
                "https://boards.greenhouse.io/robots.txt",
                url,
            ],
        )
        self.assertTrue(robots.closed)
        self.assertTrue(page.closed)

    def test_stops_when_robots_disallows_the_approved_path(self):
        url = "https://careers.example.com/jobs"
        result, opener = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://careers.example.com/robots.txt",
                    200,
                    b"User-agent: *\nDisallow: /jobs\n",
                    {"Content-Type": "text/plain"},
                )
            ],
            url,
        )

        self.assertEqual(result.ats, "unknown")
        self.assertEqual(result.blocked_reason, "robots_disallowed")
        self.assertEqual(
            opener.requested_urls,
            ["https://careers.example.com/robots.txt"],
        )

    def test_maps_robots_transport_failure_without_fetching_target(self):
        url = "https://careers.example.com/jobs"
        opener = FakeHttpOpener([])
        with (
            patch(
                "job_sources.ats.http.build_opener",
                return_value=opener,
            ),
            patch(
                "job_sources.ats.http.socket.getaddrinfo",
                side_effect=public_dns_result,
            ),
            patch.object(opener, "open", side_effect=TimeoutError),
        ):
            result = detect_ats_url(url)

        self.assertEqual(result.blocked_reason, "robots_unavailable")

    def test_rejects_private_addresses_before_building_an_opener(self):
        private_dns = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with (
            patch(
                "job_sources.ats.http.socket.getaddrinfo",
                return_value=private_dns,
            ),
            patch("job_sources.ats.http.build_opener") as build,
        ):
            result = detect_ats_url("https://careers.example.com/jobs")

        self.assertEqual(result.blocked_reason, "unsafe_host")
        build.assert_not_called()

    def test_pinned_https_connection_uses_validated_ip_and_original_sni(self):
        from job_sources.ats.http import _PinnedHTTPSConnection

        raw_socket = Mock()
        tls_socket = Mock()
        context = Mock()
        context.wrap_socket.return_value = tls_socket
        with (
            patch(
                "job_sources.ats.http.socket.create_connection",
                return_value=raw_socket,
            ) as create_connection,
            patch(
                "job_sources.ats.http.socket.getaddrinfo"
            ) as resolve_again,
        ):
            connection = _PinnedHTTPSConnection(
                "careers.example.com",
                443,
                "93.184.216.34",
                timeout=10.0,
                context=context,
            )
            connection.connect()

        create_connection.assert_called_once_with(
            ("93.184.216.34", 443),
            10.0,
            None,
        )
        context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="careers.example.com",
        )
        self.assertIs(connection.sock, tls_socket)
        resolve_again.assert_not_called()

    def test_rejects_cross_host_redirect_without_following_it(self):
        url = "https://careers.example.com/jobs"
        result, opener = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://careers.example.com/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    url,
                    302,
                    headers={"Location": "https://other.example/jobs"},
                ),
            ],
            url,
        )

        self.assertEqual(result.blocked_reason, "unsafe_redirect")
        self.assertEqual(
            opener.requested_urls,
            [
                "https://careers.example.com/robots.txt",
                url,
            ],
        )

    def test_follows_bounded_same_host_redirects(self):
        url = "https://jobs.lever.co/example"
        redirected = "https://jobs.lever.co/example/jobs"
        result, opener = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://jobs.lever.co/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    url,
                    302,
                    headers={"Location": "/example/jobs"},
                ),
                FakeHttpResponse(
                    redirected,
                    200,
                    fixture("lever").encode(),
                    {"Content-Type": "text/html; charset=utf-8"},
                ),
            ],
            url,
        )

        self.assertEqual(result.ats, "lever")
        self.assertIsNone(result.blocked_reason)
        self.assertEqual(opener.requested_urls[-1], redirected)

    def test_checks_robots_again_before_following_a_page_redirect(self):
        url = "https://careers.example.com/public"
        redirected = "https://careers.example.com/private"
        result, opener = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://careers.example.com/robots.txt",
                    200,
                    b"User-agent: *\nDisallow: /private\n",
                    {"Content-Type": "text/plain"},
                ),
                FakeHttpResponse(
                    url,
                    302,
                    headers={"Location": "/private"},
                ),
            ],
            url,
        )

        self.assertEqual(result.blocked_reason, "robots_disallowed")
        self.assertNotIn(redirected, opener.requested_urls)

    def test_rejects_https_downgrade_redirect(self):
        url = "https://careers.example.com/jobs"
        result, opener = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://careers.example.com/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    url,
                    302,
                    headers={"Location": "http://careers.example.com/jobs"},
                ),
            ],
            url,
        )

        self.assertEqual(result.blocked_reason, "unsafe_redirect")
        self.assertNotIn(
            "http://careers.example.com/jobs",
            opener.requested_urls,
        )

    def test_stops_after_the_redirect_limit(self):
        url = "https://careers.example.com/jobs"
        result, opener = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://careers.example.com/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    url,
                    302,
                    headers={"Location": "/jobs/one"},
                ),
                FakeHttpResponse(
                    "https://careers.example.com/jobs/one",
                    302,
                    headers={"Location": "/jobs/two"},
                ),
            ],
            url,
            policy=AtsHttpPolicy(max_redirects=1),
        )

        self.assertEqual(result.blocked_reason, "too_many_redirects")
        self.assertNotIn(
            "https://careers.example.com/jobs/two",
            opener.requested_urls,
        )

    def test_rejects_non_html_and_oversized_target_responses(self):
        url = "https://careers.example.com/jobs"
        cases = (
            (
                FakeHttpResponse(
                    url,
                    200,
                    b"{}",
                    {"Content-Type": "application/json"},
                ),
                AtsHttpPolicy(),
                "unsupported_content_type",
            ),
            (
                FakeHttpResponse(
                    url,
                    200,
                    b"12345",
                    {"Content-Type": "text/html"},
                ),
                AtsHttpPolicy(max_html_bytes=4),
                "response_too_large",
            ),
        )

        for page, policy, reason in cases:
            with self.subTest(reason=reason):
                result, _ = self.run_with_http(
                    [
                        FakeHttpResponse(
                            "https://careers.example.com/robots.txt",
                            404,
                        ),
                        page,
                    ],
                    url,
                    policy=policy,
                )
                self.assertEqual(result.blocked_reason, reason)

    def test_maps_unsuccessful_target_status_without_parsing_the_body(self):
        url = "https://careers.example.com/jobs"
        cases = (
            (503, "http_503"),
            (429, "rate_limited"),
        )

        for status, reason in cases:
            with self.subTest(status=status):
                result, _ = self.run_with_http(
                    [
                        FakeHttpResponse(
                            "https://careers.example.com/robots.txt",
                            404,
                        ),
                        FakeHttpResponse(
                            url,
                            status,
                            fixture("moka").encode(),
                            {"Content-Type": "text/html"},
                        ),
                    ],
                    url,
                )
                self.assertEqual(result.ats, "unknown")
                self.assertEqual(result.blocked_reason, reason)
                self.assertEqual(result.evidence, ())

    def test_passes_noindex_response_header_to_the_pure_detector(self):
        url = "https://careers.example.com/jobs"
        result, _ = self.run_with_http(
            [
                FakeHttpResponse(
                    "https://careers.example.com/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    url,
                    200,
                    fixture("moka").encode(),
                    {
                        "Content-Type": "text/html",
                        "X-Robots-Tag": "noindex",
                    },
                ),
            ],
            url,
        )

        self.assertEqual(result.ats, "unknown")
        self.assertEqual(result.blocked_reason, "noindex")
        self.assertEqual(result.evidence, ())


class AtsBatchDetectionTests(unittest.TestCase):
    def test_returns_ordered_detections_and_review_clusters(self):
        greenhouse_url = "https://boards.greenhouse.io/example"
        lever_url = "https://jobs.lever.co/example"
        greenhouse_opener = FakeHttpOpener(
            [
                FakeHttpResponse(
                    "https://boards.greenhouse.io/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    greenhouse_url,
                    200,
                    fixture("greenhouse").encode(),
                    {"Content-Type": "text/html; charset=utf-8"},
                ),
            ]
        )
        lever_opener = FakeHttpOpener(
            [
                FakeHttpResponse(
                    "https://jobs.lever.co/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    lever_url,
                    200,
                    fixture("lever").encode(),
                    {"Content-Type": "text/html; charset=utf-8"},
                ),
            ]
        )

        with (
            patch(
                "job_sources.ats.http.build_opener",
                side_effect=[greenhouse_opener, lever_opener],
            ),
            patch(
                "job_sources.ats.http.socket.getaddrinfo",
                side_effect=public_dns_result,
            ),
        ):
            report = detect_ats_urls(
                [greenhouse_url, lever_url],
                max_workers=1,
            )

        self.assertIsInstance(report, AtsBatchReport)
        self.assertTrue(
            all(isinstance(item, AtsBatchItem) for item in report.items)
        )
        self.assertEqual(
            tuple(item.member_id for item in report.items),
            ("item-000001", "item-000002"),
        )
        self.assertEqual(
            tuple(item.detection.ats for item in report.items),
            ("greenhouse", "lever"),
        )
        self.assertEqual(
            tuple(cluster.ats for cluster in report.clusters),
            ("greenhouse", "lever"),
        )
        self.assertEqual(
            tuple(cluster.members for cluster in report.clusters),
            (("item-000001",), ("item-000002",)),
        )

    def test_detects_a_duplicate_url_once_but_keeps_each_input_member(self):
        url = "https://jobs.lever.co/example"
        opener = FakeHttpOpener(
            [
                FakeHttpResponse(
                    "https://jobs.lever.co/robots.txt",
                    404,
                ),
                FakeHttpResponse(
                    url,
                    200,
                    fixture("lever").encode(),
                    {"Content-Type": "text/html; charset=utf-8"},
                ),
            ]
        )

        with (
            patch(
                "job_sources.ats.http.build_opener",
                return_value=opener,
            ) as build_opener,
            patch(
                "job_sources.ats.http.socket.getaddrinfo",
                side_effect=public_dns_result,
            ),
        ):
            report = detect_ats_urls([url, url], max_workers=1)

        self.assertEqual(
            tuple(item.member_id for item in report.items),
            ("item-000001", "item-000002"),
        )
        self.assertEqual(
            tuple(item.detection.ats for item in report.items),
            ("lever", "lever"),
        )
        self.assertEqual(
            report.clusters[0].members,
            ("item-000001", "item-000002"),
        )
        build_opener.assert_called_once()

    def test_rejects_unbounded_batch_settings_before_http(self):
        cases = (
            ([], 0, "max_workers"),
            ([], 9, "max_workers"),
            (["file:///jobs"] * 101, 1, "at most 100"),
        )

        for urls, max_workers, message in cases:
            with self.subTest(message=message):
                with patch(
                    "job_sources.ats.http.build_opener"
                ) as build_opener:
                    with self.assertRaisesRegex(ValueError, message):
                        detect_ats_urls(
                            urls,
                            max_workers=max_workers,
                        )
                build_opener.assert_not_called()

    def test_stops_consuming_an_iterable_at_the_batch_limit(self):
        def too_many_urls():
            for index in range(101):
                yield f"file:///jobs/{index}"
            raise AssertionError("batch consumed beyond its documented limit")

        with patch(
            "job_sources.ats.http.build_opener"
        ) as build_opener:
            with self.assertRaisesRegex(ValueError, "at most 100"):
                detect_ats_urls(
                    too_many_urls(),
                    max_workers=1,
                )
        build_opener.assert_not_called()

    def test_runs_unique_urls_concurrently_but_keeps_input_order(self):
        greenhouse_url = "https://boards.greenhouse.io/example"
        lever_url = "https://jobs.lever.co/example"
        barrier = Barrier(2)
        openers = {
            "boards.greenhouse.io": BarrierHttpOpener(
                [
                    FakeHttpResponse(
                        "https://boards.greenhouse.io/robots.txt",
                        404,
                    ),
                    FakeHttpResponse(
                        greenhouse_url,
                        200,
                        fixture("greenhouse").encode(),
                        {"Content-Type": "text/html; charset=utf-8"},
                    ),
                ],
                barrier,
            ),
            "jobs.lever.co": BarrierHttpOpener(
                [
                    FakeHttpResponse(
                        "https://jobs.lever.co/robots.txt",
                        404,
                    ),
                    FakeHttpResponse(
                        lever_url,
                        200,
                        fixture("lever").encode(),
                        {"Content-Type": "text/html; charset=utf-8"},
                    ),
                ],
                barrier,
            ),
        }

        with (
            patch(
                "job_sources.ats.http.build_opener",
                side_effect=lambda approved: openers[approved.hostname],
            ),
            patch(
                "job_sources.ats.http.socket.getaddrinfo",
                side_effect=public_dns_result,
            ),
        ):
            report = detect_ats_urls(
                [greenhouse_url, lever_url],
                max_workers=2,
            )

        self.assertEqual(
            tuple(item.detection.ats for item in report.items),
            ("greenhouse", "lever"),
        )

    def test_serializes_a_deterministic_json_report_without_urls(self):
        detection = detect_ats(
            "https://jobs.lever.co/example?access_token=secret",
            fixture("lever"),
        )
        report = AtsBatchReport(
            items=(
                AtsBatchItem(
                    member_id="item-000001",
                    detection=detection,
                ),
            ),
            clusters=cluster_ats_detections(
                {"item-000001": detection}
            ),
        )

        serialized = serialize_ats_batch_report(report, format="json")
        payload = json.loads(serialized)

        self.assertEqual(
            set(payload),
            {"items", "clusters"},
        )
        self.assertEqual(
            payload["items"][0]["member_id"],
            "item-000001",
        )
        self.assertEqual(
            payload["items"][0]["detection"]["ats"],
            "lever",
        )
        self.assertEqual(
            payload["clusters"][0]["members"],
            ["item-000001"],
        )
        self.assertNotIn("/example", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("secret", serialized)
        self.assertEqual(
            serialized,
            serialize_ats_batch_report(report, format="json"),
        )

    def test_serializes_jsonl_as_detection_then_cluster_records(self):
        detection = detect_ats(
            "https://boards.greenhouse.io/example",
            fixture("greenhouse"),
        )
        report = AtsBatchReport(
            items=(
                AtsBatchItem(
                    member_id="item-000001",
                    detection=detection,
                ),
            ),
            clusters=cluster_ats_detections(
                {"item-000001": detection}
            ),
        )

        lines = serialize_ats_batch_report(
            report,
            format="jsonl",
        ).splitlines()
        records = tuple(json.loads(line) for line in lines)

        self.assertEqual(len(records), 2)
        self.assertEqual(
            tuple(record["record_type"] for record in records),
            ("detection", "cluster"),
        )
        self.assertEqual(
            records[0]["payload"]["member_id"],
            "item-000001",
        )
        self.assertEqual(
            records[0]["payload"]["detection"]["ats"],
            "greenhouse",
        )
        self.assertEqual(
            records[1]["payload"]["members"],
            ["item-000001"],
        )


class AtsClusteringTests(unittest.TestCase):
    def test_separates_confirmed_and_candidate_results_for_the_same_ats(self):
        clusters = cluster_ats_detections(
            {
                "vendor-host": detect_ats(
                    "https://example.mokahr.com/jobs",
                    fixture("moka"),
                ),
                "custom-domain": detect_ats(
                    "https://careers.example.com/jobs",
                    fixture("moka"),
                ),
            }
        )

        self.assertEqual(len(clusters), 2)
        self.assertTrue(all(isinstance(item, AtsCluster) for item in clusters))
        confirmed, candidate = clusters
        self.assertEqual(confirmed.review_status, "confirmed")
        self.assertEqual(confirmed.ats, "moka")
        self.assertEqual(confirmed.version_hint, "career_site")
        self.assertEqual(confirmed.members, ("vendor-host",))
        self.assertEqual(confirmed.min_confidence, 0.97)
        self.assertEqual(confirmed.max_confidence, 0.97)
        self.assertEqual(candidate.review_status, "candidate")
        self.assertEqual(candidate.ats, "moka")
        self.assertEqual(candidate.members, ("custom-domain",))
        self.assertEqual(candidate.min_confidence, 0.65)
        self.assertEqual(candidate.max_confidence, 0.65)

    def test_keeps_unknown_and_blocked_results_in_separate_review_queues(self):
        javascript_shell = """
        <div id="root"></div>
        <noscript>Please enable JavaScript to view jobs.</noscript>
        <script src="/assets/app.js"></script>
        """
        clusters = cluster_ats_detections(
            {
                "dynamic-unknown": detect_ats(
                    "https://careers.example.com/jobs",
                    javascript_shell,
                ),
                "blocked-lever": detect_ats(
                    "https://jobs.lever.co/example",
                    "",
                    status_code=403,
                ),
            }
        )

        unknown, blocked = clusters
        self.assertEqual(unknown.review_status, "unknown")
        self.assertEqual(unknown.ats, "unknown")
        self.assertEqual(unknown.members, ("dynamic-unknown",))
        self.assertTrue(unknown.requires_browser)
        self.assertEqual(unknown.blocked_reasons, ())
        self.assertEqual(blocked.review_status, "blocked")
        self.assertEqual(blocked.ats, "lever")
        self.assertEqual(blocked.members, ("blocked-lever",))
        self.assertFalse(blocked.requires_browser)
        self.assertEqual(blocked.blocked_reasons, ("http_403",))

    def test_aggregates_deterministically_and_keeps_versions_separate(self):
        first = AtsDetection(
            ats="moka",
            confidence=0.55,
            evidence=("resource_host:static.mokahr.com",),
            version_hint="career_site",
            supports_json_ld=False,
            requires_browser=False,
            blocked_reason=None,
        )
        second = AtsDetection(
            ats="moka",
            confidence=0.65,
            evidence=(
                "generator:moka career site",
                "resource_host:static.mokahr.com",
            ),
            version_hint="career_site",
            supports_json_ld=True,
            requires_browser=True,
            blocked_reason=None,
        )
        other_version = AtsDetection(
            ats="moka",
            confidence=0.6,
            evidence=("generator:moka legacy portal",),
            version_hint="legacy",
            supports_json_ld=False,
            requires_browser=False,
            blocked_reason=None,
        )

        forward = cluster_ats_detections(
            {
                "z-member": second,
                "a-member": first,
                "legacy-member": other_version,
            }
        )
        reverse = cluster_ats_detections(
            {
                "legacy-member": other_version,
                "a-member": first,
                "z-member": second,
            }
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(len(forward), 2)
        current, legacy = forward
        self.assertEqual(current.version_hint, "career_site")
        self.assertEqual(current.members, ("a-member", "z-member"))
        self.assertEqual(current.min_confidence, 0.55)
        self.assertEqual(current.max_confidence, 0.65)
        self.assertEqual(
            current.evidence,
            (
                "generator:moka career site",
                "resource_host:static.mokahr.com",
            ),
        )
        self.assertTrue(current.supports_json_ld)
        self.assertTrue(current.requires_browser)
        self.assertEqual(legacy.version_hint, "legacy")
        self.assertEqual(legacy.members, ("legacy-member",))
        self.assertEqual(cluster_ats_detections({}), ())

    def test_cluster_order_is_stable_for_none_and_empty_version_hints(self):
        no_version = AtsDetection(
            ats="moka",
            confidence=0.6,
            evidence=(),
            version_hint=None,
            supports_json_ld=False,
            requires_browser=False,
            blocked_reason=None,
        )
        empty_version = AtsDetection(
            ats="moka",
            confidence=0.6,
            evidence=(),
            version_hint="",
            supports_json_ld=False,
            requires_browser=False,
            blocked_reason=None,
        )

        forward = cluster_ats_detections(
            {"no-version": no_version, "empty-version": empty_version}
        )
        reverse = cluster_ats_detections(
            {"empty-version": empty_version, "no-version": no_version}
        )

        self.assertEqual(forward, reverse)
        self.assertEqual(
            {cluster.version_hint for cluster in forward},
            {None, ""},
        )


if __name__ == "__main__":
    unittest.main()
