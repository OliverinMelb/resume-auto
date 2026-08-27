import io
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from resume_agent.discovery_http import LocalJsonClient
from resume_agent.discovery_schemas import DiscoveredJob
from resume_agent.discovery_sources import (
    BossResearchAdapter,
    NowcoderResearchAdapter,
    SourceInteractionRequiredError,
)
from resume_agent.repository import Repository
from resume_agent import rewrite_cli
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


class ContractTransport:
    def __init__(self, response=None):
        self.response = response or {}
        self.calls = []

    def request_json(self, method, url, payload, timeout_seconds):
        self.calls.append((method, url, payload, timeout_seconds))
        return self.response


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

    def test_internal_validation_rendering_hides_sensitive_inputs(self):
        sentinels_and_payloads = [
            (
                ("OVERSIZED_TEXT_SENTINEL",),
                {"jd_text": "OVERSIZED_TEXT_SENTINEL" + "x" * 100_001},
            ),
            (
                ("OVERSIZED_URL_SENTINEL",),
                {
                    "jd_url": "https://www.nowcoder.com/OVERSIZED_URL_SENTINEL"
                    + "x" * 8192
                },
            ),
            (
                ("MUTUAL_TEXT_SENTINEL", "MUTUAL_URL_SENTINEL"),
                {
                    "jd_text": "MUTUAL_TEXT_SENTINEL",
                    "jd_url": "https://www.nowcoder.com/jobs/detail/7?secret=MUTUAL_URL_SENTINEL",
                },
            ),
            (
                ("EXTRA_URL_FIELD_SENTINEL",),
                {
                    "jd_url": "https://www.nowcoder.com/jobs/detail/7",
                    "private_extra": "EXTRA_URL_FIELD_SENTINEL",
                },
            ),
            (
                ("EXTRA_TEXT_BODY_SENTINEL", "EXTRA_TEXT_FIELD_SENTINEL"),
                {
                    "jd_text": "EXTRA_TEXT_BODY_SENTINEL",
                    "private_extra": "EXTRA_TEXT_FIELD_SENTINEL",
                },
            ),
        ]
        for sentinels, payload in sentinels_and_payloads:
            with self.subTest(sentinels=sentinels):
                with self.assertRaises(ValidationError) as raised:
                    JobTarget(**payload)
                for sentinel in sentinels:
                    self.assertNotIn(sentinel, str(raised.exception))
                    self.assertNotIn(sentinel, repr(raised.exception))

    def test_external_payload_factory_returns_only_stable_safe_error(self):
        factory = getattr(JobTarget, "from_payload", None)
        self.assertIsNotNone(factory, "safe external payload factory is required")
        cases = [
            (
                ("SAFE_FACTORY_OVERSIZED",),
                {"jd_text": "SAFE_FACTORY_OVERSIZED" + "x" * 100_001},
            ),
            (
                ("SAFE_FACTORY_URL_OVERSIZED",),
                {
                    "jd_url": "https://www.nowcoder.com/SAFE_FACTORY_URL_OVERSIZED"
                    + "x" * 8192
                },
            ),
            (
                ("SAFE_FACTORY_TEXT", "SAFE_FACTORY_URL"),
                {
                    "jd_text": "SAFE_FACTORY_TEXT",
                    "jd_url": "https://www.nowcoder.com/jobs/detail/7?SAFE_FACTORY_URL",
                },
            ),
            (
                ("SAFE_FACTORY_EXTRA",),
                {
                    "jd_url": "https://www.nowcoder.com/jobs/detail/7",
                    "secret": "SAFE_FACTORY_EXTRA",
                },
            ),
            (
                ("SAFE_FACTORY_TEXT_BODY", "SAFE_FACTORY_TEXT_EXTRA"),
                {
                    "jd_text": "SAFE_FACTORY_TEXT_BODY",
                    "secret": "SAFE_FACTORY_TEXT_EXTRA",
                },
            ),
        ]
        for sentinels, payload in cases:
            with self.subTest(sentinels=sentinels):
                try:
                    factory(payload)
                except ValueError as error:
                    renderings = [str(error), repr(error), repr(error.args)]
                    if hasattr(error, "errors"):
                        renderings.append(repr(error.errors()))
                    if hasattr(error, "json"):
                        renderings.append(error.json())
                    self.assertEqual(str(error), "INVALID_JOB_TARGET")
                    self.assertIsNone(error.__cause__)
                    self.assertIsNone(error.__context__)
                    self.assertTrue(
                        all(
                            sentinel not in rendering
                            for sentinel in sentinels
                            for rendering in renderings
                        )
                    )
                else:
                    self.fail("invalid external payload was accepted")
        valid = factory({"jd_text": "  Build agents  ", "title": " Agent "})
        self.assertEqual((valid.jd_text, valid.title), ("Build agents", "Agent"))


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
        with self.assertRaisesRegex(ValueError, code) as raised:
            (resolver or self.resolver).resolve(
                target, candidate_id or self.candidate["id"]
            )
        self.assertEqual(self.job_count(), before)
        return raised.exception

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

    def test_direct_text_identity_includes_effective_title_and_company(self):
        body = "Build reliable agents"
        first = self.resolver.resolve(
            JobTarget(jd_text=body, title="Agent", company="Acme"),
            self.candidate["id"],
        )
        retried = self.resolver.resolve(
            JobTarget(jd_text=body, title="Agent", company="Acme"),
            self.candidate["id"],
        )
        other_title = self.resolver.resolve(
            JobTarget(jd_text=body, title="RAG Engineer", company="Acme"),
            self.candidate["id"],
        )
        other_company = self.resolver.resolve(
            JobTarget(jd_text=body, title="Agent", company="Other"),
            self.candidate["id"],
        )

        self.assertEqual(first, retried)
        self.assertEqual(len({first, other_title, other_company}), 3)
        jobs = self.repository.get_jobs([first, other_title, other_company])
        self.assertEqual(
            [(job["title"], job["company"]) for job in jobs],
            [("Agent", "Acme"), ("RAG Engineer", "Acme"), ("Agent", "Other")],
        )

    def test_concurrent_direct_text_resolution_preserves_effective_identity(self):
        barrier = threading.Barrier(6)

        class GatedRepository(Repository):
            def create_job_if_absent(self, payload, fingerprint):
                barrier.wait(timeout=5)
                return super().create_job_if_absent(payload, fingerprint)

        resolver = TargetedJobResolver(
            GatedRepository(self.repository.database_path), {}
        )
        target = JobTarget(
            jd_text="Build concurrent agents", title="Agent", company="Acme"
        )
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
        job = self.repository.get_job(results[0])
        self.assertEqual((job["title"], job["company"]), ("Agent", "Acme"))
        self.assertEqual(len(self.repository.list_jobs(self.candidate["id"])), 1)

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
        url = "https://WWW.NOWCODER.COM:443/jobs/detail/7"
        self.resolver.resolve(JobTarget(jd_url=url), self.candidate["id"])
        self.assertEqual(self.nowcoder.calls, [(url, "targeted")])

    def test_real_adapters_align_with_exact_supported_hosts(self):
        nowcoder_transport = ContractTransport({
            "source_job_id": "n-7",
            "source_url": "https://www.nowcoder.com/jobs/detail/7",
            "title": "Agent Engineer",
            "company": "Acme",
            "locations": [],
            "description": "Build agents",
            "responsibilities": "",
            "requirements": "",
            "salary_text": None,
            "published_at": None,
            "last_verified_at": "2026-08-27T00:00:00Z",
            "detail_status": "complete",
            "score": None,
            "qualified": None,
        })
        nowcoder = NowcoderResearchAdapter(
            LocalJsonClient("http://localhost:8002", transport=nowcoder_transport)
        )
        boss_transport = ContractTransport()
        boss = BossResearchAdapter(
            LocalJsonClient("http://localhost:8001", transport=boss_transport)
        )
        resolver = TargetedJobResolver(
            self.repository, {"nowcoder": nowcoder, "boss": boss}
        )

        resolved = resolver.resolve(
            JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7"),
            self.candidate["id"],
        )
        self.assertEqual(self.repository.get_job(resolved)["external_job_id"], "n-7")
        self.assertEqual(len(nowcoder_transport.calls), 1)
        self.assert_failure_without_job(
            "SOURCE_INTERACTION_REQUIRED",
            JobTarget(jd_url="https://www.zhipin.com/job_detail/7.html"),
            resolver=resolver,
        )
        self.assertEqual(boss_transport.calls, [])

        for url in (
            "https://nowcoder.com/jobs/detail/7",
            "https://zhipin.com/job_detail/7.html",
        ):
            with self.subTest(url=url):
                self.assert_failure_without_job(
                    "UNSUPPORTED_JD_SOURCE", JobTarget(jd_url=url), resolver=resolver
                )
        self.assertEqual(len(nowcoder_transport.calls), 1)
        self.assertEqual(boss_transport.calls, [])

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
                error = self.assert_failure_without_job(
                    code,
                    JobTarget(jd_url="https://www.nowcoder.com/jobs/detail/7?secret=x"),
                    resolver=resolver,
                )
                self.assertNotIn("secret=x", str(error))

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

    def test_rejects_symlinked_ancestor_when_supported(self):
        real = self.root / "real"
        real.mkdir()
        (real / "target.md").write_text("private", encoding="utf-8")
        linked = self.root / "linked"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlinks unavailable")
        with self.assertRaisesRegex(ValueError, "unsafe"):
            read_target_jd_file(linked / "target.md")

    def test_checks_each_existing_component_before_and_after_open(self):
        nested = self.root / "nested"
        nested.mkdir()
        target = nested / "target.md"
        target.write_text("Agent JD", encoding="utf-8")
        checked = []

        def record(path):
            checked.append(Path(path).absolute())
            return False

        with patch(
            "resume_agent.rewrite_cli._is_reparse_point", side_effect=record
        ):
            self.assertEqual(read_target_jd_file(target), "Agent JD")

        self.assertEqual(checked.count(nested.absolute()), 2)
        self.assertEqual(checked.count(target.absolute()), 2)

    def test_rejects_patched_unsafe_ancestor_before_open(self):
        nested = self.root / "nested"
        nested.mkdir()
        target = nested / "target.md"
        target.write_text("Agent JD", encoding="utf-8")

        with patch(
            "resume_agent.rewrite_cli._is_reparse_point",
            side_effect=lambda path: Path(path).absolute() == nested.absolute(),
        ), patch("resume_agent.rewrite_cli.os.open") as open_file:
            with self.assertRaisesRegex(ValueError, "unsafe"):
                read_target_jd_file(target)
        open_file.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Windows uses final handle paths")
    def test_non_windows_rejects_leaf_identity_change_after_open(self):
        target = self.root / "target.md"
        target.write_text("Agent JD", encoding="utf-8")
        with patch("os.path.samestat", return_value=False) as same_stat:
            with self.assertRaisesRegex(ValueError, "unsafe"):
                read_target_jd_file(target)
        same_stat.assert_called_once()

    def test_reads_at_most_100001_characters_from_fixed_handle(self):
        target = self.root / "target.md"
        target.write_text("path content must not be reopened", encoding="utf-8")

        class RecordingReader:
            def __init__(self):
                self.limit = None

            def __enter__(self):
                return self

            def __exit__(self, *unused):
                return False

            def read(self, limit):
                self.limit = limit
                return "bounded content"

        reader = RecordingReader()
        with patch("io.TextIOWrapper", return_value=reader):
            self.assertEqual(read_target_jd_file(target), "bounded content")
        self.assertEqual(reader.limit, 100_001)

    def test_rejects_invalid_utf8_with_stable_error(self):
        target = self.root / "target.md"
        target.write_bytes(b"\xff\xfe\xff")
        with self.assertRaisesRegex(ValueError, "UTF-8") as raised:
            read_target_jd_file(target)
        self.assertNotIn(str(target), str(raised.exception))

    def test_character_limit_is_checked_before_trimming(self):
        accepted = self.root / "accepted.md"
        accepted.write_text("x" * 100_000, encoding="utf-8")
        oversized = self.root / "oversized.md"
        oversized.write_text("x" * 100_000 + " ", encoding="utf-8")

        self.assertEqual(len(read_target_jd_file(accepted)), 100_000)
        with self.assertRaisesRegex(ValueError, "100000"):
            read_target_jd_file(oversized)

    def test_windows_final_path_normalizes_dos_and_unc_prefixes(self):
        normalize = getattr(rewrite_cli, "_normalize_windows_final_path", None)
        self.assertIsNotNone(normalize, "Windows final path normalizer is required")
        self.assertEqual(
            normalize(r"\\?\C:\Users\Case\Target.MD"),
            r"c:\users\case\target.md",
        )
        self.assertEqual(
            normalize(r"\\?\UNC\Server\Share\Folder\Target.MD"),
            r"\\server\share\folder\target.md",
        )
        self.assertEqual(
            normalize(r"C:/Users/Case/../Case/Target.MD"),
            r"c:\users\case\target.md",
        )

    def test_windows_path_validator_rejects_ads_and_device_prefixes(self):
        validate = getattr(rewrite_cli, "_validate_windows_target_path", None)
        self.assertIsNotNone(validate, "Windows lexical path validator is required")
        for value in (
            r"document.pdf:target.txt",
            r"C:\safe\document.pdf:target.txt",
            r"\\?\C:\safe\target.txt",
            r"\\?\UNC\server\share\target.txt",
            r"\\.\C:\safe\target.txt",
            r"//?/C:/safe/target.txt",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "unsafe") as raised:
                    validate(value)
                self.assertNotIn(value, str(raised.exception))
        for value in (
            r"target.txt",
            r"C:\safe\target.txt",
            r"\\server\share\target.txt",
        ):
            with self.subTest(value=value):
                validate(value)

    @unittest.skipUnless(os.name == "nt", "Windows handle path check")
    def test_windows_rejects_mismatched_final_handle_before_content_read(self):
        target = self.root / "target.md"
        target.write_text("must not return", encoding="utf-8")
        different = self.root / "different.md"
        with patch(
            "resume_agent.rewrite_cli._windows_final_path_for_descriptor",
            return_value=str(different),
            create=True,
        ) as final_path, patch(
            "io.TextIOWrapper", wraps=io.TextIOWrapper
        ) as text_reader:
            with self.assertRaisesRegex(ValueError, "unsafe"):
                read_target_jd_file(target)
        final_path.assert_called_once()
        text_reader.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows handle path check")
    def test_windows_rejects_final_handle_api_failure_before_content_read(self):
        target = self.root / "target.md"
        target.write_text("must not return", encoding="utf-8")
        with patch(
            "resume_agent.rewrite_cli._windows_final_path_for_descriptor",
            side_effect=OSError("private failure"),
            create=True,
        ) as final_path, patch(
            "io.TextIOWrapper", wraps=io.TextIOWrapper
        ) as text_reader:
            with self.assertRaisesRegex(ValueError, "unsafe") as raised:
                read_target_jd_file(target)
        final_path.assert_called_once()
        text_reader.assert_not_called()
        self.assertNotIn("private failure", str(raised.exception))

    @unittest.skipUnless(os.name == "nt", "Windows ADS semantics")
    def test_windows_read_rejects_ads_before_open(self):
        target = self.root / "document.pdf"
        target.write_text("base", encoding="utf-8")
        ads = Path(f"{target}:target.txt")
        with patch("resume_agent.rewrite_cli.os.open") as open_file:
            with self.assertRaisesRegex(ValueError, "unsafe"):
                read_target_jd_file(ads)
        open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
