from datetime import datetime, timezone
import unittest

from resume_agent.discovery_schemas import (
    DiscoveredJob,
    canonicalize_job_url,
    deduplicate_discovered_jobs,
)
from resume_agent.job_family import known_job_family


def discovered(*, source, source_job_id, url):
    return DiscoveredJob(
        source=source,
        source_job_id=source_job_id,
        canonical_url=url,
        title=f"Example role {source_job_id}",
        detail=f"Example job detail {source_job_id}",
        detail_status="complete",
        search_keyword="Example",
        verified_at=datetime.now(timezone.utc),
    )


class DiscoveryModelTests(unittest.TestCase):
    def test_url_identity_removes_fragment_and_tracking_query(self):
        self.assertEqual(
            canonicalize_job_url(
                "HTTPS://Jobs.Example.com/role/7?utm_source=x&jobId=7#detail"
            ),
            "https://jobs.example.com/role/7?jobId=7",
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

    def test_known_job_family_covers_agent_sales_accounting_and_nursing(self):
        self.assertEqual(known_job_family("AI Agent Engineer"), "ai_engineering")
        self.assertEqual(known_job_family("销售经理"), "sales")
        self.assertEqual(known_job_family("Accountant"), "finance_accounting")
        self.assertEqual(known_job_family("临床护士"), "healthcare_nursing")
        self.assertIsNone(known_job_family("Backend Specialist"))
