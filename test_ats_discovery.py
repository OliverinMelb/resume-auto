import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from job_sources.contracts import NormalizedJob


def normalized_job(
    *,
    source: str,
    source_job_id: str,
    company: str,
    title: str,
    score: float,
    url: str,
) -> NormalizedJob:
    return NormalizedJob(
        source=source,
        source_job_id=source_job_id,
        source_url=url,
        official_apply_url=url,
        title=title,
        company=company,
        locations=("Remote",),
        recruitment_channel="social",
        employment_type=None,
        work_mode="remote",
        description="Build reliable products",
        responsibilities=(),
        requirements=(),
        salary_text=None,
        published_at=None,
        expires_at=None,
        last_verified_at="2026-08-04T12:00:00+00:00",
        campus=None,
        internship=None,
        raw_data={},
        upstream_platform=source,
        score=score,
        qualified=True,
    )


class AtsTenantDiscoveryTests(unittest.TestCase):
    def test_detects_the_official_smartrecruiters_career_host(self):
        from job_sources.ats import detect_ats

        result = detect_ats(
            "https://careers.smartrecruiters.com/smartrecruiters",
            "<main>Open positions</main>",
        )

        self.assertEqual(result.ats, "smartrecruiters")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(
            result.evidence,
            ("hostname:careers.smartrecruiters.com",),
        )

    def test_detects_the_official_workable_career_host(self):
        from job_sources.ats import detect_ats

        result = detect_ats(
            "https://apply.workable.com/clearstory",
            "<main>Open positions</main>",
        )

        self.assertEqual(result.ats, "workable")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(result.evidence, ("hostname:apply.workable.com",))

    def test_detects_the_official_ashby_job_board_host(self):
        from job_sources.ats import detect_ats

        result = detect_ats(
            "https://jobs.ashbyhq.com/linear",
            "<main>Open positions</main>",
        )

        self.assertEqual(result.ats, "ashby")
        self.assertGreaterEqual(result.confidence, 0.9)
        self.assertEqual(result.evidence, ("hostname:jobs.ashbyhq.com",))

    def test_collects_and_normalizes_ashby_public_jobs(self):
        from job_sources.ats.listings import collect_ats_jobs

        payload = {
            "apiVersion": "1",
            "jobs": [
                {
                    "title": "Frontend Engineer",
                    "location": "San Francisco",
                    "secondaryLocations": [{"location": "Remote - US"}],
                    "department": "Engineering",
                    "team": "Product",
                    "isListed": True,
                    "isRemote": True,
                    "workplaceType": "Remote",
                    "descriptionPlain": "Build TypeScript products.",
                    "publishedAt": "2026-08-01T12:00:00+00:00",
                    "employmentType": "FullTime",
                    "jobUrl": "https://jobs.ashbyhq.com/linear/job-123",
                    "applyUrl": "https://jobs.ashbyhq.com/linear/job-123/application",
                    "compensation": {
                        "scrapeableCompensationSalarySummary": "$180K - $220K"
                    },
                },
                {
                    "title": "Hidden role",
                    "location": "Remote",
                    "isListed": False,
                    "jobUrl": "https://jobs.ashbyhq.com/linear/hidden",
                    "applyUrl": "https://jobs.ashbyhq.com/linear/hidden/application",
                },
                {
                    "title": "Wrong tenant",
                    "location": "Remote",
                    "isListed": True,
                    "jobUrl": "https://jobs.ashbyhq.com/attacker/job-456",
                    "applyUrl": "https://jobs.ashbyhq.com/attacker/job-456/application",
                },
                {
                    "title": "External application",
                    "location": "Remote",
                    "isListed": True,
                    "jobUrl": "https://jobs.ashbyhq.com/linear/job-789",
                    "applyUrl": "https://phishing.example/job-789/application",
                },
                {
                    "title": "Mismatched nested path",
                    "location": "Remote",
                    "isListed": True,
                    "jobUrl": "https://jobs.ashbyhq.com/linear/group/job-999",
                    "applyUrl": "https://jobs.ashbyhq.com/linear/group/other/application",
                },
            ],
        }

        with patch(
            "job_sources.ats.listings._request_json",
            return_value=payload,
        ) as request_json:
            result = collect_ats_jobs(
                "https://jobs.ashbyhq.com/linear",
                company="Linear",
                score=False,
            )

        self.assertEqual(result.ats, "ashby")
        self.assertEqual(result.discovered_count, 5)
        self.assertEqual(result.scanned_count, 5)
        self.assertEqual(result.matched_count, 1)
        self.assertEqual(len(result.jobs), 1)
        job = result.jobs[0]
        self.assertEqual(job.source_job_id, "linear:job-123")
        self.assertEqual(job.title, "Frontend Engineer")
        self.assertEqual(job.company, "Linear")
        self.assertEqual(job.locations, ("San Francisco", "Remote - US"))
        self.assertEqual(job.employment_type, "fulltime")
        self.assertEqual(job.work_mode, "remote")
        self.assertEqual(job.description, "Build TypeScript products.")
        self.assertEqual(job.salary_text, "$180K - $220K")
        self.assertEqual(job.published_at, "2026-08-01T12:00:00+00:00")
        self.assertEqual(job.upstream_platform, "ashby")
        self.assertEqual(
            job.raw_data,
            {"department": "Engineering", "team": "Product"},
        )
        request_json.assert_called_once_with(
            "https://api.ashbyhq.com/posting-api/job-board/linear"
        )

    def test_collects_smartrecruiters_public_posting_details_safely(self):
        from job_sources.ats.listings import collect_ats_jobs

        listing = {
            "limit": 100,
            "offset": 0,
            "totalFound": 2,
            "content": [
                {"id": "123", "name": "Frontend Engineer"},
                {"id": "456", "name": "Unsafe Engineer"},
            ],
        }
        valid_detail = {
            "id": "123",
            "name": "Frontend Engineer",
            "company": {"identifier": "acme", "name": "Acme"},
            "releasedDate": "2026-08-01T09:30:00Z",
            "location": {
                "fullLocation": "Berlin, Germany",
                "remote": True,
                "hybrid": False,
            },
            "department": {"label": "Engineering"},
            "function": {"label": "Product Development"},
            "typeOfEmployment": {"label": "Full-time"},
            "postingUrl": (
                "https://jobs.smartrecruiters.com/acme/123-frontend-engineer"
            ),
            "applyUrl": (
                "https://jobs.smartrecruiters.com/acme/"
                "123-frontend-engineer?oga=true"
            ),
            "active": True,
            "jobAd": {
                "sections": {
                    "jobDescription": {
                        "title": "Job Description",
                        "text": "<p>Build accessible React products.</p>",
                    },
                    "qualifications": {
                        "title": "Qualifications",
                        "text": "<ul><li>TypeScript</li></ul>",
                    },
                }
            },
        }
        unsafe_detail = {
            **valid_detail,
            "id": "456",
            "name": "Unsafe Engineer",
            "postingUrl": "https://jobs.smartrecruiters.com/acme/456-unsafe",
            "applyUrl": "https://phishing.example/acme/456-unsafe",
        }

        with patch(
            "job_sources.ats.listings._request_json",
            side_effect=[listing, valid_detail, unsafe_detail],
        ) as request_json:
            result = collect_ats_jobs(
                "https://careers.smartrecruiters.com/acme",
                keyword="frontend",
                score=False,
            )

        self.assertEqual(result.ats, "smartrecruiters")
        self.assertEqual(result.discovered_count, 2)
        self.assertEqual(result.scanned_count, 2)
        self.assertEqual(result.matched_count, 1)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.jobs), 1)
        job = result.jobs[0]
        self.assertEqual(job.source_job_id, "acme:123")
        self.assertEqual(job.title, "Frontend Engineer")
        self.assertEqual(job.company, "Acme")
        self.assertEqual(job.locations, ("Berlin, Germany",))
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.work_mode, "remote")
        self.assertEqual(
            job.description,
            "Build accessible React products. TypeScript",
        )
        self.assertEqual(job.published_at, "2026-08-01T09:30:00Z")
        self.assertEqual(job.upstream_platform, "smartrecruiters")
        self.assertEqual(
            request_json.call_args_list,
            [
                unittest.mock.call(
                    "https://api.smartrecruiters.com/v1/companies/"
                    "acme/postings?q=frontend&limit=100&offset=0"
                ),
                unittest.mock.call(
                    "https://api.smartrecruiters.com/v1/companies/"
                    "acme/postings/123"
                ),
                unittest.mock.call(
                    "https://api.smartrecruiters.com/v1/companies/"
                    "acme/postings/456"
                ),
            ],
        )

    def test_smartrecruiters_detail_transport_failure_fails_the_board(self):
        from job_sources.ats.listings import (
            JobListingFetchError,
            collect_ats_jobs,
        )

        listing = {
            "limit": 100,
            "offset": 0,
            "totalFound": 1,
            "content": [{"id": "123", "name": "Frontend Engineer"}],
        }
        with patch(
            "job_sources.ats.listings._request_json",
            side_effect=[listing, JobListingFetchError("collection_failed")],
        ):
            with self.assertRaisesRegex(
                JobListingFetchError, "collection_failed"
            ):
                collect_ats_jobs(
                    "https://careers.smartrecruiters.com/acme",
                    score=False,
                )

    def test_smartrecruiters_discards_detail_with_another_posting_id(self):
        from job_sources.ats.listings import collect_ats_jobs

        listing = {
            "limit": 100,
            "offset": 0,
            "totalFound": 1,
            "content": [{"id": "123", "name": "Frontend Engineer"}],
        }
        mismatched_detail = {
            "id": "999",
            "name": "Different Engineer",
            "company": {"identifier": "acme", "name": "Acme"},
            "postingUrl": "https://jobs.smartrecruiters.com/acme/999-role",
            "applyUrl": "https://jobs.smartrecruiters.com/acme/999-role",
            "active": True,
        }
        with patch(
            "job_sources.ats.listings._request_json",
            side_effect=[listing, mismatched_detail],
        ):
            result = collect_ats_jobs(
                "https://careers.smartrecruiters.com/acme",
                score=False,
            )

        self.assertEqual(result.discovered_count, 1)
        self.assertEqual(result.scanned_count, 1)
        self.assertEqual(result.matched_count, 0)
        self.assertEqual(result.jobs, ())
        self.assertTrue(result.complete)

    def test_collects_workable_public_account_jobs_safely(self):
        from job_sources.ats.listings import collect_ats_jobs

        payload = {
            "name": "Clearstory",
            "description": "Construction software",
            "jobs": [
                {
                    "title": "Junior Software Engineer",
                    "shortcode": "B4E3BB8DE1",
                    "employment_type": "Full-time",
                    "telecommuting": True,
                    "department": "Engineering",
                    "function": "Software Development",
                    "url": (
                        "https://careers.clearstory.example/"
                        "jobs/B4E3BB8DE1"
                    ),
                    "shortlink": "https://apply.workable.com/j/B4E3BB8DE1",
                    "application_url": (
                        "https://apply.workable.com/j/B4E3BB8DE1/apply"
                    ),
                    "published_on": "2026-07-20",
                    "created_at": "2026-07-19",
                    "locations": [
                        {
                            "country": "United States",
                            "city": "Walnut Creek",
                            "region": "California",
                            "hidden": False,
                        }
                    ],
                    "description": (
                        "<p>Build React and TypeScript applications.</p>"
                    ),
                },
                {
                    "title": "Unsafe Software Engineer",
                    "shortcode": "BAD0000001",
                    "url": "https://apply.workable.com/j/OTHER000001",
                    "application_url": (
                        "https://apply.workable.com/j/BAD0000001/apply"
                    ),
                    "description": "Should be excluded",
                },
            ],
        }

        with patch(
            "job_sources.ats.listings._request_json",
            return_value=payload,
        ) as request_json:
            result = collect_ats_jobs(
                "https://apply.workable.com/clearstory",
                keyword="software",
                score=False,
            )

        self.assertEqual(result.ats, "workable")
        self.assertEqual(result.discovered_count, 2)
        self.assertEqual(result.scanned_count, 2)
        self.assertEqual(result.matched_count, 1)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.jobs), 1)
        job = result.jobs[0]
        self.assertEqual(job.source_job_id, "clearstory:B4E3BB8DE1")
        self.assertEqual(
            job.source_url,
            "https://apply.workable.com/j/B4E3BB8DE1",
        )
        self.assertEqual(job.title, "Junior Software Engineer")
        self.assertEqual(job.company, "Clearstory")
        self.assertEqual(
            job.locations,
            ("Walnut Creek, California, United States",),
        )
        self.assertEqual(job.employment_type, "full-time")
        self.assertEqual(job.work_mode, "remote")
        self.assertEqual(
            job.description,
            "Build React and TypeScript applications.",
        )
        self.assertEqual(job.published_at, "2026-07-20")
        self.assertEqual(job.upstream_platform, "workable")
        request_json.assert_called_once_with(
            "https://www.workable.com/api/accounts/clearstory?details=true",
            allowed_redirect_origins=(("https", "apply.workable.com", 443),),
        )

    def test_rejects_global_workable_job_url_without_a_tenant(self):
        from job_sources.ats.listings import (
            UnsupportedJobBoardError,
            collect_ats_jobs,
        )

        with patch("job_sources.ats.listings._request_json") as request_json:
            with self.assertRaises(UnsupportedJobBoardError):
                collect_ats_jobs(
                    "https://apply.workable.com/j/B4E3BB8DE1",
                    score=False,
                )

        request_json.assert_not_called()

    def test_index_records_become_canonical_deduplicated_boards(self):
        from job_sources.ats.discovery import boards_from_index_records

        records = (
            {"url": "https://job-boards.greenhouse.io/OpenAI/jobs/123"},
            {"url": "https://boards.greenhouse.io/openai/jobs/456?x=1"},
            {"url": "https://jobs.lever.co/Netflix/abc/apply"},
            {"url": "https://jobs.eu.lever.co/spotify"},
            {"url": "https://jobs.ashbyhq.com/Linear/role-id"},
            {"url": "https://acme.zhiye.com/social/detail?jobAdId=7"},
            {"url": "http://jobs.lever.co/insecure/job"},
            {"url": "https://jobs.lever.co.attacker.example/not-safe"},
            {"url": "https://jobs.ashbyhq.com/invalid%20tenant/job"},
            {"not_url": "https://jobs.lever.co/missing"},
        )

        boards = boards_from_index_records(records, limit=50)

        self.assertEqual(
            boards,
            (
                self._board(
                    provider="beisen",
                    tenant="acme.zhiye.com",
                    career_url="https://acme.zhiye.com/jobs",
                ),
                self._board(
                    provider="ashby",
                    tenant="Linear",
                    career_url="https://jobs.ashbyhq.com/Linear",
                ),
                self._board(
                    provider="greenhouse",
                    tenant="openai",
                    career_url="https://job-boards.greenhouse.io/openai",
                ),
                self._board(
                    provider="lever",
                    tenant="Netflix",
                    career_url="https://jobs.lever.co/Netflix",
                ),
                self._board(
                    provider="lever",
                    tenant="spotify",
                    career_url="https://jobs.eu.lever.co/spotify",
                ),
            ),
        )

    def test_index_records_discover_one_canonical_smartrecruiters_board(self):
        from job_sources.ats.discovery import AtsBoard, boards_from_index_records

        boards = boards_from_index_records(
            (
                {
                    "url": (
                        "https://jobs.smartrecruiters.com/Acme/"
                        "744000123-engineer"
                    )
                },
                {"url": "https://careers.smartrecruiters.com/acme"},
                {"url": "https://jobs.smartrecruiters.com.attacker.test/acme/1"},
            )
        )

        self.assertEqual(
            boards,
            (
                AtsBoard(
                    "smartrecruiters",
                    "acme",
                    "https://careers.smartrecruiters.com/acme",
                ),
            ),
        )

    def test_index_records_discover_tenant_scoped_workable_board_only(self):
        from job_sources.ats.discovery import AtsBoard, boards_from_index_records

        boards = boards_from_index_records(
            (
                {"url": "https://apply.workable.com/Clearstory/"},
                {
                    "url": (
                        "https://apply.workable.com/clearstory/j/"
                        "B4E3BB8DE1/"
                    )
                },
                {"url": "https://apply.workable.com/j/B4E3BB8DE1/"},
                {"url": "https://apply.workable.com.attacker.test/clearstory"},
            )
        )

        self.assertEqual(
            boards,
            (
                AtsBoard(
                    "workable",
                    "clearstory",
                    "https://apply.workable.com/clearstory",
                ),
            ),
        )

    def test_discovery_uses_fixed_index_and_reuses_fresh_cache(self):
        from job_sources.ats.discovery import discover_ats_boards

        calls = []

        def fetch_text(url):
            calls.append(url)
            if url == "https://index.commoncrawl.org/collinfo.json":
                return json.dumps(
                    [{"id": "CC-MAIN-2026-30", "cdx-api": "ignored"}]
                )
            return "\n".join(
                (
                    json.dumps({"url": "https://jobs.lever.co/acme/role-1"}),
                    json.dumps({"url": "https://jobs.lever.co/acme/role-2"}),
                )
            )

        now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "ats-boards.json"
            first = discover_ats_boards(
                limit=10,
                refresh=True,
                cache_path=cache_path,
                fetch_text=fetch_text,
                now=now,
            )
            second = discover_ats_boards(
                limit=10,
                cache_path=cache_path,
                fetch_text=lambda _: self.fail("fresh cache should be reused"),
                now=now,
            )

        self.assertFalse(first.cached)
        self.assertEqual(first.crawl, "CC-MAIN-2026-30")
        self.assertEqual(len(first.boards), 1)
        self.assertTrue(any(board.tenant == "acme" for board in first.boards))
        self.assertTrue(second.cached)
        self.assertEqual(second.boards, first.boards)
        self.assertEqual(calls[0], "https://index.commoncrawl.org/collinfo.json")
        self.assertEqual(len(calls), 12)
        for url in calls:
            self.assertTrue(url.startswith("https://index.commoncrawl.org/"))
        self.assertTrue(any("output=json" in url and "limit=10" in url for url in calls[1:]))

        self.assertTrue(
            any("url=boards.eu.greenhouse.io%2F%2A" in url for url in calls[1:])
        )
        self.assertTrue(
            any(
                "url=careers.smartrecruiters.com%2F%2A" in url
                for url in calls[1:]
            )
        )
        self.assertTrue(
            any("url=apply.workable.com%2F%2A" in url for url in calls[1:])
        )

    def test_discovery_replaces_corrupt_cache_and_reports_transport_failure(self):
        from job_sources.ats.discovery import AtsDiscoveryError, discover_ats_boards

        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "ats-boards.json"
            cache_path.write_text("not-json", encoding="utf-8")

            def fetch_text(url):
                if url.endswith("collinfo.json"):
                    return json.dumps([{"id": "CC-MAIN-2026-30"}])
                return json.dumps({"url": "https://jobs.lever.co/acme/role"})

            catalog = discover_ats_boards(
                limit=20,
                cache_path=cache_path,
                fetch_text=fetch_text,
                now=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
            )
            self.assertEqual([board.tenant for board in catalog.boards], ["acme"])

            cache_path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(AtsDiscoveryError):
                discover_ats_boards(
                    limit=20,
                    cache_path=cache_path,
                    fetch_text=lambda _: (_ for _ in ()).throw(TimeoutError()),
                    now=datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
                )

    @staticmethod
    def _board(**values):
        from job_sources.ats.discovery import AtsBoard

        return AtsBoard(**values)


