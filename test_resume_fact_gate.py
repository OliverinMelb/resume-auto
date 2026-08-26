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
            "rewritten_resume_markdown": "## 项目经历\n- 工具调用成功率达到 86%",
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


def result_for_claim(claim, *, status, quote=None, basis=None):
    return CodexRewriteResult.model_validate(
        {
            "verdict": "匹配",
            "scorecard": [],
            "risks": [],
            "missing_information": [],
            "rewritten_resume_markdown": f"## 项目经历\n- {claim}",
            "rewrite_reasons": [],
            "depth_recommendations": [],
            "interviewer_probes": [],
            "claim_audit": [
                {
                    "claim": claim,
                    "source_resume_ids": ["source_resume_1"] if quote else [],
                    "source_quotes": [quote] if quote else [],
                    "job_ids": ["job_1"],
                    "change_type": "unchanged",
                    "fact_status": status,
                    "measurement_basis": basis,
                }
            ],
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
            result_with_metric("user_confirmed"), self.resumes, self.jobs, []
        )
        answers = [
            FactAnswer(
                question_id=first.blockers[0].question_id,
                answer="100 次固定回归中成功 86 次",
                source_note="本地回归记录，候选人确认",
            )
        ]

        gate = validate_rewrite_facts(
            result_with_metric("user_confirmed"), self.resumes, self.jobs, answers
        )

        self.assertEqual(gate.blockers, [])

    def test_source_verified_metric_with_source_basis_passes(self):
        quote = "工具调用成功率达到 86%；统计口径为100次固定回归"
        self.resumes[0]["content"] = quote
        result = result_with_metric("source_verified", "100次固定回归")
        result.claim_audit[0].source_quotes = [quote]

        gate = validate_rewrite_facts(
            result,
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
                answer="100次固定回归中成功86次，成功率为86%",
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
        markdown = (
            "2027年6月毕业｜18895477216｜Python 3.12｜Java 21｜"
            "Node.js v20.11.1｜2024-01-03｜2027 年 6 月 1 日"
        )

        self.assertEqual(extract_business_metrics(markdown), [])

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

        first_metric_ids = [
            item.question_id
            for item in first.blockers
            if item.question_id.startswith("metric-")
        ]
        second_metric_ids = [
            item.question_id
            for item in second.blockers
            if item.question_id.startswith("metric-")
        ]
        self.assertEqual(len(first_metric_ids), len(metrics))
        self.assertEqual(
            first_metric_ids,
            second_metric_ids,
        )
        questions = "\n".join(
            item.question
            for item in first.blockers
            if item.question_id.startswith("metric-")
        )
        for metric in metrics:
            with self.subTest(metric=metric):
                self.assertIn(metric, questions)

    def test_unrelated_quote_cannot_support_a_rewritten_claim(self):
        claim = "主导 Kubernetes 集群迁移"
        result = empty_result(markdown=f"- {claim}")
        result.claim_audit = [
            result_with_metric().claim_audit[0].model_copy(
                update={
                    "claim": claim,
                    "source_resume_ids": ["source_resume_1"],
                    "source_quotes": ["熟悉 Python"],
                    "fact_status": "source_verified",
                    "measurement_basis": None,
                }
            )
        ]
        resumes = [
            {"id": "source_resume_1", "content": "技能：熟悉 Python"}
        ]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertTrue(any("关联" in item.reason for item in gate.blockers))

    def test_missing_claim_audit_cannot_be_repaired_by_metric_answer(self):
        claim = "工具调用成功率达到 86%"
        result = empty_result(markdown=f"- {claim}")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        metric_id = next(
            item.question_id
            for item in initial.blockers
            if item.question_id.startswith("metric-")
        )

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=metric_id,
                    answer="100次固定回归中成功86次",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertTrue(any("缺少 claim_audit" in item.reason for item in gate.blockers))

    def test_duplicate_claim_audit_cannot_be_repaired_by_metric_answer(self):
        result = result_with_metric()
        result.claim_audit.append(result.claim_audit[0].model_copy(deep=True))
        initial = validate_rewrite_facts(result_with_metric(), self.resumes, self.jobs, [])
        metric_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=metric_id,
                    answer="100次固定回归中成功86次",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertTrue(any("重复 claim_audit" in item.reason for item in gate.blockers))

    def test_unrelated_claim_audit_is_a_blocker(self):
        result = empty_result(markdown="- 主导 Kubernetes 集群迁移")
        result.claim_audit = [
            result_with_metric().claim_audit[0].model_copy(
                update={
                    "claim": "熟悉 Python",
                    "source_quotes": ["熟悉 Python"],
                    "fact_status": "source_verified",
                }
            )
        ]
        resumes = [{"id": "source_resume_1", "content": "熟悉 Python"}]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        reasons = "\n".join(item.reason for item in gate.blockers)
        self.assertIn("缺少 claim_audit", reasons)
        self.assertIn("未覆盖任何成稿实质行", reasons)

    def test_source_verified_claim_requires_owned_source_and_quote(self):
        claim = "熟悉 Python"
        result = empty_result(markdown=f"- {claim}")
        result.claim_audit = [
            result_with_metric().claim_audit[0].model_copy(
                update={
                    "claim": claim,
                    "source_resume_ids": [],
                    "source_quotes": [],
                    "fact_status": "source_verified",
                }
            )
        ]

        gate = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        self.assertTrue(any("source_verified" in item.reason for item in gate.blockers))

    def test_metric_and_basis_must_be_in_the_same_bound_quote(self):
        claim = "工具调用成功率达到 86%"
        result = result_with_metric("source_verified", "100次固定回归")
        result.claim_audit[0].source_quotes = ["工具调用采用 Python"]
        resumes = [
            {
                "id": "source_resume_1",
                "content": "工具调用采用 Python。其他位置提到100次固定回归。",
            }
        ]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertTrue(
            any(item.question_id.startswith("metric-") for item in gate.blockers)
        )

    def test_matching_and_unrelated_answers_cannot_compose_metric_evidence(self):
        result = result_with_metric("user_confirmed", "100次固定回归")
        initial = validate_rewrite_facts(result_with_metric(), self.resumes, self.jobs, [])
        metric_id = initial.blockers[0].question_id
        answers = [
            FactAnswer(
                question_id=metric_id,
                answer="候选人确认该成功率为86%",
                source_note="候选人确认，具体记录待查",
            ),
            FactAnswer(
                question_id="gap-aaaaaaaaaaaa",
                answer="统计口径为100次固定回归",
                source_note="本地回归记录，候选人确认",
            ),
        ]

        gate = validate_rewrite_facts(result, self.resumes, self.jobs, answers)

        self.assertTrue(
            any(item.question_id == metric_id for item in gate.blockers)
        )

    def test_placeholder_answer_and_source_note_do_not_unlock(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        metric_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                {
                    "question_id": metric_id,
                    "answer": "不知道",
                    "source_note": "帮我补上",
                }
            ],
        )

        self.assertTrue(any(item.question_id == metric_id for item in gate.blockers))

    def test_unrelated_statistical_formula_does_not_explain_current_metric(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        metric_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=metric_id,
                    answer="统计100次回归中成功90次",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertTrue(any(item.question_id == metric_id for item in gate.blockers))

    def test_different_metric_value_in_answer_does_not_unlock(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        metric_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=metric_id,
                    answer="候选人确认成功率为186%",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertTrue(any(item.question_id == metric_id for item in gate.blockers))

    def test_metric_value_is_not_a_substring_match_in_quote(self):
        result = result_with_metric("source_verified", "100次固定回归")
        quote = "历史报表显示成功率达到 186%，统计口径为100次固定回归"
        result.claim_audit[0].source_quotes = [quote]
        resumes = [{"id": "source_resume_1", "content": quote}]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertTrue(
            any(item.question_id.startswith("metric-") for item in gate.blockers)
        )

    def test_measurement_basis_is_not_a_numeric_substring_match(self):
        result = result_with_metric("source_verified", "100次固定回归")
        quote = "工具调用成功率达到 86%，统计口径为1100次固定回归"
        result.claim_audit[0].source_quotes = [quote]
        resumes = [
            {
                "id": "source_resume_1",
                "content": f"{quote}。另处记录100次固定回归",
            }
        ]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertTrue(
            any(item.question_id.startswith("metric-") for item in gate.blockers)
        )

    def test_extended_business_metric_vocabulary_and_nfkc(self):
        claim = "3年经验/管理10人/服务100万用户/成功率86％/峰值500 QPS"

        self.assertEqual(
            [metric for _, metric in extract_business_metrics(claim)],
            ["3年", "10人", "100万用户", "86%", "500 QPS"],
        )

    def test_every_substantive_line_needs_its_own_exact_audit(self):
        result = empty_result(
            markdown="# 技能\n- 熟悉 Python\n- 主导 Kubernetes 集群迁移"
        )
        result.claim_audit = [
            result_with_metric().claim_audit[0].model_copy(
                update={
                    "claim": "熟悉 Python",
                    "source_quotes": ["熟悉 Python"],
                    "fact_status": "source_verified",
                }
            )
        ]
        resumes = [{"id": "source_resume_1", "content": "熟悉 Python"}]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertEqual(
            sum("缺少 claim_audit" in item.reason for item in gate.blockers), 1
        )

    def test_general_user_confirmed_claim_uses_stable_gap_answer(self):
        claim = "负责内部发布流程"
        result = empty_result(markdown=f"- {claim}")
        result.claim_audit = [
            result_with_metric().claim_audit[0].model_copy(
                update={
                    "claim": claim,
                    "source_resume_ids": [],
                    "source_quotes": [],
                    "fact_status": "user_confirmed",
                    "measurement_basis": None,
                }
            )
        ]
        first = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        self.assertEqual(len(first.blockers), 1)
        self.assertTrue(first.blockers[0].question_id.startswith("gap-"))

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=first.blockers[0].question_id,
                    answer="候选人负责内部发布流程",
                    source_note="候选人确认，依据仓库发布记录",
                )
            ],
        )

        self.assertEqual(gate.blockers, [])

    def test_short_kubernetes_quote_does_not_support_full_migration_claim(self):
        claim = "主导 Kubernetes 集群迁移"
        result = result_for_claim(
            claim,
            status="source_verified",
            quote="Kubernetes",
        )
        resumes = [{"id": "source_resume_1", "content": "技能：Kubernetes"}]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertEqual(len(gate.blockers), 1)
        self.assertTrue(gate.blockers[0].question_id.startswith("gap-"))

    def test_short_python_quote_does_not_support_combined_claim(self):
        claim = "熟悉 Python 并主导 Kubernetes 集群迁移"
        result = result_for_claim(
            claim,
            status="source_verified",
            quote="熟悉 Python",
        )
        resumes = [{"id": "source_resume_1", "content": "技能：熟悉 Python"}]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertEqual(len(gate.blockers), 1)
        self.assertTrue(gate.blockers[0].question_id.startswith("gap-"))

    def test_only_allowlisted_section_headings_are_skipped(self):
        result = empty_result(markdown="# 高级架构师｜8年经验\n## 项目经历")

        gate = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        self.assertEqual(
            [metric for _, metric in extract_business_metrics(result.rewritten_resume_markdown)],
            ["8年"],
        )
        self.assertTrue(any("缺少 claim_audit" in item.reason for item in gate.blockers))

    def test_heading_allowlist_is_applied_after_stripping_hashes(self):
        section = validate_rewrite_facts(
            empty_result(markdown="##项目经历"), self.resumes, self.jobs, []
        )
        name = validate_rewrite_facts(
            empty_result(markdown="#曹申傲"), self.resumes, self.jobs, []
        )

        self.assertEqual(section.blockers, [])
        self.assertTrue(any("缺少 claim_audit" in item.reason for item in name.blockers))

    def test_unrelated_lunch_answer_does_not_confirm_general_claim(self):
        claim = "主导 Kubernetes 集群迁移"
        result = result_for_claim(claim, status="user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="午餐菜单是鸡肉沙拉",
                    source_note="候选人确认，依据记录",
                )
            ],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_percentage_answer_rejects_wrong_ratio(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="成功率86%，统计样本为86/85",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_percentage_answer_rejects_negated_matching_ratio(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="不是86/100，而是85/100",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_percentage_answer_accepts_reproducible_ratio(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="统计样本为86/100，即成功率86%",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertEqual(gate.blockers, [])

    def test_percentage_answer_accepts_equivalent_chinese_ratio_order(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="共100次固定回归，其中86次成功",
                    source_note="本地回归记录，候选人确认",
                )
            ],
        )

        self.assertEqual(gate.blockers, [])

    def test_percentage_confirmation_without_basis_is_rejected(self):
        result = result_with_metric("user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="候选人确认成功率为86%",
                    source_note="候选人确认，依据回归记录",
                )
            ],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_qps_answer_requires_exact_unit_and_throughput_method(self):
        claim = "接口峰值达到 500 QPS"
        result = result_for_claim(
            claim,
            status="user_confirmed",
            quote=claim,
        )
        resumes = [{"id": "source_resume_1", "content": claim}]
        initial = validate_rewrite_facts(result, resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        invalid_answers = ["500次回归均通过", "服务能力为500 QPS"]
        for answer in invalid_answers:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="压测报告和监控日志",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

        gate = validate_rewrite_facts(
            result,
            resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="压测峰值为500 QPS，持续10分钟",
                    source_note="压测报告和监控日志",
                )
            ],
        )
        self.assertEqual(gate.blockers, [])

    def test_source_verified_metric_cannot_borrow_candidate_basis(self):
        claim = "工具调用成功率达到 86%"
        basis = "100次固定回归"
        result = result_for_claim(
            claim,
            status="source_verified",
            quote=claim,
            basis=basis,
        )
        resumes = [
            {
                "id": "source_resume_1",
                "content": f"{claim}。其他位置记录{basis}",
            }
        ]
        initial = validate_rewrite_facts(result, resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id
        answers = [
            FactAnswer(
                question_id=question_id,
                answer="100次固定回归中成功86次，成功率为86%",
                source_note="本地回归记录，候选人确认",
            )
        ]

        source_gate = validate_rewrite_facts(result, resumes, self.jobs, answers)
        result.claim_audit[0].fact_status = "user_confirmed"
        user_gate = validate_rewrite_facts(result, resumes, self.jobs, answers)

        self.assertEqual(
            [item.question_id for item in source_gate.blockers], [question_id]
        )
        self.assertEqual(user_gate.blockers, [])

    def test_other_metric_needs_exact_value_and_method_in_answer(self):
        claim = "管理10人团队"
        result = result_for_claim(
            claim,
            status="user_confirmed",
            quote=claim,
        )
        resumes = [{"id": "source_resume_1", "content": claim}]
        initial = validate_rewrite_facts(result, resumes, self.jobs, [])
        question_id = initial.blockers[0].question_id

        weak = validate_rewrite_facts(
            result,
            resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="候选人确认管理10人",
                    source_note="候选人确认，依据项目记录",
                )
            ],
        )
        strong = validate_rewrite_facts(
            result,
            resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="团队清单记录管理10人",
                    source_note="候选人确认，依据项目记录",
                )
            ],
        )

        self.assertEqual([item.question_id for item in weak.blockers], [question_id])
        self.assertEqual(strong.blockers, [])

    def test_chinese_scale_and_currency_metrics_table(self):
        positives = {
            "服务上千用户": "上千用户",
            "覆盖百余家企业": "百余家",
            "管理数十人": "数十人",
            "完成近百项任务": "近百项",
            "执行约百次回归": "约百次",
            "单价500元": "500元",
            "节省50万元": "50万元",
        }
        for claim, expected in positives.items():
            with self.subTest(claim=claim):
                self.assertEqual(
                    [metric for _, metric in extract_business_metrics(claim)],
                    [expected],
                )

        negatives = [
            "2027 年 6 月 1 日",
            "18895477216",
            "Python 3.12",
            "Java 21",
            "Node.js v20.11.1",
        ]
        for claim in negatives:
            with self.subTest(claim=claim):
                self.assertEqual(extract_business_metrics(claim), [])


if __name__ == "__main__":
    unittest.main()
