import re
import unittest

import resume_agent.rewrite_fact_gate as rewrite_fact_gate
from resume_agent.rewrite_fact_gate import (
    InvalidModelResult,
    extract_business_metrics,
    validate_rewrite_facts,
)
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer as FactAnswerModel


QUESTION_ID = re.compile(r"^(?:metric|gap|conflict)-[0-9a-f]{12}$")
CONFIRMED_AT = "2026-08-26T01:30:00Z"


def FactAnswer(**values):
    return FactAnswerModel(
        user_confirmed=True,
        confirmed_at=CONFIRMED_AT,
        **values,
    )


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
                    "claim_status": "verified",
                }
            ],
            "gap_acceptance_plans": [],
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
            "gap_acceptance_plans": [],
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
                    "claim_status": "verified",
                }
            ],
            "gap_acceptance_plans": [],
        }
    )


def in_progress_result(
    claim,
    *,
    markdown=None,
    gap_id="gap-aaaaaaaaaaaa",
    test_commands=None,
):
    return CodexRewriteResult.model_validate(
        {
            "verdict": "匹配",
            "scorecard": [],
            "risks": [],
            "missing_information": [],
            "rewritten_resume_markdown": (
                markdown
                if markdown is not None
                else f"## 在建项目（待验收）\n- {claim}"
            ),
            "rewrite_reasons": [],
            "depth_recommendations": [],
            "interviewer_probes": [],
            "claim_audit": [
                {
                    "claim": claim,
                    "source_resume_ids": [],
                    "source_quotes": [],
                    "job_ids": ["job_1"],
                    "change_type": "strengthened",
                    "fact_status": "unverified",
                    "measurement_basis": None,
                    "claim_status": "in_progress",
                    "gap_id": gap_id,
                    "acceptance_id": None,
                }
            ],
            "gap_acceptance_plans": [
                {
                    "gap_id": gap_id,
                    "jd_requirements": ["LangGraph 多 Agent 路由"],
                    "project_scope": "实现本地路由原型并以固定回归集验收",
                    "required_artifacts": ["代码仓库", "回归报告"],
                    "test_commands": test_commands or ["python -m unittest -v"],
                    "acceptance_metrics": ["固定回归集全部通过"],
                    "interview_topics": ["路由设计"],
                }
            ],
        }
    )


def accepted_result(
    claim,
    *,
    acceptance_id="gap_acceptance_123",
    gap_id="gap-aaaaaaaaaaaa",
):
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
                    "source_resume_ids": [],
                    "source_quotes": [],
                    "job_ids": ["job_1"],
                    "change_type": "strengthened",
                    "fact_status": "user_confirmed",
                    "measurement_basis": None,
                    "claim_status": "verified",
                    "gap_id": gap_id,
                    "acceptance_id": acceptance_id,
                }
            ],
            "gap_acceptance_plans": [],
        }
    )


def accepted_record(
    *claims,
    acceptance_id="gap_acceptance_123",
    gap_id="gap-aaaaaaaaaaaa",
):
    return {
        "id": acceptance_id,
        "candidate_id": "candidate_1",
        "gap_id": gap_id,
        "evidence": {
            "gap_id": gap_id,
            "project_path": "C:/work/agent-eval",
            "verified_claims": list(claims),
            "artifact_hashes": {"report.json": "a" * 64},
            "test_records": [
                {
                    "command": "python -m unittest -v",
                    "exit_code": 0,
                    "output_sha256": "b" * 64,
                }
            ],
            "completed_at": "2026-08-26T03:00:00Z",
            "user_confirmed": True,
            "confirmed_at": "2026-08-26T03:01:00Z",
        },
    }


