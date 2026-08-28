import json
import unittest

from resume_agent.rewrite_reports import (
    build_ats_report,
    build_claim_ledger,
    build_gap_roadmap,
    sanitize_report_text,
)
from resume_agent.rewrite_schemas import CodexRewriteResult, GapAcceptancePlan


RESUME_ID = "source_resume_" + "a" * 32
JOB_ID = "job_" + "b" * 32
CANDIDATE_ID = "candidate_" + "c" * 32
ACCEPTANCE_ID = "gap_acceptance_" + "d" * 32
OTHER_RESUME_ID = "source_resume_" + "e" * 32
OTHER_JOB_ID = "job_" + "f" * 32


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
    def test_report_sanitizer_redacts_bounded_credential_contexts(self):
        credential_inputs = (
            "token=topsecret",
            "Token: topsecret",
            "access token: topsecret",
            "API Key: topsecret",
            "OPENAI_API_KEY=topsecret",
            "BOSS_TOKEN=topsecret",
            "--token topsecret",
            "--api-key=topsecret",
            "Authorization: Bearer topsecret",
        )

        for value in credential_inputs:
            with self.subTest(value=value):
                sanitized = sanitize_report_text(value)
                self.assertNotIn("topsecret", sanitized.casefold())

        self.assertEqual(
            sanitize_report_text("tokenizer preserves the token budget"),
            "tokenizer preserves the token budget",
        )
        self.assertEqual(
            sanitize_report_text("tokenizer=topsecret; token budget"),
            "tokenizer=topsecret; token budget",
        )

    def test_report_sanitizer_decodes_percent_encoding_and_fails_closed(self):
        cases = (
            ("contact%2540example.com", "[REDACTED_EMAIL]"),
            (
                "https%3A%2F%2Fexample.com%2Fprivate%3Ftoken%3Dtopsecret",
                "[REDACTED_URL]",
            ),
            ("tok\u200ben%3Dtopsecret", "[REDACTED_CREDENTIAL]"),
            ("token%252525253Dtopsecret", "[REDACTED_UNSAFE]"),
            ("candidate%C3%28@example.com", "[REDACTED_UNSAFE]"),
            ("token%3Dtopsecret\ud800", "[REDACTED_UNSAFE]"),
            ("token%ZZ=topsecret", "[REDACTED_UNSAFE]"),
        )

        for value, expected in cases:
            with self.subTest(value=value):
                sanitized = sanitize_report_text(value)
                self.assertEqual(sanitized, expected)
                self.assertNotIn("topsecret", sanitized.casefold())

    def test_report_sanitizer_redacts_continuous_phone_numbers(self):
        for number in ("4155552671", "442071838750", "861381234567890"):
            with self.subTest(number=number):
                self.assertEqual(
                    sanitize_report_text(f"call {number}"),
                    "call [REDACTED_PHONE]",
                )

        self.assertEqual(
            sanitize_report_text("invoice 1234567890123456"),
            "invoice 1234567890123456",
        )

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

    def test_claim_ledger_bytes_are_stable_for_reversed_nonduplicate_sources(self):
        base = rewrite_result().claim_audit[0].model_dump()
        first = {
            **base,
            "claim": "Token budget",
            "source_resume_ids": [RESUME_ID],
            "job_ids": [JOB_ID],
        }
        second = {
            **base,
            "claim": "API architecture",
            "source_resume_ids": [OTHER_RESUME_ID],
            "job_ids": [OTHER_JOB_ID],
        }

        rendered = []
        for claims in ([first, second], [second, first]):
            ledger = build_claim_ledger(
                rewrite_result(claims=claims),
                [RESUME_ID, OTHER_RESUME_ID],
                [JOB_ID, OTHER_JOB_ID],
                [],
            )
            rendered.append(
                json.dumps(
                    ledger,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )

        self.assertEqual(rendered[0], rendered[1])

    def test_claim_ledger_rejects_claims_that_collide_after_sanitizing(self):
        base = rewrite_result().claim_audit[0].model_dump()
        claims = [
            {
                **base,
                "claim": "Contact alice@example.com",
                "source_resume_ids": [RESUME_ID],
                "job_ids": [JOB_ID],
            },
            {
                **base,
                "claim": "contact bob@example.org",
                "source_resume_ids": [OTHER_RESUME_ID],
                "job_ids": [OTHER_JOB_ID],
            },
        ]

        with self.assertRaisesRegex(ValueError, "^report input is invalid$") as caught:
            build_claim_ledger(
                rewrite_result(claims=claims),
                [RESUME_ID, OTHER_RESUME_ID],
                [JOB_ID, OTHER_JOB_ID],
                [],
            )

        self.assertNotIn("alice@example.com", repr(caught.exception))
        self.assertNotIn("bob@example.org", repr(caught.exception))

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

    def test_gap_roadmap_sanitizes_every_prose_field(self):
        plan = GapAcceptancePlan.model_validate(
            {
                "gap_id": "gap-bbbbbbbbbbbb",
                "jd_requirements": ["OPENAI_API_KEY=requirement-secret"],
                "project_scope": "contact%2540example.com",
                "required_artifacts": ["call 4155552671"],
                "test_commands": ["curl --token command-secret"],
                "acceptance_metrics": ["access token: acceptance-secret"],
                "interview_topics": [
                    "https%3A%2F%2Fexample.com%2Fprivate%3Ftoken%3Dtopic-secret"
                ],
            }
        )

        roadmap = build_gap_roadmap([plan])

        for secret in (
            "requirement-secret",
            "contact@example.com",
            "4155552671",
            "command-secret",
            "acceptance-secret",
            "topic-secret",
            "example.com",
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