class AtsBatchSearchTests(unittest.TestCase):
    def test_searches_direct_collectors_and_isolates_failures(self):
        from job_sources.ats.discovery import AtsBoard
        from job_sources.ats.listings import (
            AtsJobCollection,
            JobListingFetchError,
        )
        from job_sources.ats.search import search_ats_boards

        boards = (
            AtsBoard("greenhouse", "acme", "https://job-boards.greenhouse.io/acme"),
            AtsBoard("lever", "broken", "https://jobs.lever.co/broken"),
            AtsBoard("ashby", "linear", "https://jobs.ashbyhq.com/linear"),
        )
        shared_url = "https://jobs.example/shared"
        high = normalized_job(
            source="greenhouse",
            source_job_id="acme:1",
            company="Acme",
            title="Frontend Engineer",
            score=95.0,
            url=shared_url,
        )
        duplicate = replace(
            high,
            source="ashby",
            source_job_id="linear:duplicate",
            company="Linear",
            score=90.0,
        )
        second = normalized_job(
            source="ashby",
            source_job_id="linear:2",
            company="Linear",
            title="Product Engineer",
            score=80.0,
            url="https://jobs.ashbyhq.com/linear/2",
        )
        calls = []

        def collect(url, *, company, keyword, limit, score):
            calls.append((url, company, keyword, limit, score))
            if "broken" in url:
                raise JobListingFetchError("collection_failed")
            jobs = (high,) if "greenhouse" in url else (duplicate, second)
            return AtsJobCollection(
                ats="greenhouse" if "greenhouse" in url else "ashby",
                jobs=jobs,
                discovered_count=len(jobs),
                scanned_count=len(jobs),
                matched_count=len(jobs),
                complete=True,
                stop_reason="upstream_end",
            )

        report = search_ats_boards(
            boards,
            keyword="frontend",
            limit=20,
            max_boards=20,
            max_workers=4,
            collector=collect,
        )

        self.assertEqual(report.searched_count, 3)
        self.assertEqual(report.searched_boards, boards)
        self.assertEqual(report.succeeded_count, 2)
        self.assertEqual(report.failed_count, 1)
        self.assertEqual([job.title for job in report.jobs], [
            "Frontend Engineer",
            "Product Engineer",
        ])
        self.assertEqual(report.failures[0].provider, "lever")
        self.assertEqual(report.failures[0].tenant, "broken")
        self.assertEqual(report.failures[0].reason, "collection_failed")
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[1] is None for call in calls))
        self.assertTrue(all(call[2:] == ("frontend", 20, True) for call in calls))

    def test_search_canonicalizes_urls_and_orders_complete_ties_stably(self):
        from job_sources.ats.discovery import AtsBoard
        from job_sources.ats.listings import AtsJobCollection
        from job_sources.ats.search import search_ats_boards

        boards = (
            AtsBoard("ashby", "second", "https://jobs.ashbyhq.com/second"),
            AtsBoard("ashby", "first", "https://jobs.ashbyhq.com/first"),
        )
        first = normalized_job(
            source="ashby",
            source_job_id="first:2",
            company="Same",
            title="Engineer",
            score=80.0,
            url="https://jobs.example/role/",
        )
        duplicate = replace(
            first,
            source_job_id="second:1",
            source_url="https://JOBS.EXAMPLE:443/role#details",
            official_apply_url="https://JOBS.EXAMPLE:443/role#apply",
        )

        def collect(url, **_):
            job = duplicate if url.endswith("second") else first
            return AtsJobCollection("ashby", (job,), 1, 1, 1, True, "upstream_end")

        outputs = []
        for _ in range(3):
            report = search_ats_boards(
                boards,
                keyword="engineer",
                collector=collect,
            )
            outputs.append(tuple(job.source_job_id for job in report.jobs))

        self.assertEqual(outputs, [("first:2",)] * 3)


if __name__ == "__main__":
    unittest.main()
