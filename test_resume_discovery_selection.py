import itertools
from datetime import datetime, timedelta, timezone
import unittest

from resume_agent.discovery_schemas import (
    DiscoveredJob,
    discovered_job_fingerprint,
)
from resume_agent.discovery_selection import (
    filter_complete_agent_jobs,
    select_representative_jobs,
)


BASE_TIME = datetime(2026, 8, 26, tzinfo=timezone.utc)


def job(
    title,
    *,
    source="boss",
    number=1,
    company="Company",
    detail="Build reliable Agent workflows",
    detail_status="complete",
    verified_at=BASE_TIME,
    qualified=None,
):
    host = (
        "www.zhipin.com/job_detail"
        if source == "boss"
        else "www.nowcoder.com/jobs/detail"
    )
    return DiscoveredJob(
        source=source,
        source_job_id=f"{source}-{number}",
        canonical_url=f"https://{host}/{number}",
        title=title,
        company=company,
        detail=detail,
        detail_status=detail_status,
        search_keyword="AI Agent",
        verified_at=verified_at,
        qualified=qualified,
    )


class DiscoverySelectionTests(unittest.TestCase):
    def test_filter_rejects_partial_cross_family_and_unconfirmed_unknown_titles(self):
        eligible, rejected = filter_complete_agent_jobs(
            [
                job("AI Agent Engineer", number=1),
                job("Mystery Builder", number=2, detail="Own an LLM platform"),
                job("护士", number=3, detail="Build an Agent workflow"),
                job("Agent Platform", number=4, detail_status="partial"),
                job("Mystery Builder", number=5, detail="Own internal systems"),
                job("AI Sales", number=6, detail="Sell an Agent workflow"),
            ]
        )

        self.assertEqual(
            [item.source_job_id for item in eligible], ["boss-1", "boss-2"]
        )
        self.assertEqual(
            [(item.job.source_job_id, item.reason) for item in rejected],
            [
                ("boss-3", "cross_family"),
                ("boss-4", "partial_detail"),
                ("boss-5", "unknown_family"),
                ("boss-6", "cross_family"),
            ],
        )

    def test_unknown_titles_require_an_explicit_agent_detail_signal(self):
        signals = ["Agent", "智能体", "RAG", "LLM", "大模型", "workflow", "工作流"]
        eligible, rejected = filter_complete_agent_jobs(
            [
                job(
                    "Platform Specialist",
                    number=index,
                    detail=f"Responsible for {signal} delivery",
                )
                for index, signal in enumerate(signals, start=1)
            ]
        )

        self.assertEqual(len(eligible), len(signals))
        self.assertEqual(rejected, [])

    def test_only_known_ai_engineering_titles_bypass_detail_confirmation(self):
        eligible, rejected = filter_complete_agent_jobs(
            [
                job("AI Engineer", number=1, detail="Own internal systems"),
                job("LLM Developer", number=2, detail="Own internal systems"),
                job("Agent Architect", number=3, detail="Own internal systems"),
            ]
        )

        self.assertEqual(len(eligible), 3)
        self.assertEqual(rejected, [])

    def test_unknown_bare_ai_and_agent_like_titles_require_detail_confirmation(self):
        eligible, rejected = filter_complete_agent_jobs(
            [
                job(
                    "AI Product Manager",
                    number=1,
                    detail="负责增长策略和商业化",
                ),
                job("AI Strategy Lead", number=2, detail="Own growth strategy"),
                job("Agent Platform", number=3, detail="Own internal systems"),
                job("RAG Specialist", number=4, detail="Own search systems"),
                job("AI Sales", number=5, detail="Own commercial accounts"),
            ]
        )

        self.assertEqual(eligible, [])
        self.assertEqual(
            [item.reason for item in rejected],
            [
                "unknown_family",
                "unknown_family",
                "unknown_family",
                "unknown_family",
                "cross_family",
            ],
        )

    def test_greedy_selection_is_permutation_stable_and_prioritizes_diversity(self):
        jobs = [
            job(
                "Agent Alpha",
                source="boss",
                number=1,
                company="A",
                verified_at=BASE_TIME + timedelta(days=4),
                qualified=True,
            ),
            job(
                "Agent Beta",
                source="nowcoder",
                number=2,
                company="A",
                verified_at=BASE_TIME,
                qualified=True,
            ),
            job(
                "Agent Gamma",
                source="boss",
                number=3,
                company="B",
                verified_at=BASE_TIME + timedelta(days=3),
                qualified=True,
            ),
            job(
                "Agent Delta",
                source="nowcoder",
                number=4,
                company="C",
                verified_at=BASE_TIME + timedelta(days=10),
                qualified=False,
            ),
        ]

        orders = {
            tuple(
                item.source_job_id
                for item in select_representative_jobs(
                    list(permutation), minimum=3, maximum=3
                )[0]
            )
            for permutation in itertools.permutations(jobs)
        }

        self.assertEqual(orders, {("boss-1", "nowcoder-2", "boss-3")})

    def test_fingerprint_is_the_final_stable_tie_breaker(self):
        tied = [
            job("Agent One", number=1, company="A", detail="Agent abc"),
            job("Agent Two", number=2, company="B", detail="Agent xyz"),
            job("Agent Six", number=6, company="C", detail="Agent qrs"),
        ]
        expected = sorted(tied, key=discovered_job_fingerprint)

        selected, rejected = select_representative_jobs(
            list(reversed(tied)), minimum=3, maximum=3
        )

        self.assertEqual(selected, expected)
        self.assertEqual(rejected, [])

    def test_selection_validates_bounds_and_never_exceeds_maximum(self):
        jobs = [job("Agent Engineer", number=index) for index in range(1, 22)]

        for minimum, maximum in [(2, 20), (3, 21), (5, 4), (True, 20)]:
            with self.subTest(minimum=minimum, maximum=maximum):
                with self.assertRaisesRegex(ValueError, "selection bounds"):
                    select_representative_jobs(
                        jobs, minimum=minimum, maximum=maximum
                    )

        selected, rejected = select_representative_jobs(
            jobs, minimum=3, maximum=7
        )
        self.assertEqual(len(selected), 7)
        self.assertEqual(rejected, [])


if __name__ == "__main__":
    unittest.main()
