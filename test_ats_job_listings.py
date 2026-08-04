import json
import unittest
from dataclasses import replace
from unittest.mock import patch


class FakeHttpResponse:
    def __init__(
        self,
        body,
        *,
        url,
        content_type="application/json",
        status=200,
    ):
        self._body = body
        self._offset = 0
        self._url = url
        self.status = status
        self.headers = self.Headers(content_type)

    class Headers:
        def __init__(self, content_type):
            self._content_type = content_type

        def get_content_type(self):
            return self._content_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self):
        return self._url

    def read(self, amount=-1):
        if self._offset >= len(self._body):
            return b""
        if amount < 0:
            amount = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, **kwargs):
        self.calls.append((request, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class AtsJobListingTests(unittest.TestCase):
    def test_collects_and_normalizes_beisen_public_jobs(self):
        from job_sources.ats.listings import collect_ats_jobs

        endpoint = (
            "https://beisen.zhiye.com/api/Jobad/GetJobAdPageList"
        )
        payload = {
            "Code": 200,
            "Count": 1,
            "Data": [
                {
                    "JobAdId": 430044249,
                    "JobAdName": "Python 开发实习生",
                    "Duty": "<p>负责平台服务</p>",
                    "Require": "<p>熟悉 Python</p>",
                    "Org": "平台研发部",
                    "Category": "实习生招聘",
                    "CategoryId": "3",
                    "LocNames": ["北京市", "北京市", "上海市"],
                    "Salary": "200-300 元/天",
                    "Kind": "实习",
                    "HeadCount": "2",
                    "Station": ["研发", "平台", "研发"],
                    "PostDate": "2026-08-01T08:30:00",
                    "EndTime": "2222-02-02T00:00:00",
                    "Degree": "本科",
                    "YearsOfWorking": "不限",
                    "DetailAddress": "海淀区",
                    "ChangeDate": "2026-08-02T09:00:00",
                }
            ],
        }
        response = FakeHttpResponse(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            url=endpoint,
        )
        opener = FakeOpener(response=response)

        with patch(
            "job_sources.ats.listings.beisen.build_opener",
            return_value=opener,
        ):
            result = collect_ats_jobs(
                "https://beisen.zhiye.com/jobs",
                company="北森",
                score=False,
            )

        self.assertEqual(result.ats, "beisen")
        self.assertEqual(result.discovered_count, 1)
        self.assertEqual(result.scanned_count, 1)
        self.assertEqual(result.matched_count, 1)
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "upstream_end")
        self.assertEqual(len(result.jobs), 1)
        job = result.jobs[0]
        self.assertEqual(job.source, "beisen")
        self.assertEqual(job.source_job_id, "beisen.zhiye.com:430044249")
        self.assertEqual(job.title, "Python 开发实习生")
        self.assertEqual(job.company, "北森")
        self.assertEqual(job.locations, ("北京市", "上海市"))
        self.assertEqual(job.recruitment_channel, "internship")
        self.assertEqual(job.employment_type, "实习")
        self.assertEqual(job.description, "负责平台服务\n\n熟悉 Python")
        self.assertEqual(job.responsibilities, ("负责平台服务",))
        self.assertEqual(job.requirements, ("熟悉 Python",))
        self.assertEqual(job.salary_text, "200-300 元/天")
        self.assertEqual(job.published_at, "2026-08-01T08:30:00")
        self.assertIsNone(job.expires_at)
        self.assertEqual(
            job.source_url,
            "https://beisen.zhiye.com/intern/detail?jobAdId=430044249",
        )
        self.assertEqual(job.official_apply_url, job.source_url)
        self.assertEqual(job.upstream_platform, "beisen")
        self.assertEqual(
            job.raw_data,
            {
                "organization": "平台研发部",
                "category": "实习生招聘",
                "category_id": "3",
                "head_count": "2",
                "station": ("研发", "平台"),
                "degree": "本科",
                "years_of_working": "不限",
                "detail_address": "海淀区",
                "change_date": "2026-08-02T09:00:00",
            },
        )

        request, open_kwargs = opener.calls[0]
        self.assertEqual(request.full_url, endpoint)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(open_kwargs, {"timeout": 10.0})
        headers = {key.casefold(): value for key, value in request.header_items()}
        self.assertEqual(headers["accept"], "application/json")
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(headers["x-requested-with"], "xmlhttprequest")
        self.assertEqual(headers["langtype"], "zh_CN")
        request_payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request_payload["PageIndex"], 0)
        self.assertEqual(request_payload["PageSize"], 50)
        self.assertEqual(request_payload["KeyWords"], "")
        self.assertEqual(request_payload["SpecialType"], 0)
        self.assertIn("Salary", request_payload["DisplayFields"])

    def test_collects_and_normalizes_greenhouse_public_jobs(self):
        from job_sources.ats.listings import collect_ats_jobs

        payload = {
            "jobs": [
                {
                    "id": 123,
                    "internal_job_id": 456,
                    "title": "Senior Python Engineer",
                    "updated_at": "2026-08-01T12:00:00Z",
                    "location": {"name": "Remote"},
                    "absolute_url": (
                        "https://job-boards.greenhouse.io/wikimedia/jobs/123"
                    ),
                    "content": "&lt;p&gt;Build &amp;amp; ship.&lt;/p&gt;",
                    "departments": [{"id": 7, "name": "Engineering"}],
                    "offices": [{"id": 8, "name": "Distributed"}],
                }
            ],
            "meta": {"total": 1},
        }

        with patch(
            "job_sources.ats.listings._request_json",
            return_value=payload,
        ) as request_json:
            result = collect_ats_jobs(
                "https://job-boards.greenhouse.io/wikimedia",
                company="Wikimedia Foundation",
                score=False,
            )

        self.assertEqual(result.ats, "greenhouse")
        self.assertEqual(result.discovered_count, 1)
        self.assertEqual(result.scanned_count, 1)
        self.assertEqual(result.matched_count, 1)
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "upstream_end")
        self.assertEqual(len(result.jobs), 1)
        job = result.jobs[0]
        self.assertEqual(job.source, "greenhouse")
        self.assertEqual(job.source_job_id, "wikimedia:123")
        self.assertEqual(job.title, "Senior Python Engineer")
        self.assertEqual(job.company, "Wikimedia Foundation")
        self.assertEqual(job.locations, ("Remote",))
        self.assertEqual(job.recruitment_channel, "social")
        self.assertIsNone(job.employment_type)
        self.assertEqual(job.work_mode, "remote")
        self.assertEqual(job.description, "Build & ship.")
        self.assertEqual(
            job.source_url,
            "https://job-boards.greenhouse.io/wikimedia/jobs/123",
        )
        self.assertEqual(job.official_apply_url, job.source_url)
        self.assertEqual(job.upstream_platform, "greenhouse")
        self.assertIsNone(job.score)
        self.assertIsNone(job.qualified)
        self.assertEqual(
            job.raw_data,
            {
                "internal_job_id": 456,
                "updated_at": "2026-08-01T12:00:00Z",
                "departments": ("Engineering",),
                "offices": ("Distributed",),
            },
        )
        request_json.assert_called_once_with(
            "https://boards-api.greenhouse.io/v1/boards/"
            "wikimedia/jobs?content=true"
        )

    def test_collects_and_normalizes_lever_public_jobs(self):
        from job_sources.ats.listings import collect_ats_jobs

        payload = [
            {
                "id": "posting-abc",
                "text": "Data Science Intern",
                "categories": {
                    "location": "Remote - US",
                    "allLocations": ["Remote - US", "New York, NY"],
                    "commitment": "Internship",
                    "team": "Data",
                    "department": "Engineering",
                },
                "country": "US",
                "createdAt": 1_700_000_000_000,
                "descriptionPlain": "Build production ML systems.",
                "lists": [
                    {
                        "text": "Requirements",
                        "content": "<li>Python</li><li>SQL</li>",
                    }
                ],
                "hostedUrl": "https://jobs.lever.co/dnb/posting-abc",
                "applyUrl": (
                    "https://jobs.lever.co/dnb/posting-abc/apply"
                ),
                "workplaceType": "hybrid",
                "salaryRange": {
                    "currency": "USD",
                    "interval": "year",
                    "min": 100000,
                    "max": 120000,
                },
            }
        ]

        with patch(
            "job_sources.ats.listings._request_json",
            return_value=payload,
        ) as request_json:
            result = collect_ats_jobs(
                "https://jobs.lever.co/dnb",
                company="Dun & Bradstreet",
                score=False,
            )

        self.assertEqual(result.ats, "lever")
        self.assertEqual(result.discovered_count, 1)
        self.assertEqual(result.scanned_count, 1)
        self.assertEqual(result.matched_count, 1)
        self.assertTrue(result.complete)
        job = result.jobs[0]
        self.assertEqual(job.source, "lever")
        self.assertEqual(job.source_job_id, "dnb:posting-abc")
        self.assertEqual(job.title, "Data Science Intern")
        self.assertEqual(job.company, "Dun & Bradstreet")
        self.assertEqual(job.locations, ("Remote - US", "New York, NY"))
        self.assertEqual(job.recruitment_channel, "internship")
        self.assertEqual(job.employment_type, "internship")
        self.assertEqual(job.work_mode, "hybrid")
        self.assertEqual(job.published_at, "2023-11-14T22:13:20+00:00")
        self.assertEqual(job.description, "Build production ML systems.")
        self.assertEqual(job.responsibilities, ())
        self.assertEqual(job.requirements, ("Python SQL",))
        self.assertEqual(job.salary_text, "USD 100000–120000 per year")
        self.assertEqual(
            job.source_url,
            "https://jobs.lever.co/dnb/posting-abc",
        )
        self.assertEqual(
            job.official_apply_url,
            "https://jobs.lever.co/dnb/posting-abc/apply",
        )
        self.assertEqual(job.upstream_platform, "lever")
        self.assertEqual(
            job.raw_data,
            {
                "categories": {
                    "commitment": "Internship",
                    "department": "Engineering",
                    "team": "Data",
                },
                "country": "US",
                "workplace_type": "hybrid",
            },
        )
        request_json.assert_called_once_with(
            "https://api.lever.co/v0/postings/"
            "dnb?mode=json&skip=0&limit=50"
        )

    def test_rejects_unsupported_urls_and_invalid_collection_inputs(self):
        from job_sources.ats.listings import (
            InvalidJobCollectionRequest,
            UnsupportedJobBoardError,
            collect_ats_jobs,
        )

        unsupported_urls = (
            "http://jobs.lever.co/example",
            "https://user@jobs.lever.co/example",
            "https://jobs.lever.co:444/example",
            "https://jobs.lever.co.attacker.example/example",
            "https://jobs.lever.co/",
            "https://jobs.lever.co/not%20safe",
            "http://beisen.zhiye.com/jobs",
            "https://user@beisen.zhiye.com/jobs",
            "https://beisen.zhiye.com:444/jobs",
            "https://zhiye.com/jobs",
            "https://zhiye.com.attacker.example/jobs",
            "https://future.jereh.com/form",
            "https://bad_label.zhiye.com/jobs",
        )
        for url in unsupported_urls:
            with self.subTest(url=url):
                with self.assertRaisesRegex(
                    UnsupportedJobBoardError,
                    "unsupported_job_board",
                ) as raised:
                    collect_ats_jobs(url, score=False)
                self.assertNotIn(url, str(raised.exception))

        invalid_options = (
            {"limit": 0},
            {"limit": 51},
            {"limit": True},
            {"keyword": "k" * 101},
            {"company": "c" * 201},
        )
        for options in invalid_options:
            with self.subTest(option=tuple(options)):
                with self.assertRaisesRegex(
                    InvalidJobCollectionRequest,
                    "invalid_request",
                ):
                    collect_ats_jobs(
                        "https://jobs.lever.co/example",
                        score=False,
                        **options,
                    )

    def test_filters_deduplicates_and_skips_bad_records_before_limit(self):
        from job_sources.ats.listings import collect_ats_jobs

        def greenhouse_job(job_id, title, description):
            return {
                "id": job_id,
                "title": title,
                "location": {"name": "Berlin"},
                "absolute_url": (
                    f"https://job-boards.greenhouse.io/example/jobs/{job_id}"
                ),
                "content": description,
            }

        metadata_only_job = greenhouse_job(
            2,
            "Frontend Engineer",
            "TypeScript",
        )
        metadata_only_job["departments"] = [{"name": "Python Programs"}]

        payload = {
            "jobs": [
                greenhouse_job(1, "Python Engineer", "Backend systems"),
                greenhouse_job(1, "Duplicate Python Engineer", "Duplicate"),
                {"id": 99, "content": "Python but missing required fields"},
                metadata_only_job,
                greenhouse_job(3, "Platform Engineer", "Python infrastructure"),
            ],
            "meta": {"total": 5},
        }

        with patch(
            "job_sources.ats.listings._request_json",
            return_value=payload,
        ):
            result = collect_ats_jobs(
                "https://job-boards.greenhouse.io/example",
                keyword=" python ",
                limit=1,
                score=False,
            )

        self.assertEqual(result.discovered_count, 5)
        self.assertEqual(result.scanned_count, 5)
        self.assertEqual(result.matched_count, 3)
        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(result.jobs[0].source_job_id, "example:1")

    def test_skips_malformed_records_and_rejects_changed_payload_shapes(self):
        from job_sources.ats.listings import (
            JobListingFetchError,
            collect_ats_jobs,
        )

        valid_job = {
            "id": 1,
            "title": "Backend Engineer",
            "location": {"name": "Remote"},
            "absolute_url": "https://boards.greenhouse.io/example/jobs/1",
            "content": "Build APIs",
        }
        with patch(
            "job_sources.ats.listings._request_json",
            return_value={"jobs": [None, valid_job], "meta": {"total": 2}},
        ):
            result = collect_ats_jobs(
                "https://boards.greenhouse.io/example",
                score=False,
            )
        self.assertEqual(tuple(job.title for job in result.jobs), ("Backend Engineer",))

        valid_lever_job = {
            "id": "valid-1",
            "text": "Platform Engineer",
            "categories": {},
            "hostedUrl": "https://jobs.lever.co/example/valid-1",
        }
        malformed_lever_jobs = [
            None,
            {**valid_lever_job, "id": {"nested": "id"}},
            {**valid_lever_job, "text": 123},
            {**valid_lever_job, "hostedUrl": 456},
        ]
        with patch(
            "job_sources.ats.listings._request_json",
            return_value=[*malformed_lever_jobs, valid_lever_job],
        ):
            lever_result = collect_ats_jobs(
                "https://jobs.lever.co/example",
                score=False,
            )
        self.assertEqual(
            tuple(job.source_job_id for job in lever_result.jobs),
            ("example:valid-1",),
        )

        changed_payloads = (
            ("https://boards.greenhouse.io/example", {}),
            ("https://jobs.lever.co/example", {"postings": []}),
        )
        for url, payload in changed_payloads:
            with self.subTest(url=url), patch(
                "job_sources.ats.listings._request_json",
                return_value=payload,
            ):
                with self.assertRaisesRegex(
                    JobListingFetchError,
                    "collection_failed",
                ):
                    collect_ats_jobs(url, score=False)

    def test_derives_internship_and_campus_channels_conservatively(self):
        from job_sources.ats.listings import collect_ats_jobs

        greenhouse_payload = {
            "jobs": [
                {
                    "id": 1,
                    "title": "Software Engineering Intern",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                    "content": "Learn and build",
                },
                {
                    "id": 2,
                    "title": "Graduate Engineer",
                    "location": {"name": "London"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/2",
                    "content": "Start your career",
                },
                {
                    "id": 3,
                    "title": "International Sales Engineer",
                    "location": {"name": "Singapore"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/3",
                    "content": "Grow global sales",
                },
            ],
            "meta": {"total": 3},
        }
        with patch(
            "job_sources.ats.listings._request_json",
            return_value=greenhouse_payload,
        ):
            greenhouse = collect_ats_jobs(
                "https://boards.greenhouse.io/x",
                score=False,
            )
        self.assertEqual(
            tuple(job.recruitment_channel for job in greenhouse.jobs),
            ("internship", "campus", "social"),
        )

        lever_payload = [
            {
                "id": "graduate-1",
                "text": "University Graduate Analyst",
                "categories": {"commitment": "Full-time"},
                "hostedUrl": "https://jobs.lever.co/x/graduate-1",
            }
        ]
        with patch(
            "job_sources.ats.listings._request_json",
            return_value=lever_payload,
        ):
            lever = collect_ats_jobs(
                "https://jobs.lever.co/x",
                score=False,
            )
        self.assertEqual(lever.jobs[0].recruitment_channel, "campus")

    def test_maps_scoring_failures_to_the_safe_collection_error(self):
        from job_sources.ats.listings import (
            JobListingFetchError,
            collect_ats_jobs,
        )

        payload = {
            "jobs": [
                {
                    "id": 1,
                    "title": "Backend Engineer",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                    "content": "Build APIs",
                }
            ],
            "meta": {"total": 1},
        }
        with (
            patch("job_sources.ats.listings._request_json", return_value=payload),
            patch(
                "job_sources.ats.listings.evaluate_normalized_job",
                side_effect=RuntimeError("secret scoring detail"),
            ),
        ):
            with self.assertRaises(JobListingFetchError) as raised:
                collect_ats_jobs("https://boards.greenhouse.io/x")
        self.assertEqual(str(raised.exception), "collection_failed")

    def test_scores_returned_jobs_and_orders_ties_stably(self):
        from job_sources.ats.listings import collect_ats_jobs

        payload = {
            "jobs": [
                {
                    "id": 1,
                    "title": "First",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/1",
                    "content": "One",
                },
                {
                    "id": 2,
                    "title": "Second",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/2",
                    "content": "Two",
                },
                {
                    "id": 3,
                    "title": "Third",
                    "location": {"name": "Remote"},
                    "absolute_url": "https://boards.greenhouse.io/x/jobs/3",
                    "content": "Three",
                },
            ],
            "meta": {"total": 3},
        }
        scores = {"First": 70.0, "Second": 90.0, "Third": 90.0}

        def score_job(job):
            score = scores[job.title]
            return replace(job, score=score, qualified=score >= 80)

        with (
            patch(
                "job_sources.ats.listings._request_json",
                return_value=payload,
            ),
            patch(
                "job_sources.ats.listings.evaluate_normalized_job",
                side_effect=score_job,
            ) as evaluator,
        ):
            result = collect_ats_jobs(
                "https://boards.greenhouse.io/x",
                limit=3,
            )

        self.assertEqual(
            tuple(job.title for job in result.jobs),
            ("Second", "Third", "First"),
        )
        self.assertEqual(
            tuple(job.score for job in result.jobs),
            (90.0, 90.0, 70.0),
        )
        self.assertEqual(
            tuple(job.qualified for job in result.jobs),
            (True, True, False),
        )
        self.assertEqual(evaluator.call_count, 3)

    def test_paginates_lever_until_a_short_page(self):
        from job_sources.ats.listings import collect_ats_jobs

        def lever_job(index):
            return {
                "id": f"job-{index}",
                "text": f"Engineer {index}",
                "categories": {"location": "London"},
                "descriptionPlain": "Build systems",
                "hostedUrl": f"https://jobs.eu.lever.co/acme/job-{index}",
                "applyUrl": f"https://jobs.eu.lever.co/acme/job-{index}/apply",
                "workplaceType": "on-site",
            }

        first_page = [lever_job(index) for index in range(50)]
        second_page = [lever_job(50), lever_job(51)]
        with patch(
            "job_sources.ats.listings._request_json",
            side_effect=(first_page, second_page),
        ) as request_json:
            result = collect_ats_jobs(
                "https://jobs.eu.lever.co/acme",
                limit=50,
                score=False,
            )

        self.assertEqual(result.discovered_count, 52)
        self.assertEqual(result.scanned_count, 52)
        self.assertEqual(result.matched_count, 52)
        self.assertEqual(len(result.jobs), 50)
        self.assertTrue(result.complete)
        self.assertEqual(result.stop_reason, "upstream_end")
        self.assertEqual(
            tuple(call.args[0] for call in request_json.call_args_list),
            (
                "https://api.eu.lever.co/v0/postings/"
                "acme?mode=json&skip=0&limit=50",
                "https://api.eu.lever.co/v0/postings/"
                "acme?mode=json&skip=50&limit=50",
            ),
        )

    def test_caps_beisen_pagination_at_two_hundred_scanned_jobs(self):
        from job_sources.ats.listings import collect_ats_jobs

        endpoint = "https://nfyy.zhiye.com/api/Jobad/GetJobAdPageList"

        def raw_job(index):
            return {
                "JobAdId": index,
                "JobAdName": f"医师 {index}",
                "CategoryId": "1",
                "LocNames": ["广州市"],
            }

        openers = []
        for page_index in range(4):
            data = [
                raw_job(page_index * 50 + offset)
                for offset in range(50)
            ]
            body = json.dumps(
                {"Code": 200, "Count": 249, "Data": data},
                ensure_ascii=False,
            ).encode("utf-8")
            openers.append(
                FakeOpener(FakeHttpResponse(body, url=endpoint))
            )

        with patch(
            "job_sources.ats.listings.beisen.build_opener",
            side_effect=openers,
        ) as make_opener:
            result = collect_ats_jobs(
                "https://nfyy.zhiye.com/jobs",
                limit=50,
                score=False,
            )

        self.assertEqual(result.discovered_count, 249)
        self.assertEqual(result.scanned_count, 200)
        self.assertEqual(result.matched_count, 200)
        self.assertEqual(len(result.jobs), 50)
        self.assertFalse(result.complete)
        self.assertEqual(result.stop_reason, "scan_limit")
        self.assertEqual(make_opener.call_count, 4)
        self.assertEqual(
            tuple(
                json.loads(opener.calls[0][0].data.decode("utf-8"))[
                    "PageIndex"
                ]
                for opener in openers
            ),
            (0, 1, 2, 3),
        )

    def test_beisen_changed_envelopes_fail_without_leaking_details(self):
        from job_sources.ats.listings import (
            JobListingFetchError,
            collect_ats_jobs,
        )

        endpoint = "https://beisen.zhiye.com/api/Jobad/GetJobAdPageList"
        payloads = (
            {"Code": 500, "Count": 0, "Data": []},
            {"Code": 200, "Count": -1, "Data": []},
            {"Code": 200, "Count": 1, "Data": {}},
            ["unexpected"],
        )
        for payload in payloads:
            response = FakeHttpResponse(
                json.dumps(payload).encode("utf-8"),
                url=endpoint,
            )
            with self.subTest(payload=payload), patch(
                "job_sources.ats.listings.beisen.build_opener",
                return_value=FakeOpener(response),
            ):
                with self.assertRaises(JobListingFetchError) as raised:
                    collect_ats_jobs(
                        "https://beisen.zhiye.com/jobs",
                        score=False,
                    )
                self.assertEqual(str(raised.exception), "collection_failed")

    def test_beisen_transport_failures_are_bounded_and_safe(self):
        from job_sources.ats.listings import (
            JobListingFetchError,
            collect_ats_jobs,
        )

        endpoint = "https://beisen.zhiye.com/api/Jobad/GetJobAdPageList"
        failures = (
            FakeOpener(error=OSError("private upstream detail")),
            FakeOpener(FakeHttpResponse(b"{}", url=endpoint, status=503)),
            FakeOpener(
                FakeHttpResponse(
                    b"{}",
                    url=endpoint,
                    content_type="text/html",
                )
            ),
            FakeOpener(FakeHttpResponse(b"not json", url=endpoint)),
            FakeOpener(
                FakeHttpResponse(
                    b"x" * (4 * 1024 * 1024 + 1),
                    url=endpoint,
                )
            ),
            FakeOpener(
                FakeHttpResponse(
                    b"{}",
                    url="https://evil.example/jobs",
                )
            ),
        )

        for opener in failures:
            with self.subTest(opener=opener), patch(
                "job_sources.ats.listings.beisen.build_opener",
                return_value=opener,
            ):
                with self.assertRaises(JobListingFetchError) as raised:
                    collect_ats_jobs(
                        "https://beisen.zhiye.com/jobs",
                        score=False,
                    )
                self.assertEqual(str(raised.exception), "collection_failed")

        with patch(
            "job_sources.ats.listings.beisen.build_opener",
            return_value=FakeOpener(error=OSError("stopped")),
        ) as make_opener:
            with self.assertRaises(JobListingFetchError):
                collect_ats_jobs(
                    "https://beisen.zhiye.com/jobs",
                    score=False,
                )
        redirect_handler = make_opener.call_args.args[0]
        self.assertIsNone(
            redirect_handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://beisen.zhiye.com/other",
            )
        )

    def test_filters_beisen_jobs_by_public_organization_metadata(self):
        from job_sources.ats.listings import collect_ats_jobs

        endpoint = "https://beisen.zhiye.com/api/Jobad/GetJobAdPageList"
        payload = {
            "Code": 200,
            "Count": 3,
            "Data": [
                {
                    "JobAdId": 7,
                    "JobAdName": "研发工程师",
                    "Org": "人工智能中心",
                    "Category": "社会招聘",
                    "CategoryId": "1",
                },
                {
                    "JobAdId": 7,
                    "JobAdName": "重复岗位",
                    "Org": "人工智能中心",
                    "CategoryId": "1",
                },
                {"JobAdId": 8, "Org": "人工智能中心"},
            ],
        }
        response = FakeHttpResponse(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            url=endpoint,
        )
        with patch(
            "job_sources.ats.listings.beisen.build_opener",
            return_value=FakeOpener(response),
        ):
            result = collect_ats_jobs(
                "https://beisen.zhiye.com/jobs",
                keyword="人工智能",
                score=False,
            )

        self.assertEqual(result.scanned_count, 3)
        self.assertEqual(result.matched_count, 1)
        self.assertEqual(
            tuple(job.source_job_id for job in result.jobs),
            ("beisen.zhiye.com:7",),
        )

    def test_maps_beisen_channels_routes_and_empty_date_sentinel(self):
        from job_sources.ats.listings import collect_ats_jobs

        endpoint = "https://beisen.zhiye.com/api/Jobad/GetJobAdPageList"
        payload = {
            "Code": 200,
            "Count": 3,
            "Data": [
                {
                    "JobAdId": 1,
                    "JobAdName": "社会招聘岗位",
                    "CategoryId": "1",
                    "PostDate": "0001-01-01T00:00:00",
                },
                {
                    "JobAdId": 2,
                    "JobAdName": "校园招聘岗位",
                    "CategoryId": 2,
                },
                {
                    "JobAdId": 3,
                    "JobAdName": "暑期岗位",
                    "Category": "暑期实习",
                    "CategoryId": "99",
                },
            ],
        }
        response = FakeHttpResponse(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            url=endpoint,
        )
        with patch(
            "job_sources.ats.listings.beisen.build_opener",
            return_value=FakeOpener(response),
        ):
            result = collect_ats_jobs(
                "https://beisen.zhiye.com/jobs",
                score=False,
            )

        self.assertEqual(
            tuple(job.recruitment_channel for job in result.jobs),
            ("social", "campus", "internship"),
        )
        self.assertEqual(
            tuple(job.source_url for job in result.jobs),
            (
                "https://beisen.zhiye.com/social/detail?jobAdId=1",
                "https://beisen.zhiye.com/campus/detail?jobAdId=2",
                "https://beisen.zhiye.com/intern/detail?jobAdId=3",
            ),
        )
        self.assertIsNone(result.jobs[0].published_at)
        self.assertEqual(result.jobs[1].campus, {})
        self.assertEqual(result.jobs[2].internship, {})

    def test_fetches_public_json_with_fixed_get_and_timeout(self):
        from job_sources.ats.listings import collect_ats_jobs

        endpoint = (
            "https://boards-api.greenhouse.io/v1/boards/"
            "example/jobs?content=true"
        )
        body = json.dumps(
            {
                "jobs": [
                    {
                        "id": 7,
                        "title": "Backend Engineer",
                        "location": {"name": "Remote"},
                        "absolute_url": (
                            "https://boards.greenhouse.io/example/jobs/7"
                        ),
                        "content": "Build APIs",
                    }
                ],
                "meta": {"total": 1},
            }
        ).encode("utf-8")
        response = FakeHttpResponse(body, url=endpoint)

        opener = FakeOpener(response=response)
        with patch(
            "job_sources.ats.listings.build_opener",
            return_value=opener,
        ) as make_opener:
            result = collect_ats_jobs(
                "https://boards.greenhouse.io/example",
                score=False,
            )

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(make_opener.call_count, 1)
        redirect_handler = make_opener.call_args.args[0]
        self.assertEqual(redirect_handler.max_redirections, 2)
        request, open_kwargs = opener.calls[0]
        self.assertEqual(request.full_url, endpoint)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertEqual(open_kwargs, {"timeout": 10.0})

    def test_maps_transport_and_response_failures_to_one_safe_error(self):
        from job_sources.ats.listings import (
            JobListingFetchError,
            collect_ats_jobs,
        )

        endpoint = (
            "https://boards-api.greenhouse.io/v1/boards/"
            "example/jobs?content=true"
        )
        failures = (
            FakeOpener(error=OSError("secret upstream detail")),
            FakeOpener(
                response=FakeHttpResponse(b"{}", url=endpoint, status=503)
            ),
            FakeOpener(
                response=FakeHttpResponse(
                    b"{}", url=endpoint, content_type="text/html"
                )
            ),
            FakeOpener(
                response=FakeHttpResponse(b"not json", url=endpoint)
            ),
            FakeOpener(
                response=FakeHttpResponse(
                    b"x" * (4 * 1024 * 1024 + 1),
                    url=endpoint,
                )
            ),
            FakeOpener(
                response=FakeHttpResponse(
                    b"{}",
                    url="https://evil.example/jobs",
                )
            ),
        )

        for opener in failures:
            with self.subTest(opener=opener), patch(
                "job_sources.ats.listings.build_opener",
                return_value=opener,
            ):
                with self.assertRaises(JobListingFetchError) as raised:
                    collect_ats_jobs(
                        "https://boards.greenhouse.io/example",
                        score=False,
                    )
                self.assertEqual(str(raised.exception), "collection_failed")


if __name__ == "__main__":
    unittest.main()
