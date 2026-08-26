import re
import unittest

from resume_agent.rewrite_fact_gate import (
    InvalidModelResult,
    extract_business_metrics,
    validate_rewrite_facts,
)
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer


QUESTION_ID = re.compile(r"^(?:metric|gap|conflict)-[0-9a-f]{12}$")


def result_with_metric(status="unverified", basis=None):
    return CodexRewriteResult.model_validate(
        {
            "verdict": "匹配",
            "scorecard": [],
            "risks": [],
            "missing_information": [],
            "rewritten_resume_markdown": "# 曹申傲\n- 工具调用成功率达到 86%",
            "rewrite_reasons": [],
            "depth_recommendations": [],
            "interviewer_probes": [],
            "claim_audit": [
                {
                    "claim": "工具调用成功率达到 86%",
                    "source_resume_ids": ["source_resume_1"],
                    "source_quotes": ["工具调用成功率达到 86%"],
                    "job_ids": ["job_1"],
                    "change_type": "unchanged",
                    "fact_status": status,
                    "measurement_basis": basis,
                }
            ],
        }
    )


def empty_result(*, markdown="", missing_information=None):
    return CodexRewriteResult.model_validate(
        {
            "verdict": "匹配",
            "scorecard": [],
            "risks": [],
            "missing_information": missing_information or [],
            "rewritten_resume_markdown": markdown,
            "rewrite_reasons": [],
            "depth_recommendations": [],
            "interviewer_probes": [],
            "claim_audit": [],
        }
    )


