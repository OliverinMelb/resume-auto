from datetime import datetime, timedelta, timezone
from typing import get_args
import unittest

from pydantic import ValidationError

from resume_agent.discovery_schemas import (
    DiscoveryRequest,
    DiscoveryRunStatus,
    DiscoveredJob,
    canonicalize_job_url,
    deduplicate_discovered_jobs,
    discovered_job_fingerprint,
)
from resume_agent.job_family import known_job_family


def discovered(*, source, source_job_id, url, **overrides):
    payload = {
        "source": source,
        "source_job_id": source_job_id,
        "canonical_url": url,
        "title": f"Example role {source_job_id}",
        "detail": f"Example job detail {source_job_id}",
        "detail_status": "complete",
        "search_keyword": "Example",
        "verified_at": datetime.now(timezone.utc),
    }
    payload.update(overrides)
    return DiscoveredJob(**payload)


class DiscoveryModelTests(unittest.TestCase):
    def test_url_identity_removes_fragment_and_tracking_query(self):
        self.assertEqual(
            canonicalize_job_url(
                "HTTPS://Jobs.Example.com/role/7?utm_source=x&jobId=7#detail"
            ),
            "https://jobs.example.com/role/7?jobId=7",
        )

    def test_url_identity_preserves_bracketed_ipv6_with_userinfo_and_port(self):
        self.assertEqual(
            canonicalize_job_url(
                "HTTP://User:Secret@[::1]:8080/role/7?utm_source=x&jobId=7#detail"
            ),
            "http://User:Secret@[::1]:8080/role/7?jobId=7",
        )

    def test_deduplication_uses_url_then_source_id_then_content(self):
        jobs = [
            discovered(source="boss", source_job_id="7", url="https://x/job/7"),
            discovered(source="boss", source_job_id="8", url="https://x/job/7#top"),
            discovered(source="nowcoder", source_job_id="9", url="https://n/job/9"),
        ]
        self.assertEqual(
            [item.source_job_id for item in deduplicate_discovered_jobs(jobs)],
            ["7", "9"],
        )

    def test_deduplication_uses_source_id_after_distinct_urls(self):
        first = discovered(source="boss", source_job_id="7", url="https://x/job/7")
        duplicate = discovered(
            source="boss", source_job_id="7", url="https://x/job/7-new",
        )
        self.assertEqual(deduplicate_discovered_jobs([first, duplicate]), [first])

    def test_deduplication_uses_normalized_content_after_url_and_source_id(self):
        first = discovered(
            source="boss", source_job_id="7", url="https://x/job/7",
            company=" Example  Co ", title="AI Agent Engineer",
            detail="Build agent\nworkflows",
        )
        duplicate = discovered(
            source="nowcoder", source_job_id="9", url="https://n/job/9",
            company="example co", title="ai agent engineer",
            detail="build agent workflows",
        )
        self.assertEqual(deduplicate_discovered_jobs([first, duplicate]), [first])

    def test_content_fingerprint_is_stable_for_normalized_content(self):
        first = discovered(
            source="boss", source_job_id="7", url="https://x/job/7",
            company=" Example  Co ", title="AI Agent Engineer",
            detail="Build agent\nworkflows",
        )
        equivalent = discovered(
            source="nowcoder", source_job_id="9", url="https://n/job/9",
            company="example co", title="ai agent engineer",
            detail="build agent workflows",
        )
        fingerprint = discovered_job_fingerprint(first)
        self.assertEqual(fingerprint, discovered_job_fingerprint(first))
        self.assertEqual(fingerprint, discovered_job_fingerprint(equivalent))
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")

    def test_discovered_job_rejects_non_utc_timestamps(self):
        with self.assertRaises(ValidationError):
            discovered(
                source="boss", source_job_id="7", url="https://x/job/7",
                verified_at=datetime(2026, 8, 26, 1, 0),
            )
        with self.assertRaises(ValidationError):
            discovered(
                source="boss", source_job_id="8", url="https://x/job/8",
                verified_at=datetime(2026, 8, 26, 9, 0, tzinfo=timezone(timedelta(hours=8))),
            )

    def test_discovery_request_rejects_duplicate_sources(self):
        with self.assertRaises(ValidationError):
            DiscoveryRequest(candidate_id="candidate-1", sources=["boss", "boss"], keywords=["AI Agent"])

    def test_discovery_request_rejects_duplicate_keywords(self):
        with self.assertRaises(ValidationError):
            DiscoveryRequest(candidate_id="candidate-1", sources=["boss"], keywords=["AI Agent", "AI Agent"])

    def test_public_models_reject_extra_fields_and_invalid_score_bound(self):
        with self.assertRaises(ValidationError):
            discovered(source="boss", source_job_id="7", url="https://x/job/7", extra="value")
        with self.assertRaises(ValidationError):
            discovered(source="boss", source_job_id="8", url="https://x/job/8", score=101)

    def test_discovery_run_status_contract_is_closed(self):
        self.assertEqual(
            get_args(DiscoveryRunStatus),
            ("CREATED", "COLLECTING", "READY_FOR_IMPORT", "PARTIAL", "IMPORTED", "FAILED"),
        )

    def test_known_job_family_covers_agent_sales_accounting_and_nursing(self):
        self.assertEqual(known_job_family("AI Agent Engineer"), "ai_engineering")
        self.assertEqual(known_job_family("销售经理"), "sales")
        self.assertEqual(known_job_family("Accountant"), "finance_accounting")
        self.assertEqual(known_job_family("临床护士"), "healthcare_nursing")
        self.assertIsNone(known_job_family("Backend Specialist"))