class FactGateTests(unittest.TestCase):
    def setUp(self):
        self.resumes = [
            {"id": "source_resume_1", "content": "工具调用成功率达到 86%"}
        ]
        self.jobs = [{"id": "job_1"}]

    def assertInProgressBlocked(self, gate):
        self.assertTrue(gate.blockers)
        self.assertTrue(
            any("在建" in item.reason or "in_progress" in item.reason for item in gate.blockers),
            [item.reason for item in gate.blockers],
        )

    def test_validate_in_progress_claims_is_a_public_gate_helper(self):
        self.assertTrue(
            callable(getattr(rewrite_fact_gate, "validate_in_progress_claims", None))
        )

    def test_in_progress_claim_language_table(self):
        cases = [
            ("在建：正在实现LangGraph多Agent路由，计划以回归集验收", True),
            ("在建：正在实现路由，计划以上线前回归集验收", True),
            ("在建：正在实现路由，计划验收交付物完整性", True),
            ("在建：正在实现路由，计划以100条固定回归集验收", True),
            ("计划成功发布候选构建后验收，正在实现发布流程", True),
            ("正在实现路由并计划实现自动验收", True),
            ("主导LangGraph多Agent平台上线", False),
            ("为客户上线RAG系统", False),
            ("准确率提升到92%", False),
            ("节省人工成本40%", False),
            ("项目现\u200b已\u3000完 成并交 付", False),
            ("面向甲\u200b方生产用户部署", False),
            ("在建：正在为[客](https://example.test)户实现RAG，计划验收", False),
            ("在建：项目已完\ufe0f成，待验收", False),
            ("在建：已经完成核心模块，待验收", False),
            ("在建：核心模块已完工，待验收", False),
            ("在建：成功发布核心模块，待验收", False),
            ("在建：核心模块已投入使用，待验收", False),
            ("在建：系统稳定运行，待验收", False),
            ("在建：系统正式投产，待验收", False),
            ("在建：服务真实用户，待验收", False),
            ("在建：转化率显著提升，待验收", False),
            ("在建：可用性达到四个九，待验收", False),
            ("在建：日活增长一倍，待验收", False),
            ("在建：已&#23436;&#25104;核心模块，待验收", False),
            ("在建：已<span></span>完成核心模块，待验收", False),
            ("核心模块开发完成，正在补充测试", False),
            ("核心能力已实现，正在补充测试", False),
            ("已经部署至线上环境，正在补充测试", False),
            ("已经运行在线上，正在补充测试", False),
            ("计划后续重构但核心模块开发完成正在补充测试", False),
            ("计划后续重构然而核心能力实现完毕正在补充测试", False),
            ("计划后续重构可是核心模块开发完成正在补充测试", False),
            ("计划后续重构却核心能力实现完毕正在补充测试", False),
            ("计划后续重构而核心模块开发完成正在补充测试", False),
            ("核心功能完成了，正在补充测试", False),
            ("核心模块已经做完，正在补充测试", False),
            ("核心模块开发完，正在补充测试", False),
            ("核心能力实现完毕，正在补充测试", False),
            ("核心能力实现完，正在补充测试", False),
            ("系统运行于线上，正在补充测试", False),
            ("核心服务部署在线上，正在补充测试", False),
            ("在建：核心功能全部完成，正在补充测试，计划验收", False),
            ("核心模块开发完正在补充测试", False),
            ("系统计划部署在线上后验收，正在实现发布流程", True),
            ("系统部署在线上，正在迭代", False),
            ("在建：核心功能完成度仍待提升，正在补充测试，计划验收", True),
            ("在建：核心功能未完成，正在补充测试，计划验收", True),
            ("在建：核心功能待完成，正在补充测试，计划验收", True),
            ("在建：核心模块开发未完成，正在补充测试，计划验收", True),
            ("核心模块开发完成计划上线后验收正在迭代", False),
            ("核心模块构建完成计划交付后验收正在迭代", False),
            ("核心模块计划上线后开发完成正在迭代", False),
            ("核心模块开发完成，计划上线后验收，正在迭代", False),
            ("核心模块计划开发完成计划上线后验收正在迭代", True),
            ("核心模块计划开发完成，计划上线后验收，正在迭代", True),
            ("核心模块已完成计划部署在线上后验收正在迭代", False),
            ("核心模块计划开发完成后继续补充回归测试最终上线正在迭代", False),
            ("系统计划未完成就部署在线上后验收正在实现", True),
            ("系统计划待完成后部署在线上验收正在实现", True),
            ("计划在上线前完成回归，正在实现路由", True),
            ("计划发布候选构建并完成验收，正在实现流程", True),
            ("计划实现并完成核心模块，正在迭代", True),
            ("在建：正在负责实现核心模块，计划验收", True),
            ("效率翻倍，正在优化", False),
            ("成本减半，正在验证", False),
            ("计划已上线的系统继续迭代，正在实现新路由", False),
        ]
        for claim, allowed in cases:
            with self.subTest(claim=claim):
                gate = validate_rewrite_facts(
                    in_progress_result(claim), self.resumes, self.jobs, []
                )
                if allowed:
                    self.assertEqual(gate.blockers, [])
                else:
                    self.assertInProgressBlocked(gate)

    def test_in_progress_heading_is_unique_explicit_and_level_scoped(self):
        claim = "在建：正在实现LangGraph多Agent路由，计划以回归集验收"
        cases = [
            (f"在建项目（待验收）\n- {claim}", False),
            (f"## 项目经历中的在建项目（待验收）说明\n- {claim}", False),
            (f"### 在建项目（待验收）\n## 项目经历\n- {claim}", False),
            (
                f"## 在建项目（待验收）\n- {claim}\n"
                f"### 在 建 项目（ 待 验收 ）\n- 说明",
                False,
            ),
            (f"###\u3000在 建 项目（ 待 验收 ）\n- {claim}", True),
            (f"## 在建项目（待验收） ##\n- {claim}", True),
        ]
        for markdown, allowed in cases:
            with self.subTest(markdown=markdown):
                gate = validate_rewrite_facts(
                    in_progress_result(claim, markdown=markdown),
                    self.resumes,
                    self.jobs,
                    [],
                )
                if allowed:
                    self.assertEqual(gate.blockers, [])
                else:
                    self.assertInProgressBlocked(gate)

    def test_in_progress_claim_repeated_outside_section_is_blocked(self):
        claim = "在建：正在实现LangGraph多Agent路由，计划以回归集验收"
        markdown = (
            f"## 在建项目（待验收）\n- {claim}\n"
            f"## 项目经历\n- {claim}"
        )

        gate = validate_rewrite_facts(
            in_progress_result(claim, markdown=markdown), self.resumes, self.jobs, []
        )

        self.assertInProgressBlocked(gate)

    def test_rendered_claim_identity_blocks_unicode_and_markdown_collisions(self):
        visible = "在建：正在实现LangGraph多Agent路由，计划验收"
        cases = [
            (
                "在建：正\u200b在实现LangGraph多Agent路由，计划验收",
                visible,
            ),
            (
                visible,
                f"[{visible}](https://example.test/pending)",
            ),
            (
                f"[{visible}](https://example.test/a_(b))",
                visible,
            ),
            (
                "在建：正在实现LangGraph多&#65;gent路由，计划验收",
                visible,
            ),
        ]
        for pending_claim, outside_claim in cases:
            with self.subTest(pending_claim=pending_claim, outside_claim=outside_claim):
                markdown = (
                    f"## 在建项目（待验收）\n- {pending_claim}\n"
                    f"## 项目经历\n- {outside_claim}"
                )
                result = in_progress_result(pending_claim, markdown=markdown)
                verified_audit = result.claim_audit[0].model_copy(
                    update={
                        "claim": outside_claim,
                        "claim_status": "verified",
                        "gap_id": None,
                        "source_resume_ids": ["source_resume_1"],
                        "source_quotes": [outside_claim],
                        "fact_status": "source_verified",
                    }
                )
                result = result.model_copy(
                    update={"claim_audit": [result.claim_audit[0], verified_audit]}
                )
                resumes = [{"id": "source_resume_1", "content": outside_claim}]

                gate = validate_rewrite_facts(result, resumes, self.jobs, [])

                self.assertInProgressBlocked(gate)

    def test_rendered_heading_identity_rejects_wrapped_duplicate_section(self):
        claim = "在建：正在实现LangGraph多Agent路由，计划验收"
        headings = [
            "[在建项目（待验收）](https://example.test/section)",
            "[在建项目（待验收）](https://example.test/a_(b))",
            "在建项目（待&#39564;收）",
            "在建项\u200b目（待验收）",
        ]
        for duplicate_heading in headings:
            with self.subTest(duplicate_heading=duplicate_heading):
                markdown = (
                    f"## 在建项目（待验收）\n- {claim}\n"
                    f"## {duplicate_heading}"
                )
                result = in_progress_result(claim, markdown=markdown)
                verified_audit = result.claim_audit[0].model_copy(
                    update={
                        "claim": duplicate_heading,
                        "claim_status": "verified",
                        "gap_id": None,
                        "source_resume_ids": ["source_resume_1"],
                        "source_quotes": [duplicate_heading],
                        "fact_status": "source_verified",
                    }
                )
                result = result.model_copy(
                    update={"claim_audit": [result.claim_audit[0], verified_audit]}
                )
                resumes = [{"id": "source_resume_1", "content": duplicate_heading}]

                gate = validate_rewrite_facts(result, resumes, self.jobs, [])

                self.assertInProgressBlocked(gate)

    def test_source_evidence_identity_preserves_link_target_and_html_facts(self):
        cases = [
            (
                "访问[项目主页](https://evil.test/a_(b))",
                "访问[项目主页](https://trusted.test/a_(b))",
            ),
            ("使用 List<Foo> 处理请求", "使用 List<Bar> 处理请求"),
        ]
        for claim, source_quote in cases:
            with self.subTest(claim=claim):
                result = result_for_claim(
                    claim, status="source_verified", quote=source_quote
                )
                resumes = [
                    {"id": "source_resume_1", "content": source_quote}
                ]

                gate = validate_rewrite_facts(result, resumes, self.jobs, [])

                self.assertTrue(gate.blockers)
                self.assertTrue(
                    any("source_verified" in item.reason for item in gate.blockers),
                    [item.reason for item in gate.blockers],
                )

    def test_in_progress_section_rejects_non_in_progress_audit(self):
        claim = "在建：正在实现LangGraph多Agent路由，计划以回归集验收"
        result = in_progress_result(claim)
        verified_audit = result.claim_audit[0].model_copy(
            update={
                "claim_status": "verified",
                "gap_id": None,
                "source_resume_ids": ["source_resume_1"],
                "source_quotes": [claim],
                "fact_status": "source_verified",
            }
        )
        result = result.model_copy(
            update={"claim_audit": [verified_audit], "gap_acceptance_plans": []}
        )
        resumes = [{"id": "source_resume_1", "content": claim}]

        gate = validate_rewrite_facts(result, resumes, self.jobs, [])

        self.assertInProgressBlocked(gate)

    def test_in_progress_claim_requires_one_distinct_matching_gap_plan(self):
        claim = "在建：正在实现LangGraph多Agent路由，计划以回归集验收"
        result = in_progress_result(claim)
        mismatched_plan = result.gap_acceptance_plans[0].model_copy(
            update={"gap_id": "gap-bbbbbbbbbbbb"}
        )
        mismatched = result.model_copy(update={"gap_acceptance_plans": [mismatched_plan]})
        duplicate_audit = result.claim_audit[0].model_copy(
            update={"claim": "正在实现固定回归集并计划验收"}
        )
        reused = result.model_copy(
            update={
                "rewritten_resume_markdown": (
                    f"## 在建项目（待验收）\n- {claim}\n"
                    "- 正在实现固定回归集并计划验收"
                ),
                "claim_audit": [result.claim_audit[0], duplicate_audit],
            }
        )

        for candidate in (mismatched, reused):
            with self.subTest(candidate=candidate):
                gate = validate_rewrite_facts(candidate, self.resumes, self.jobs, [])
                self.assertInProgressBlocked(gate)

    def test_fact_answer_cannot_unlock_invalid_in_progress_claim(self):
        claim = "实现LangGraph多Agent路由"
        result = in_progress_result(claim)
        first = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        self.assertInProgressBlocked(first)
        answer = FactAnswer(
            question_id=first.blockers[0].question_id,
            answer=claim,
            source_note="本地代码仓库记录，候选人确认",
        )

        answered = validate_rewrite_facts(
            result, self.resumes, self.jobs, [answer]
        )

        self.assertInProgressBlocked(answered)
        self.assertEqual(
            [item.question_id for item in answered.blockers],
            [item.question_id for item in first.blockers],
        )

    def test_accepted_gap_claim_requires_exact_record_and_gap_pair(self):
        claim = "实现本地 Agent 评测集"
        acceptance = accepted_record(claim)

        gate = validate_rewrite_facts(
            accepted_result(claim), self.resumes, self.jobs, [], [acceptance]
        )

        self.assertEqual(gate.blockers, [])
        for result in (
            accepted_result(claim, acceptance_id="gap_acceptance_missing"),
            accepted_result(claim, gap_id="gap-bbbbbbbbbbbb"),
        ):
            with self.subTest(result=result), self.assertRaises(InvalidModelResult):
                validate_rewrite_facts(
                    result, self.resumes, self.jobs, [], [acceptance]
                )

    def test_accepted_gap_claim_must_equal_one_complete_verified_assertion(self):
        acceptance = accepted_record("实现本地 Agent 评测集")
        invalid_claims = [
            "主导实现本地 Agent 评测集",
            "实现本地 Agent 评测集，准确率达到 92%",
            "C:/work/agent-eval",
            "python -m unittest -v",
        ]

        for claim in invalid_claims:
            with self.subTest(claim=claim), self.assertRaises(InvalidModelResult):
                validate_rewrite_facts(
                    accepted_result(claim), self.resumes, self.jobs, [], [acceptance]
                )

    def test_accepted_gap_allows_only_listed_metric_and_no_external_production_fact(self):
        measured = "固定 100 条回归集通过 100 条"
        measured_gate = validate_rewrite_facts(
            accepted_result(measured),
            self.resumes,
            self.jobs,
            [],
            [accepted_record(measured)],
        )
        self.assertEqual(measured_gate.blockers, [])

        forbidden_claims = [
            "为客户上线 RAG 系统",
            "项目已部署到生产环境并服务真实用户",
        ]
        for claim in forbidden_claims:
            with self.subTest(claim=claim), self.assertRaises(InvalidModelResult):
                validate_rewrite_facts(
                    accepted_result(claim),
                    self.resumes,
                    self.jobs,
                    [],
                    [accepted_record(claim)],
                )

    def test_promoted_claim_policy_scans_markdown_urls_and_rejects_html(self):
        safe_link = "实现[本地 Agent 评测](https://example.test/project)"
        gate = validate_rewrite_facts(
            accepted_result(safe_link),
            self.resumes,
            self.jobs,
            [],
            [accepted_record(safe_link)],
        )
        self.assertEqual(gate.blockers, [])

        forbidden_claims = [
            "实现[本地 Agent 评测](https://example.test/客户)",
            "实现[本地 Agent 评测](https://example.test/%E5%AE%A2%E6%88%B7)",
            "实现[本地 Agent 评测](https://example.test/?role=%E7%94%B2%E6%96%B9)",
            "实现[本地 Agent 评测](https://example.test/%E4%B8%8A%E7%BA%BF)",
            '<span data-owner="客户">实现本地 Agent 评测</span>',
            "<strong>实现本地 Agent 评测</strong>",
            (
                "&lt;span data-target=&quot;生产环境&quot;&gt;"
                "实现本地 Agent 评测&lt;/span&gt;"
            ),
        ]
        for claim in forbidden_claims:
            with self.subTest(claim=claim), self.assertRaises(InvalidModelResult):
                validate_rewrite_facts(
                    accepted_result(claim),
                    self.resumes,
                    self.jobs,
                    [],
                    [accepted_record(claim)],
                )

    def test_promoted_claim_policy_rejects_english_external_facts_by_word(self):
        forbidden_claims = [
            "Built the evaluator for a client",
            "Delivered the customer integration",
            "Implemented the company platform",
            "Runs in production",
            "Serves real users",
            "Launched the Agent evaluator",
            "Deployed the Agent evaluator",
            "Built [the evaluator](https://example.test/%63%6C%69%65%6E%74)",
            "See <https://example.test/real/users> for documentation",
            "Read [docs](https://example.test/real%2Fusers)",
        ]
        for claim in forbidden_claims:
            with self.subTest(claim=claim), self.assertRaises(InvalidModelResult):
                validate_rewrite_facts(
                    accepted_result(claim),
                    self.resumes,
                    self.jobs,
                    [],
                    [accepted_record(claim)],
                )

        safe_claims = [
            "Implemented a client-side library",
            "Added accompanying evaluator documentation",
            "Implemented deployment planning",
        ]
        for claim in safe_claims:
            with self.subTest(claim=claim):
                gate = validate_rewrite_facts(
                    accepted_result(claim),
                    self.resumes,
                    self.jobs,
                    [],
                    [accepted_record(claim)],
                )
                self.assertEqual(gate.blockers, [])

    def test_promoted_claim_html_detection_preserves_code_generics_and_autolinks(self):
        safe_claims = [
            "Used List<Foo> in the local evaluator",
            "Used Map<String, Foo> in the local evaluator",
            "Validated x < y-z > 0 in local tests",
            "Documented `x<y-z>0` comparison behavior",
            "Documented `<span data-mode=\"safe\">` parser behavior",
            "See <https://example.test/evaluator> for local documentation",
        ]
        for claim in safe_claims:
            with self.subTest(claim=claim):
                gate = validate_rewrite_facts(
                    accepted_result(claim),
                    self.resumes,
                    self.jobs,
                    [],
                    [accepted_record(claim)],
                )
                self.assertEqual(gate.blockers, [])

        forbidden_claims = [
            "<span>Implemented the local evaluator</span>",
            "<!-- hidden -->Implemented the local evaluator",
            '<x-evaluator data-mode="safe">Implemented locally</x-evaluator>',
            "<x-evaluator/>",
            "<X-EVALUATOR>",
            "<x-evaluator>Implemented locally</x-evaluator>",
            "Implemented locally</x-evaluator>",
            "before <x-evaluator> after",
            "a<x-evaluator>b",
            "a</x-evaluator>b",
            "a<x-evaluator/>b",
            "Validated x<y-z>0 in local tests",
            "Checked left_value<right-value>result locally",
            '<span data-mode="safe">Implemented locally</span>',
            "See <https://example.test/%63%6C%69%65%6E%74> for documentation",
        ]
        for claim in forbidden_claims:
            with self.subTest(claim=claim), self.assertRaises(InvalidModelResult):
                validate_rewrite_facts(
                    accepted_result(claim),
                    self.resumes,
                    self.jobs,
                    [],
                    [accepted_record(claim)],
                )

    def test_unaccepted_gap_cannot_be_promoted(self):
        with self.assertRaises(InvalidModelResult):
            validate_rewrite_facts(
                accepted_result("实现本地 Agent 评测集"),
                self.resumes,
                self.jobs,
                [],
                [],
            )

    def test_promoted_gap_cannot_also_remain_in_progress(self):
        promoted_claim = "实现本地 Agent 评测集"
        pending_claim = "正在实现评测报告页面，计划验收"
        promoted = accepted_result(promoted_claim)
        pending = in_progress_result(pending_claim)
        combined = promoted.model_copy(
            update={
                "rewritten_resume_markdown": (
                    f"## 项目经历\n- {promoted_claim}\n"
                    f"## 在建项目（待验收）\n- {pending_claim}"
                ),
                "claim_audit": [
                    promoted.claim_audit[0], pending.claim_audit[0]
                ],
                "gap_acceptance_plans": pending.gap_acceptance_plans,
            }
        )

        with self.assertRaises(InvalidModelResult):
            validate_rewrite_facts(
                combined,
                self.resumes,
                self.jobs,
                [],
                [accepted_record(promoted_claim)],
            )

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
                answer="工具调用的100 次固定回归中成功 86 次",
                source_note="本地回归记录，候选人确认",
            )
        ]

        gate = validate_rewrite_facts(
            result_with_metric("user_confirmed"), self.resumes, self.jobs, answers
        )

        self.assertEqual(gate.blockers, [])

    def test_bare_candidate_confirmation_does_not_unlock_metric(self):
        first = validate_rewrite_facts(
            result_with_metric("user_confirmed"), self.resumes, self.jobs, []
        )
        question_id = first.blockers[0].question_id

        gate = validate_rewrite_facts(
            result_with_metric("user_confirmed"),
            self.resumes,
            self.jobs,
            [{
                "question_id": question_id,
                "answer": "工具调用的100 次固定回归中成功 86 次",
                "source_note": "本地回归记录，候选人确认",
            }],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_explicit_confirmation_without_concrete_source_does_not_unlock_metric(self):
        first = validate_rewrite_facts(
            result_with_metric("user_confirmed"), self.resumes, self.jobs, []
        )
        question_id = first.blockers[0].question_id

        gate = validate_rewrite_facts(
            result_with_metric("user_confirmed"),
            self.resumes,
            self.jobs,
            [FactAnswer(
                question_id=question_id,
                answer="工具调用的100次固定回归中成功86次",
                source_note="候选人确认该答案属实",
            )],
        )

        self.assertEqual([item.question_id for item in gate.blockers], [question_id])

    def test_negated_or_lost_source_notes_do_not_unlock_metric(self):
        first = validate_rewrite_facts(
            result_with_metric("user_confirmed"), self.resumes, self.jobs, []
        )
        question_id = first.blockers[0].question_id

        for source_note in (
            "候选人确认，没有记录",
            "候选人确认，记录已遗失",
            "没有日志，仅候选人确认",
        ):
            with self.subTest(source_note=source_note):
                gate = validate_rewrite_facts(
                    result_with_metric("user_confirmed"),
                    self.resumes,
                    self.jobs,
                    [FactAnswer(
                        question_id=question_id,
                        answer="工具调用的100次固定回归中成功86次",
                        source_note=source_note,
                    )],
                )

                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

    def test_source_verified_metric_with_source_basis_passes(self):
        basis = "100次固定回归中成功86次"
        quote = f"工具调用成功率达到 86%，统计口径为{basis}"
        self.resumes[0]["content"] = quote
        result = result_with_metric("source_verified", basis)
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
                answer="工具调用100次固定回归中成功86次，成功率为86%",
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
                    answer="工具调用统计样本为86/100，即成功率86%",
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
                    answer="工具调用共100次固定回归，其中86次成功",
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
                    answer="接口压测峰值为500 QPS，监控窗口持续10分钟",
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
                answer="工具调用100次固定回归中成功86次，成功率为86%",
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

    def test_mixed_metric_claim_needs_one_quote_supporting_subject_metric_and_basis(self):
        claim = "主导 Kubernetes 迁移，成功率86%"
        quote = "模型准确率86%，口径100次测试"
        result = result_for_claim(
            claim,
            status="source_verified",
            quote=quote,
            basis="100次测试",
        )

        gate = validate_rewrite_facts(
            result,
            [{"id": "source_resume_1", "content": quote}],
            self.jobs,
            [],
        )

        self.assertNotEqual(gate.blockers, [])

    def test_collective_actor_cannot_support_individual_role_claim(self):
        claim = "主导 Kubernetes 集群迁移"
        quote = "团队主导 Kubernetes 集群迁移"
        result = result_for_claim(claim, status="source_verified", quote=quote)

        gate = validate_rewrite_facts(
            result,
            [{"id": "source_resume_1", "content": quote}],
            self.jobs,
            [],
        )

        self.assertNotEqual(gate.blockers, [])

    def test_candidate_denial_does_not_confirm_nonmetric_claim(self):
        claim = "负责内部发布流程"
        result = result_for_claim(claim, status="user_confirmed")
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=initial.blockers[0].question_id,
                    answer="候选人不负责内部发布流程",
                    source_note="候选人确认，依据职责记录",
                )
            ],
        )

        self.assertNotEqual(gate.blockers, [])

    def test_percentage_value_cannot_double_as_measurement_basis(self):
        claim = "工具调用成功率达到86%"
        result = result_for_claim(
            claim,
            status="source_verified",
            quote=claim,
            basis="86%",
        )

        gate = validate_rewrite_facts(
            result,
            [{"id": "source_resume_1", "content": claim}],
            self.jobs,
            [],
        )

        self.assertNotEqual(gate.blockers, [])

    def test_model_blocker_answers_cannot_borrow_another_questions_fact(self):
        result = empty_result(
            missing_information=[
                {
                    "question_id": "education-id",
                    "question": "请确认最高学历",
                    "blocking": True,
                    "reason": "缺少教育事实",
                },
                {
                    "question_id": "employment-id",
                    "question": "请确认入职日期",
                    "blocking": True,
                    "reason": "缺少任职日期",
                },
            ]
        )
        initial = validate_rewrite_facts(result, self.resumes, self.jobs, [])
        education_id, employment_id = [item.question_id for item in initial.blockers]

        gate = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=education_id,
                    answer="入职日期为2023年3月",
                    source_note="候选人确认，依据劳动合同记录",
                ),
                FactAnswer(
                    question_id=employment_id,
                    answer="最高学历为硕士",
                    source_note="候选人确认，依据毕业证记录",
                ),
            ],
        )

        self.assertEqual(
            [item.question_id for item in gate.blockers],
            [education_id, employment_id],
        )

    def test_source_role_claim_uses_clause_local_actor_and_polarity(self):
        claim = "主导 Kubernetes 集群迁移"
        rejected_quotes = [
            "团队主导 Kubernetes 集群迁移",
            "我们主导 Kubernetes 集群迁移",
            "项目组主导 Kubernetes 集群迁移",
            "部门主导 Kubernetes 集群迁移",
            "候选人未主导 Kubernetes 集群迁移",
            "主导 Kubernetes 集群迁移不属实",
        ]
        for quote in rejected_quotes:
            with self.subTest(quote=quote):
                result = result_for_claim(
                    claim, status="source_verified", quote=quote
                )
                gate = validate_rewrite_facts(
                    result,
                    [{"id": "source_resume_1", "content": quote}],
                    self.jobs,
                    [],
                )
                self.assertNotEqual(gate.blockers, [])

        positive_quote = "我主导 Kubernetes 集群迁移"
        positive = validate_rewrite_facts(
            result_for_claim(
                claim, status="source_verified", quote=positive_quote
            ),
            [{"id": "source_resume_1", "content": positive_quote}],
            self.jobs,
            [],
        )
        self.assertEqual(positive.blockers, [])

    def test_user_confirmed_nonmetric_claim_uses_local_polarity(self):
        claim = "负责内部发布流程"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id

        for answer in [
            "候选人不负责内部发布流程",
            "候选人未负责内部发布流程",
            "候选人负责内部发布流程不属实",
        ]:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据职责记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

        positive = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="候选人负责内部发布流程",
                    source_note="候选人确认，依据职责记录",
                )
            ],
        )
        self.assertEqual(positive.blockers, [])

    def test_percentage_answer_uses_local_correction_and_claim_subject(self):
        result = result_with_metric("user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        rejected_answers = [
            "工具调用成功率86/100是错误值，实际85/100",
            "工具调用成功率未达到86/100",
            "考试86/100，工具调用成功率没有统计记录",
        ]
        for answer in rejected_answers:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据本地回归记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

        positive = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="工具调用成功率不是85/100，而是86/100",
                    source_note="候选人确认，依据本地回归记录",
                )
            ],
        )
        self.assertEqual(positive.blockers, [])

    def test_source_percentage_basis_is_distinct_and_reproducible(self):
        claim = "工具调用成功率达到86%"
        basis = "100次固定回归中成功86次"
        quote = f"{claim}，{basis}"
        gate = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=quote,
                basis=basis,
            ),
            [{"id": "source_resume_1", "content": quote}],
            self.jobs,
            [],
        )
        self.assertEqual(gate.blockers, [])

    def test_source_metric_cannot_compose_claim_and_basis_across_quotes(self):
        claim = "主导 Kubernetes 迁移，成功率86%"
        result = result_for_claim(
            claim,
            status="source_verified",
            quote=claim,
            basis="100次测试",
        )
        result.claim_audit[0].source_quotes = [claim, "模型准确率86%，100次测试"]
        content = f"{claim}。模型准确率86%，100次测试"

        gate = validate_rewrite_facts(
            result,
            [{"id": "source_resume_1", "content": content}],
            self.jobs,
            [],
        )
        self.assertNotEqual(gate.blockers, [])

    def test_user_metric_answer_must_confirm_the_mixed_claims_nonmetric_part(self):
        claim = "主导 Kubernetes 迁移，成功率86%"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id

        weak = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="Kubernetes 的100次测试中成功86次",
                    source_note="候选人确认，依据测试记录",
                )
            ],
        )
        self.assertEqual(
            [item.question_id for item in weak.blockers], [question_id]
        )

        strong = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="候选人主导 Kubernetes 迁移的100次测试中成功86次",
                    source_note="候选人确认，依据测试记录",
                )
            ],
        )
        self.assertEqual(strong.blockers, [])

    def test_qps_basis_requires_measurement_source_and_statistics_window(self):
        claim = "接口峰值达到500 QPS"
        weak_basis = "项目记录"
        weak_quote = f"{claim}，{weak_basis}"
        weak = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=weak_quote,
                basis=weak_basis,
            ),
            [{"id": "source_resume_1", "content": weak_quote}],
            self.jobs,
            [],
        )
        self.assertNotEqual(weak.blockers, [])

        good_basis = "监控统计窗口持续10分钟"
        good_quote = f"{claim}，{good_basis}"
        good = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=good_quote,
                basis=good_basis,
            ),
            [{"id": "source_resume_1", "content": good_quote}],
            self.jobs,
            [],
        )
        self.assertEqual(good.blockers, [])

    def test_extended_metric_extraction_table(self):
        positives = {
            "管理十余人": "十余人",
            "服务百万用户": "百万用户",
            "覆盖千余家": "千余家",
            "预算¥500": "¥500",
            "成本500美元": "500美元",
            "覆盖100余家": "100余家",
            "服务数百用户": "数百用户",
            "营收一亿元": "一亿元",
            "预算$500K": "$500K",
            "覆盖约100家": "约100家",
            "覆盖100家以上": "100家以上",
            "覆盖100家以下": "100家以下",
        }
        for claim, expected in positives.items():
            with self.subTest(claim=claim):
                self.assertEqual(
                    [metric for _, metric in extract_business_metrics(claim)],
                    [expected],
                )

        for claim in ["8月毕业", "React 18+"]:
            with self.subTest(claim=claim):
                self.assertEqual(extract_business_metrics(claim), [])

    def test_controlled_heading_allowlist_includes_core_advantages_and_basic_info(self):
        plain = validate_rewrite_facts(
            empty_result(markdown="# 核心优势\n## 基本信息"),
            self.resumes,
            self.jobs,
            [],
        )
        fact_bearing = validate_rewrite_facts(
            empty_result(markdown="# 核心优势｜8年经验"),
            self.resumes,
            self.jobs,
            [],
        )

        self.assertEqual(plain.blockers, [])
        self.assertTrue(
            any("缺少 claim_audit" in item.reason for item in fact_bearing.blockers)
        )
        self.assertEqual(
            [metric for _, metric in extract_business_metrics("# 核心优势｜8年经验")],
            ["8年"],
        )

    def test_direct_assertion_rejects_postfixed_actor_and_polarity(self):
        claim = "主导 Kubernetes 迁移"
        for quote in [
            "主导 Kubernetes 迁移的是团队",
            "主导 Kubernetes 迁移的是候选人",
            "主导 Kubernetes 迁移这一说法经核查后不属实",
        ]:
            with self.subTest(quote=quote):
                gate = validate_rewrite_facts(
                    result_for_claim(claim, status="source_verified", quote=quote),
                    [{"id": "source_resume_1", "content": quote}],
                    self.jobs,
                    [],
                )
                self.assertNotEqual(gate.blockers, [])

    def test_mixed_user_metric_reuses_complete_direct_assertion(self):
        claim = "主导 Kubernetes 迁移，成功率86%"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id

        for answer in [
            "团队主导 Kubernetes 迁移的100次测试中成功86次",
            "未主导 Kubernetes 迁移的100次测试中成功86次",
        ]:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据测试记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

    def test_source_percentage_basis_must_recompute_to_claimed_value(self):
        claim = "工具调用成功率达到86%"
        bad_basis = "100次测试中成功85次"
        bad_quote = f"{claim}，{bad_basis}"
        bad = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=bad_quote,
                basis=bad_basis,
            ),
            [{"id": "source_resume_1", "content": bad_quote}],
            self.jobs,
            [],
        )
        self.assertNotEqual(bad.blockers, [])

        good_basis = "100次测试中成功86次"
        good_quote = f"{claim}，{good_basis}"
        good = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=good_quote,
                basis=good_basis,
            ),
            [{"id": "source_resume_1", "content": good_quote}],
            self.jobs,
            [],
        )
        self.assertEqual(good.blockers, [])

    def test_model_blocker_known_slots_require_matching_labels_and_values(self):
        cases = [
            ("请确认最高学历", "专业是计算机", "最高学历为本科"),
            ("请确认入职日期", "离职日期为2023年", "入职日期为2023年3月"),
            (
                "项目日期存在冲突，请确认正确日期",
                "项目日期冲突尚未解决",
                "确认实际项目日期为2023年3月，以该日期为准",
            ),
        ]
        for question, weak_answer, strong_answer in cases:
            with self.subTest(question=question):
                result = empty_result(
                    missing_information=[
                        {
                            "question_id": "model-id",
                            "question": question,
                            "blocking": True,
                            "reason": "缺少确定事实",
                        }
                    ]
                )
                question_id = validate_rewrite_facts(
                    result, self.resumes, self.jobs, []
                ).blockers[0].question_id

                weak = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=weak_answer,
                            source_note="候选人确认，依据证明记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in weak.blockers], [question_id]
                )

                strong = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=strong_answer,
                            source_note="候选人确认，依据证明记录",
                        )
                    ],
                )
                self.assertEqual(strong.blockers, [])

    def test_percentage_record_uses_only_final_asserted_ratio(self):
        result = result_with_metric("user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        for answer in [
            "工具调用86/100并非真实结果，实际工具调用85/100",
            "工具调用86/100这个口径明显错误",
        ]:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据回归记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

        positive = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="工具调用85/100不是最终值，实际工具调用86/100",
                    source_note="候选人确认，依据回归记录",
                )
            ],
        )
        self.assertEqual(positive.blockers, [])

    def test_throughput_evidence_cannot_compose_across_sentences(self):
        claim = "接口达到500 QPS"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id

        for answer in [
            "接口达到500 QPS。另一项目做过压测。平均年龄30岁",
            "接口达到500 QPS，另一项目做过压测，平均年龄30岁",
        ]:
            with self.subTest(answer=answer):
                weak = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据压测记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in weak.blockers], [question_id]
                )

        positive = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="接口在持续5分钟压测窗口内达到500 QPS（P95口径）",
                    source_note="候选人确认，依据压测记录",
                )
            ],
        )
        self.assertEqual(positive.blockers, [])

    def test_source_throughput_evidence_uses_one_measurement_record(self):
        claim = "接口达到500 QPS"
        weak_basis = "另一项目做过压测。平均年龄30岁"
        weak_quote = f"{claim}。{weak_basis}"
        weak = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=weak_quote,
                basis=weak_basis,
            ),
            [{"id": "source_resume_1", "content": weak_quote}],
            self.jobs,
            [],
        )
        self.assertNotEqual(weak.blockers, [])

        good_basis = "压测窗口持续5分钟，P95口径"
        good_quote = f"{claim}，{good_basis}"
        good = validate_rewrite_facts(
            result_for_claim(
                claim,
                status="source_verified",
                quote=good_quote,
                basis=good_basis,
            ),
            [{"id": "source_resume_1", "content": good_quote}],
            self.jobs,
            [],
        )
        self.assertEqual(good.blockers, [])

    def test_model_blocker_rejects_unresolved_answers(self):
        result = empty_result(
            missing_information=[
                {
                    "question_id": "model-id",
                    "question": "请确认部署环境",
                    "blocking": True,
                    "reason": "缺少确定事实",
                }
            ]
        )
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        for answer in [
            "部署环境尚未解决",
            "部署环境待核实",
            "部署环境不确定",
            "部署环境未知",
            "部署环境无法确认",
            "部署环境未确认",
        ]:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据部署记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

    def test_fifth_review_metric_extraction_table(self):
        positives = {
            "管理100多人": "100多人",
            "服务数千用户": "数千用户",
            "拥有十余年经验": "十余年",
            "预算USD 500K": "USD 500K",
        }
        for claim, expected in positives.items():
            with self.subTest(claim=claim):
                self.assertEqual(
                    [metric for _, metric in extract_business_metrics(claim)],
                    [expected],
                )

        for claim in [
            "毕业时间：8月",
            "毕业于8月",
            "Vue 3+",
            "Next.js 14+",
            "PostgreSQL 16",
        ]:
            with self.subTest(claim=claim):
                self.assertEqual(extract_business_metrics(claim), [])

        self.assertEqual(
            [metric for _, metric in extract_business_metrics("服务10+个项目")],
            ["10+"],
        )

    def test_mixed_claim_without_comma_keeps_nonmetric_fact_for_direct_assertion(self):
        claim = "主导迁移实现成功率86%"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id

        for answer in [
            "团队主导迁移实现的统计样本为86/100",
            "未主导迁移实现的统计样本为86/100",
        ]:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据测试记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

        positive = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="候选人主导迁移实现的统计样本为86/100",
                    source_note="候选人确认，依据测试记录",
                )
            ],
        )
        self.assertEqual(positive.blockers, [])

    def test_known_slots_require_final_affirmed_value(self):
        cases = [
            ("请确认最高学历", "最高学历不是本科", "最高学历不是大专，而是本科"),
            (
                "请确认入职日期",
                "入职日期并非2023年3月",
                "入职日期并非2023年2月，实际为2023年3月",
            ),
            ("请确认部署环境", "部署环境不是Linux", "部署环境确认是Linux"),
            (
                "项目日期存在冲突，请确认正确日期",
                "确认实际项目日期不是2023年3月，以该日期为准",
                "确认实际项目日期为2023年3月，以该日期为准",
            ),
        ]
        for question, rejected, accepted in cases:
            with self.subTest(question=question):
                result = empty_result(
                    missing_information=[
                        {
                            "question_id": "model-id",
                            "question": question,
                            "blocking": True,
                            "reason": "缺少确定事实",
                        }
                    ]
                )
                question_id = validate_rewrite_facts(
                    result, self.resumes, self.jobs, []
                ).blockers[0].question_id
                rejected_gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=rejected,
                            source_note="候选人确认，依据证明记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in rejected_gate.blockers],
                    [question_id],
                )

                accepted_gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=accepted,
                            source_note="候选人确认，依据证明记录",
                        )
                    ],
                )
                self.assertEqual(accepted_gate.blockers, [])

    def test_qps_metric_clause_must_be_affirmed_and_keep_same_subject(self):
        claim = "接口达到500 QPS"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        for answer in [
            "接口未达到500 QPS，压测窗口持续5分钟",
            "接口达到500 QPS，另一服务做过压测，窗口持续5分钟",
        ]:
            with self.subTest(answer=answer):
                gate = validate_rewrite_facts(
                    result,
                    self.resumes,
                    self.jobs,
                    [
                        FactAnswer(
                            question_id=question_id,
                            answer=answer,
                            source_note="候选人确认，依据压测记录",
                        )
                    ],
                )
                self.assertEqual(
                    [item.question_id for item in gate.blockers], [question_id]
                )

    def test_percentage_record_rejects_false_or_invalid_ratio(self):
        result = result_with_metric("user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        rejected = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="工具调用统计样本86/100为虚假数据",
                    source_note="候选人确认，依据回归记录",
                )
            ],
        )
        self.assertEqual(
            [item.question_id for item in rejected.blockers], [question_id]
        )

        accepted = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="工具调用85/100为无效旧值，最终工具调用86/100",
                    source_note="候选人确认，依据回归记录",
                )
            ],
        )
        self.assertEqual(accepted.blockers, [])

    def test_percentage_final_event_controls_value_and_state(self):
        result = result_with_metric("user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        rejected = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="工具调用86/100，最终工具调用86/100为无效数据",
                    source_note="候选人确认，依据回归记录",
                )
            ],
        )
        self.assertEqual(
            [item.question_id for item in rejected.blockers], [question_id]
        )

        accepted = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer="工具调用旧86/100为无效数据，最终工具调用86/100为有效最终结果",
                    source_note="候选人确认，依据回归记录",
                )
            ],
        )
        self.assertEqual(accepted.blockers, [])

    def test_qps_final_exact_metric_event_controls_polarity(self):
        claim = "接口达到500 QPS"
        result = result_for_claim(claim, status="user_confirmed")
        question_id = validate_rewrite_facts(
            result, self.resumes, self.jobs, []
        ).blockers[0].question_id
        rejected = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer=(
                        "接口达到500 QPS但复核后未达到500 QPS，"
                        "压测窗口持续5分钟"
                    ),
                    source_note="候选人确认，依据压测记录",
                )
            ],
        )
        self.assertEqual(
            [item.question_id for item in rejected.blockers], [question_id]
        )

        accepted = validate_rewrite_facts(
            result,
            self.resumes,
            self.jobs,
            [
                FactAnswer(
                    question_id=question_id,
                    answer=(
                        "接口曾未达到500 QPS，优化后最终达到500 QPS，"
                        "持续压测5分钟取P95"
                    ),
                    source_note="候选人确认，依据压测记录",
                )
            ],
        )
        self.assertEqual(accepted.blockers, [])


if __name__ == "__main__":
    unittest.main()