class FactGateTests(unittest.TestCase):
    def setUp(self):
        self.resumes = [
            {"id": "source_resume_1", "content": "工具调用成功率达到 86%"}
        ]
        self.jobs = [{"id": "job_1"}]

    def test_source_text_without_measurement_basis_needs_facts(self):
        gate = validate_rewrite_facts(
            result_with_metric(), self.resumes, self.jobs, []
        )

        self.assertEqual(len(gate.blockers), 1)
        self.assertTrue(gate.blockers[0].question_id.startswith("metric-"))
        self.assertRegex(gate.blockers[0].question_id, QUESTION_ID)

    def test_candidate_answer_with_basis_unblocks_same_stable_question(self):
        first = validate_rewrite_facts(
            result_with_metric(), self.resumes, self.jobs, []
        )
        answers = [
            FactAnswer(
                question_id=first.blockers[0].question_id,
                answer="100 次固定回归中成功 86 次",
                source_note="本地回归记录，候选人确认",
            )
        ]

        gate = validate_rewrite_facts(
            result_with_metric(), self.resumes, self.jobs, answers
        )

        self.assertEqual(gate.blockers, [])

    def test_source_verified_metric_with_source_basis_passes(self):
        self.resumes[0]["content"] += "；统计口径为100次固定回归"

        gate = validate_rewrite_facts(
            result_with_metric("source_verified", "100次固定回归"),
            self.resumes,
            self.jobs,
            [],
        )

        self.assertEqual(gate.blockers, [])

    def test_unknown_source_id_is_invalid_model_result(self):
        result = result_with_metric("source_verified", "100次回归")
        result.claim_audit[0].source_resume_ids = ["source_resume_other"]

        with self.assertRaisesRegex(InvalidModelResult, "简历"):
            validate_rewrite_facts(result, self.resumes, self.jobs, [])

    def test_unknown_job_id_is_invalid_model_result(self):
        result = result_with_metric()
        result.claim_audit[0].job_ids = ["job_other"]

        with self.assertRaisesRegex(InvalidModelResult, "岗位"):
            validate_rewrite_facts(result, self.resumes, self.jobs, [])

    def test_source_quote_must_belong_to_the_mapped_resume(self):
        resumes = [
            {"id": "source_resume_1", "content": "简历一没有这句话"},
            {"id": "source_resume_2", "content": "工具调用成功率达到 86%"},
        ]

        with self.assertRaisesRegex(InvalidModelResult, "证据"):
            validate_rewrite_facts(result_with_metric(), resumes, self.jobs, [])

    def test_invented_measurement_basis_is_invalid_model_result(self):
        result = result_with_metric("source_verified", "100次固定回归")

        with self.assertRaisesRegex(InvalidModelResult, "measurement_basis"):
            validate_rewrite_facts(result, self.resumes, self.jobs, [])

    def test_measurement_basis_may_come_from_matching_candidate_answer(self):
        initial = validate_rewrite_facts(
            result_with_metric(), self.resumes, self.jobs, []
        )
        result = result_with_metric("user_confirmed", "100次固定回归")
        answers = [
            FactAnswer(
                question_id=initial.blockers[0].question_id,
                answer="统计口径是100次固定回归",
                source_note="候选人查阅本地回归记录后确认",
            )
        ]

        gate = validate_rewrite_facts(result, self.resumes, self.jobs, answers)

        self.assertEqual(gate.blockers, [])

    def test_user_confirmed_requires_matching_answer_with_source_note(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                {
                    "question_id": question_id,
                    "answer": "100次回归中成功86次",
                    "source_note": "   ",
                }
            ],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_model_blocking_question_ids_are_rebuilt_deterministically(self):
        result = empty_result(
            missing_information=[
                {
                    "question_id": "model-picked-id",
                    "question": "项目日期存在冲突，哪一个正确？",
                    "blocking": True,
                    "reason": "两个来源互相矛盾",
                },
                {
                    "question_id": "also-untrusted",
                    "question": "请补充部署环境",
                    "blocking": True,
                    "reason": "缺少事实依据",
                },
            ]
        )

        first = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        second = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        first_ids = [item.question_id for item in first.blockers]
        self.assertEqual(first_ids, [item.question_id for item in second.blockers])
        self.assertTrue(first_ids[0].startswith("conflict-"))
        self.assertTrue(first_ids[1].startswith("gap-"))
        self.assertTrue(all(QUESTION_ID.fullmatch(item) for item in first_ids))
        self.assertNotIn("model-picked-id", first.audit_markdown)

    def test_nonblocking_capability_gap_is_audited_without_blocking(self):
        result = empty_result(
            missing_information=[
                {
                    "question_id": "invented-id",
                    "question": "没有 Kubernetes 经历",
                    "blocking": False,
                    "reason": "岗位能力差距",
                }
            ]
        )

        gate = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        self.assertEqual(gate.blockers, [])
        self.assertIn("非阻断", gate.audit_markdown)
        self.assertRegex(gate.audit_markdown, r"gap-[0-9a-f]{12}")
        self.assertNotIn("invented-id", gate.audit_markdown)

    def test_candidate_answer_unblocks_rebuilt_model_gap_id(self):
        result = empty_result(
            missing_information=[
                {
                    "question_id": "model-id",
                    "question": "请补充部署环境",
                    "blocking": True,
                    "reason": "缺少事实依据",
                }
            ]
        )
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=initial.blockers[0].question_id,
                    answer="部署在候选人的本地 Windows 工作站",
                    source_note="候选人检查部署记录后确认",
                )
            ],
        )

        self.assertEqual(gate.blockers, [])

    def test_dates_phone_and_language_versions_are_not_business_metrics(self):
        result = empty_result(
            markdown=(
                "2027年毕业｜18895477216｜Python 3.12｜Java 21｜"
                "Node.js v20.11.1｜2024-01-03"
            )
        )

        gate = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        self.assertEqual(gate.blockers, [])

    def test_numeric_range_is_extracted_as_one_business_metric(self):
        claim = "每轮服务3-5个客户"

        self.assertEqual(extract_business_metrics(claim), [(claim, "3-5个")])

    def test_cao_shenao_fixture_gates_every_business_metric_stably(self):
        metrics = [
            "10+",
            "40+",
            "86%",
            "15秒",
            "200ms",
            "90%",
            "4个节点",
            "7种状态",
            "4个字段",
            "1024维",
        ]
        result = empty_result(
            markdown=(
                "服务10+个项目，覆盖40+场景，成功率86%，延迟从15秒降至200ms，"
                "覆盖率90%，编排4个节点、7种状态、4个字段和1024维向量"
            )
        )

        first = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        second = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        self.assertEqual(len(first.blockers), len(metrics))
        self.assertEqual(
            [item.question_id for item in first.blockers],
            [item.question_id for item in second.blockers],
        )
        questions = "\n".join(item.question for item in first.blockers)
        for metric in metrics:
            with self.subTest(metric=metric):
                self.assertIn(metric, questions)


if __name__ == "__main__":
    unittest.main()
