import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
import unittest

from pydantic import ValidationError

from resume_agent.discovery_schemas import DiscoveredJob
from resume_agent.discovery_sources import SourceInteractionRequiredError
from resume_agent.repository import Repository
from resume_agent.targeted_jobs import JobTarget, TargetedJobResolver
from resume_agent.rewrite_cli import read_target_jd_file


def discovered(source="nowcoder", **overrides):
    host = "www.nowcoder.com" if source == "nowcoder" else "www.zhipin.com"
    path = "/jobs/detail/7" if source == "nowcoder" else "/job_detail/7.html"
    payload = {
        "source": source,
        "source_job_id": "7",
        "canonical_url": f"https://{host}{path}?private=do-not-store",
        "title": "Agent Engineer",
        "company": "Acme",
        "location": "Beijing",
        "salary_raw": "20-30K",
        "detail": "Build safe Agent workflows",
        "detail_status": "complete",
        "search_keyword": "upstream keyword",
        "verified_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        "score": 91.5,
        "qualified": True,
        "source_metadata": {"private": "do-not-store"},
    }
    payload.update(overrides)
    return DiscoveredJob(**payload)


class FakeAdapter:
    def __init__(self, source, result=None, error=None, barrier=None):
        self.source = source
        self.result = result or discovered(source)
        self.error = error
        self.barrier = barrier
        self.calls = []
        self._lock = threading.Lock()

    def resolve_url(self, url, keyword):
        with self._lock:
            self.calls.append((url, keyword))
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        if self.error is not None:
            raise self.error
        return self.result


class JobTargetModelTests(unittest.TestCase):
    def test_requires_exactly_one_primary_input_and_forbids_extras(self):
        for payload in ({}, {"job_id": "job-1", "jd_text": "body"}):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValidationError, "exactly one"):
                    JobTarget(**payload)
        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            JobTarget(jd_text="body", secret="private")

    def test_rejects_blank_primary_values_and_oversized_trimmed_text(self):
        for field in ("job_id", "jd_text", "jd_url"):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValidationError, "blank"):
                    JobTarget(**{field: " \r\n\t "})
        with self.assertRaisesRegex(ValidationError, "100000"):
            JobTarget(jd_text="x" * 100_001)

    def test_supplemental_facts_are_only_valid_with_direct_text(self):
        with self.assertRaisesRegex(ValidationError, "only with jd_text"):
            JobTarget(job_id="job-1", title="Ignore me")
        with self.assertRaisesRegex(ValidationError, "only with jd_text"):
            JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7", company="Ignore")
        target = JobTarget(jd_text="  body  ", title=" Agent ", company=" Acme ")
        self.assertEqual((target.jd_text, target.title, target.company), ("body", "Agent", "Acme"))


class TargetedJobResolverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.directory.name) / "test.db")
        self.candidate = self.repository.create_candidate("Candidate", ["Agent"], "")
        self.other_candidate = self.repository.create_candidate("Other", ["Agent"], "")
        self.nowcoder = FakeAdapter("nowcoder")
        self.boss = FakeAdapter(
            "boss", error=SourceInteractionRequiredError("private source message")
        )
        self.resolver = TargetedJobResolver(
            self.repository, {"nowcoder": self.nowcoder, "boss": self.boss}
        )

    def tearDown(self):
        self.directory.cleanup()

    def job_count(self):
        return sum(
            len(self.repository.list_jobs(candidate["id"]))
            for candidate in (self.candidate, self.other_candidate)
        )

    def assert_failure_without_job(self, code, target, *, resolver=None, candidate_id=None):
        before = self.job_count()
        with self.assertRaisesRegex(ValueError, code):
            (resolver or self.resolver).resolve(
                target, candidate_id or self.candidate["id"]
            )
        self.assertEqual(self.job_count(), before)

    def test_existing_job_id_is_reused_only_for_exact_candidate(self):
        job = self.repository.create_job({
            "candidate_id": self.candidate["id"],
            "source": "manual",
            "title": "Agent",
            "detail": "Build agents",
        })
        global_job = self.repository.create_job({
            "source": "manual", "title": "Global", "detail": "Not candidate-owned"
        })

        self.assertEqual(
            self.resolver.resolve(JobTarget(job_id=job["id"]), self.candidate["id"]),
            job["id"],
        )
        self.assert_failure_without_job(
            "TARGET_CANDIDATE_MISMATCH",
            JobTarget(job_id=job["id"]),
            candidate_id=self.other_candidate["id"],
        )
        self.assert_failure_without_job(
            "TARGET_CANDIDATE_MISMATCH", JobTarget(job_id=global_job["id"])
        )

    def test_candidate_is_validated_before_any_adapter_call(self):
        self.assert_failure_without_job(
            "TARGET_CANDIDATE_INVALID",
            JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7"),
            candidate_id="candidate_missing",
        )
        self.assertEqual(self.nowcoder.calls, [])

    def test_direct_text_uses_parsed_facts_before_supplements(self):
        raw = (
            "# 职位名称\nParsed Agent\n\n# 薪资范围\n20-30K\n\n"
            "# 职位描述\nParsed detail\n\n# 公司\nParsed Company"
        )
        resolved = self.resolver.resolve(
            JobTarget(jd_text=raw, title="Override title", company="Override company"),
            self.candidate["id"],
        )

        job = self.repository.get_job(resolved)
        self.assertEqual(job["source"], "targeted_text")
        self.assertEqual(job["title"], "Parsed Agent")
        self.assertEqual(job["salary"], "20-30K")
        self.assertEqual(job["detail"], "Parsed detail")
        self.assertEqual(job["company"], "Parsed Company")
        self.assertIsNone(job["external_job_id"])

    def test_direct_text_falls_back_to_supplements_and_full_text(self):
        resolved = self.resolver.resolve(
            JobTarget(jd_text="  Build reliable agents  ", title="Agent", company="Acme"),
            self.candidate["id"],
        )
        job = self.repository.get_job(resolved)
        self.assertEqual(job["title"], "Agent")
        self.assertEqual(job["detail"], "Build reliable agents")
        self.assertEqual(job["company"], "Acme")
        self.assertEqual(job["salary"], "")

    def test_direct_text_requires_a_real_title_without_mutation(self):
        self.assert_failure_without_job(
            "TARGET_TITLE_REQUIRED", JobTarget(jd_text="Build reliable agents")
        )

    def test_direct_text_retry_reuses_one_candidate_scoped_job(self):
        target = JobTarget(jd_text="Build reliable agents", title="Agent")
        first = self.resolver.resolve(target, self.candidate["id"])
        second = self.resolver.resolve(target, self.candidate["id"])
        other = self.resolver.resolve(target, self.other_candidate["id"])

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(len(self.repository.list_jobs(self.candidate["id"])), 1)
        self.assertEqual(len(self.repository.list_jobs(self.other_candidate["id"])), 1)

    def test_nowcoder_url_routes_only_to_nowcoder_and_maps_canonical_fields(self):
        url = "https://www.nowcoder.com/jobs/detail/7?tracking=private"
        resolved = self.resolver.resolve(JobTarget(jd_url=url), self.candidate["id"])

        self.assertEqual(self.nowcoder.calls, [(url, "targeted")])
        self.assertEqual(self.boss.calls, [])
        job = self.repository.get_job(resolved)
        self.assertEqual(job["external_job_id"], "7")
        self.assertEqual(job["source"], "nowcoder")
        self.assertEqual(job["title"], "Agent Engineer")
        self.assertEqual(job["company"], "Acme")
        self.assertEqual(job["salary"], "20-30K")
        self.assertEqual(job["detail"], "Build safe Agent workflows")
        self.assertEqual(job["score"], 91.5)
        self.assertEqual(job["qualified"], 1)
        self.assertEqual(job["search_keyword"], "upstream keyword")
        self.assertNotIn("private", job["detail"])
        self.assertNotIn("nowcoder.com", job["detail"])

    def test_default_https_port_is_allowed(self):
        url = "https://NOWCODER.COM:443/jobs/detail/7"
        self.resolver.resolve(JobTarget(jd_url=url), self.candidate["id"])
        self.assertEqual(self.nowcoder.calls, [(url, "targeted")])

    def test_boss_interaction_required_is_stable_and_does_not_create_job(self):
        self.assert_failure_without_job(
            "SOURCE_INTERACTION_REQUIRED",
            JobTarget(jd_url="https://www.zhipin.com/job_detail/7.html?secret=value"),
        )
        self.assertEqual(len(self.boss.calls), 1)

    def test_rejects_spoofed_or_unsafe_urls_before_adapter_call(self):
        urls = [
            "http://www.nowcoder.com/jobs/detail/7",
            "https://www.nowcoder.com.evil.example/jobs/detail/7",
            "https://evil.example/jobs/detail/7?next=www.nowcoder.com",
            "https://user:password@www.nowcoder.com/jobs/detail/7",
            "https://www.nowcoder.com:444/jobs/detail/7",
            "https:///jobs/detail/7",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assert_failure_without_job(
                    "UNSUPPORTED_JD_SOURCE", JobTarget(jd_url=url)
                )
        self.assertEqual(self.nowcoder.calls, [])
        self.assertEqual(self.boss.calls, [])

    def test_missing_or_misconfigured_adapter_is_rejected_without_call(self):
        missing = TargetedJobResolver(self.repository, {})
        self.assert_failure_without_job(
            "SOURCE_ADAPTER_UNAVAILABLE",
            JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7"),
            resolver=missing,
        )
        wrong = FakeAdapter("boss")
        misconfigured = TargetedJobResolver(self.repository, {"nowcoder": wrong})
        self.assert_failure_without_job(
            "SOURCE_ADAPTER_MISMATCH",
            JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7"),
            resolver=misconfigured,
        )
        self.assertEqual(wrong.calls, [])

    def test_adapter_source_mismatch_partial_and_exception_leave_no_job(self):
        cases = [
            (
                "SOURCE_RESULT_MISMATCH",
                FakeAdapter("nowcoder", result=discovered("boss")),
            ),
            (
                "PARTIAL_JD",
                FakeAdapter("nowcoder", result=discovered(detail_status="partial")),
            ),
            (
                "PARTIAL_JD",
                FakeAdapter("nowcoder", result=discovered(detail="   ")),
            ),
            (
                "SOURCE_RESOLUTION_FAILED",
                FakeAdapter("nowcoder", error=RuntimeError("private token")),
            ),
        ]
        for code, adapter in cases:
            with self.subTest(code=code):
                resolver = TargetedJobResolver(self.repository, {"nowcoder": adapter})
                self.assert_failure_without_job(
                    code,
                    JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7?secret=x"),
                    resolver=resolver,
                )

    def test_concurrent_url_resolution_reuses_one_job(self):
        barrier = threading.Barrier(6)
        adapter = FakeAdapter("nowcoder", barrier=barrier)
        resolver = TargetedJobResolver(self.repository, {"nowcoder": adapter})
        target = JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7")
        results = []
        errors = []

        def resolve():
            try:
                results.append(resolver.resolve(target, self.candidate["id"]))
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=resolve) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.repository.list_jobs(self.candidate["id"])), 1)


class TargetJdFileBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_reads_utf8_bom_text_and_markdown_as_trimmed_text(self):
        for suffix in (".md", ".TXT"):
            with self.subTest(suffix=suffix):
                path = self.root / f"target{suffix}"
                path.write_text("  Agent JD  \n", encoding="utf-8-sig")
                self.assertEqual(read_target_jd_file(path), "Agent JD")

    def test_rejects_unsafe_unreadable_empty_and_oversized_inputs(self):
        directory = self.root / "folder.md"
        directory.mkdir()
        wrong_suffix = self.root / "target.pdf"
        wrong_suffix.write_text("JD", encoding="utf-8")
        empty = self.root / "empty.txt"
        empty.write_text(" \r\n ", encoding="utf-8")
        oversized = self.root / "large.md"
        oversized.write_text("x" * 100_001, encoding="utf-8")
        missing = self.root / "missing.md"
        for path in (directory, wrong_suffix, empty, oversized, missing):
            with self.subTest(path=path.name):
                with self.assertRaises(ValueError):
                    read_target_jd_file(path)

    def test_rejects_symlinked_file_when_supported(self):
        target = self.root / "private.md"
        target.write_text("private", encoding="utf-8")
        link = self.root / "linked.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            read_target_jd_file(link)


if __name__ == "__main__":
    unittest.main()
