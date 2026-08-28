import unittest

from resume_agent.rewrite_reports import (
    build_ats_report,
    build_claim_ledger,
    build_gap_roadmap,
)
from resume_agent.rewrite_schemas import CodexRewriteResult, GapAcceptancePlan


RESUME_ID = "source_resume_" + "a" * 32
JOB_ID = "job_" + "b" * 32
CANDIDATE_ID = "candidate_" + "c" * 32
ACCEPTANCE_ID = "gap_acceptance_" + "d" * 32


def rewrite_result(*, claims=None, plans=None, markdown="- Built Python agents"):
    return CodexRewriteResult.model_validate(
        {
            "verdict": "match",
            "scorecard": [],
            "risks": [],
            "missing_information": [],
            "rewritten_resume_markdown": markdown,
            "rewrite_reasons": [],
            "depth_recommendations": [],
            "interviewer_probes": [],
            "claim_audit": claims
            if claims is not None
            else [
                {
                    "claim": "Built Python agents",
                    "source_resume_ids": [RESUME_ID],
                    "source_quotes": ["Built Python agents"],
                    "job_ids": [JOB_ID],
                    "change_type": "strengthened",
                    "fact_status": "source_verified",
                    "measurement_basis": None,
                    "claim_status": "reframed",
                }
            ],
            "gap_acceptance_plans": plans if plans is not None else [],
        }
    )


class RewriteReportTests(unittest.TestCase):
    def test_claim_ledger_contains_only_the_restricted_contract(self):
        ledger = build_claim_ledger(
            rewrite_result(),
            allowed_resume_ids=[RESUME_ID],
            allowed_job_ids=[JOB_ID],
            gap_acceptances=[],
        )
        self.assertEqual(
            ledger,
            {
                "claims": [
                    {
                        "acceptance_id": None,
                        "change_type": "strengthened",
                        "claim": "Built Python agents",
                        "claim_status": "reframed",
                        "gap_id": None,
                        "source_job_ids": [JOB_ID],
                        "source_resume_ids": [RESUME_ID],
                    }
                ]
            },
        )

    def test_claim_ledger_rejects_unknown_and_duplicate_source_ids(self):
        base = rewrite_result().claim_audit[0].model_dump()
        cases = (
            ({**base, "source_resume_ids": ["source_resume_" + "c" * 32]}, [RESUME_ID], [JOB_ID]),
            ({**base, "source_resume_ids": [RESUME_ID, RESUME_ID]}, [RESUME_ID], [JOB_ID]),
            ({**base, "job_ids": ["job_" + "d" * 32]}, [RESUME_ID], [JOB_ID]),
            ({**base, "job_ids": [JOB_ID, JOB_ID]}, [RESUME_ID], [JOB_ID]),
            (base, [RESUME_ID, RESUME_ID], [JOB_ID]),
            (base, [RESUME_ID], [JOB_ID, JOB_ID]),
        )

        for claim, resume_ids, job_ids in cases:
            with self.subTest(claim=claim, resume_ids=resume_ids, job_ids=job_ids):
                with self.assertRaisesRegex(ValueError, "report input is invalid"):
                    build_claim_ledger(
                        rewrite_result(claims=[claim]),
                        allowed_resume_ids=resume_ids,
                        allowed_job_ids=job_ids,
                        gap_acceptances=[],
                    )

    def test_claim_ledger_accepts_only_current_candidate_acceptance_pairs(self):
        claim = rewrite_result().claim_audit[0].model_copy(
            update={
                "source_resume_ids": [],
                "claim_status": "verified",
                "gap_id": "gap-123456789abc",
                "acceptance_id": ACCEPTANCE_ID,
            }
        ).model_dump()
        acceptance = {
            "id": ACCEPTANCE_ID,
            "candidate_id": CANDIDATE_ID,
            "gap_id": "gap-123456789abc",
        }

        ledger = build_claim_ledger(
            rewrite_result(claims=[claim]),
            [RESUME_ID],
            [JOB_ID],
            [acceptance],
            candidate_id=CANDIDATE_ID,
        )
        self.assertEqual(ledger["claims"][0]["acceptance_id"], ACCEPTANCE_ID)

        rejected = (
            ([{**acceptance, "id": "gap_acceptance_" + "e" * 32}], CANDIDATE_ID),
            ([{**acceptance, "gap_id": "gap-aaaaaaaaaaaa"}], CANDIDATE_ID),
            ([{**acceptance, "candidate_id": "candidate_" + "f" * 32}], CANDIDATE_ID),
            ([acceptance, acceptance], CANDIDATE_ID),
            ([acceptance], None),
        )
        for acceptances, candidate_id in rejected:
            with self.subTest(acceptances=acceptances, candidate_id=candidate_id):
                with self.assertRaisesRegex(ValueError, "report input is invalid"):
                    build_claim_ledger(
                        rewrite_result(claims=[claim]),
                        [RESUME_ID],
                        [JOB_ID],
                        acceptances,
                        candidate_id=candidate_id,
                    )

        with self.assertRaisesRegex(ValueError, "report input is invalid"):
            build_claim_ledger(
                rewrite_result(claims=[claim, claim]),
                [RESUME_ID],
                [JOB_ID],
                [acceptance],
                candidate_id=CANDIDATE_ID,
            )

    def test_gap_roadmap_is_structured_sanitized_and_marks_commands_unexecuted(self):
        plan = GapAcceptancePlan.model_validate(
            {
                "gap_id": "gap-aaaaaaaaaaaa",
                "jd_requirements": ["Python token budget", "联系 candidate@example.com"],
                "project_scope": "回访 13812345678；参考 https://example.com/x?token=secret",
                "required_artifacts": [
                    "C:\\Users\\Alice\\private\\report.json",
                    "sk-proj_abcdefghijklmnopqrstuvwxyz123456",
                ],
                "test_commands": [
                    "python C:\\Users\\Alice\\private\\test.py --token super-secret",
                    "python /tmp/private/test.py",
                ],
                "acceptance_metrics": ["Authorization: Bearer ey.secret.value"],
                "interview_topics": [
                    "cookie=session-secret",
                    "token budget",
                    "回拨 (415) 555-2671",
                    "xoxb-123456789012-abcdefghijklmnop",
                ],
            }
        )

        roadmap = build_gap_roadmap([plan])

        self.assertIn("待验收", roadmap)
        self.assertIn("命令建议（未执行）", roadmap)
        self.assertIn("Python token budget", roadmap)
        self.assertIn("token budget", roadmap)
        for secret in (
            "candidate@example.com",
            "13812345678",
            "example.com",
            "super-secret",
            "session-secret",
            "ey.secret.value",
            "C:\\Users\\Alice",
            "cookie",
            "authorization",
            "sk-proj_",
            "xoxb-",
            "415",
            "/tmp/private",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret.casefold(), roadmap.casefold())

    def test_ats_report_computes_deduplicated_alias_coverage_without_raw_text(self):
        jobs = [
            {
                "id": JOB_ID,
                "candidate_id": CANDIDATE_ID,
                "title": "AI Agent Engineer",
                "detail": "PYTHON python；RAG rag 知识库",
                "source_metadata": {"cookie": "private-session"},
            }
        ]
        resumes = [
            {
                "id": RESUME_ID,
                "candidate_id": CANDIDATE_ID,
                "content": "Built Python services for candidate@example.com",
            }
        ]

        report = build_ats_report(
            jobs, resumes, "使用 PYTHON 构建 RAG 与智能体；13812345678"
        )

        self.assertIn("JD 样本关键词（3）：AI应用、Python、RAG", report)
        self.assertIn("原简历关键词（1）：Python", report)
        self.assertIn("修改后关键词（3）：AI应用、Python、RAG", report)
        self.assertIn("原简历覆盖：1/3（33.33%）", report)
        self.assertIn("修改后覆盖：3/3（100.00%）", report)
        self.assertIn("不是语义评分或通过承诺", report)
        for raw in (
            "candidate@example.com",
            "13812345678",
            "private-session",
            "Built Python services",
            "PYTHON python",
        ):
            self.assertNotIn(raw, report)

    def test_ats_report_has_ascii_boundaries_and_zero_denominator_safety(self):
        report = build_ats_report(
            [
                {
                    "id": JOB_ID,
                    "candidate_id": CANDIDATE_ID,
                    "title": "Management",
                    "detail": "rapid coverage",
                }
            ],
            [
                {
                    "id": RESUME_ID,
                    "candidate_id": CANDIDATE_ID,
                    "content": "management rapid coverage",
                }
            ],
            "management rapid coverage",
        )

        self.assertIn("JD 样本关键词（0）：无", report)
        self.assertIn("原简历覆盖：0/0（0.00%）", report)
        self.assertIn("修改后覆盖：0/0（0.00%）", report)

    def test_claim_ledger_redacts_broad_pii_and_credentials_but_keeps_token_term(self):
        claim = rewrite_result().claim_audit[0].model_copy(
            update={
                "claim": (
                    "Designed token budget; contact alice@example.io or "
                    "+1 (415) 555-2671; session_id=sensitive-session; "
                    "see https://example.com/private?debug=true"
                )
            }
        ).model_dump()

        ledger = build_claim_ledger(
            rewrite_result(claims=[claim]), [RESUME_ID], [JOB_ID], []
        )
        serialized = str(ledger)

        self.assertIn("token budget", serialized)
        for secret in (
            "alice@example.io",
            "415",
            "sensitive-session",
            "session_id",
            "example.com",
            "debug=true",
        ):
            self.assertNotIn(secret.casefold(), serialized.casefold())

if __name__ == "__main__":
    unittest.main()
