import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from pydantic import ValidationError

from resume_agent.repository import NotFoundError, Repository
from resume_agent.rewrite_schemas import GapAcceptanceRequest, TestRecord


def acceptance_payload(**changes):
    payload = {
        "gap_id": "gap-123456789abc",
        "project_path": "C:/work/agent-eval",
        "verified_claims": ["实现本地 Agent 评测集"],
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
    }
    payload.update(changes)
    return payload


class CustomAcceptanceTimezone(tzinfo):
    def __init__(self, offset=None, error=None):
        self.offset = offset
        self.error = error

    def utcoffset(self, value):
        if self.error is not None:
            raise self.error
        return self.offset

    def dst(self, value):
        return timedelta(0)


class GapAcceptanceSchemaTests(unittest.TestCase):
    def test_valid_evidence_is_trimmed_and_utc_is_canonicalized(self):
        request = GapAcceptanceRequest.model_validate(
            acceptance_payload(
                project_path="  C:/work/agent-eval  ",
                verified_claims=["  实现本地 Agent 评测集  "],
                artifact_hashes={"  report.json  ": "a" * 64},
                completed_at="2026-08-26T03:00:00+00:00",
                confirmed_at="2026-08-26T03:01:00+00:00",
            )
        )

        dumped = request.model_dump(mode="json")
        self.assertEqual(dumped["project_path"], "C:/work/agent-eval")
        self.assertEqual(dumped["verified_claims"], ["实现本地 Agent 评测集"])
        self.assertEqual(list(dumped["artifact_hashes"]), ["report.json"])
        self.assertEqual(dumped["completed_at"], "2026-08-26T03:00:00Z")
        self.assertEqual(dumped["confirmed_at"], "2026-08-26T03:01:00Z")

    def test_requires_nonempty_artifacts_and_test_records(self):
        for changes in ({"artifact_hashes": {}}, {"test_records": []}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                GapAcceptanceRequest.model_validate(acceptance_payload(**changes))

    def test_rejects_naive_non_utc_and_reversed_confirmation_times(self):
        invalid_pairs = [
            (0, "2026-08-26T03:01:00Z"),
            ("2026-08-26T03:00:00", "2026-08-26T03:01:00Z"),
            ("2026-08-26T11:00:00+08:00", "2026-08-26T03:01:00Z"),
            ("2026-08-26T03:00:00Z", "2026-08-26T11:01:00+08:00"),
            ("2026-08-26T03:02:00Z", "2026-08-26T03:01:00Z"),
        ]
        for completed_at, confirmed_at in invalid_pairs:
            with self.subTest(
                completed_at=completed_at, confirmed_at=confirmed_at
            ), self.assertRaises(ValidationError):
                GapAcceptanceRequest.model_validate(
                    acceptance_payload(
                        completed_at=completed_at, confirmed_at=confirmed_at
                    )
                )

    def test_timestamps_require_strict_rfc3339_utc_strings_or_aware_utc_datetime(self):
        invalid_values = [
            "0",
            "1",
            "1690000000",
            "2026-08-26 03:00:00Z",
            "2026-08-26T03:00:00z",
            "2026-08-26T03:00:00+0000",
            "2026-08-26T03:00:00-00:00",
            "2026-08-26T03:00Z",
            "not-rfc3339",
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                GapAcceptanceRequest.model_validate(
                    acceptance_payload(completed_at=value, confirmed_at=value)
                )

        completed = datetime(2026, 8, 26, 3, 0, tzinfo=timezone.utc)
        confirmed = datetime(2026, 8, 26, 3, 1, tzinfo=timezone.utc)
        request = GapAcceptanceRequest.model_validate(
            acceptance_payload(completed_at=completed, confirmed_at=confirmed)
        )
        self.assertEqual(
            request.model_dump(mode="json")["completed_at"],
            "2026-08-26T03:00:00Z",
        )

    def test_rejects_false_confirmation_extra_fields_and_non_lowercase_hashes(self):
        invalid_payloads = [
            acceptance_payload(user_confirmed=False),
            {**acceptance_payload(), "unexpected": "data"},
            acceptance_payload(artifact_hashes={"report.json": "A" * 64}),
            acceptance_payload(artifact_hashes={"report.json": "a" * 63}),
            acceptance_payload(
                test_records=[
                    {
                        "command": "python -m unittest -v",
                        "exit_code": 0,
                        "output_sha256": "B" * 64,
                    }
                ]
            ),
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                GapAcceptanceRequest.model_validate(payload)

        with self.assertRaises(ValidationError):
            TestRecord.model_validate(
                {
                    "command": "python -m unittest -v",
                    "exit_code": 0,
                    "output_sha256": "b" * 64,
                    "unexpected": True,
                }
            )

    def test_verified_claims_reject_blanks_duplicates_and_oversized_items(self):
        invalid_claims = [
            ["   "],
            ["\u200b"],
            ["实现本地 Agent 评测集", " 实现本地 Agent 评测集 "],
            ["Built GPT agent", "built  GPT agent"],
            ["x" * 2001],
            [f"claim-{index}" for index in range(51)],
        ]
        for claims in invalid_claims:
            with self.subTest(claims=claims), self.assertRaises(ValidationError):
                GapAcceptanceRequest.model_validate(
                    acceptance_payload(verified_claims=claims)
                )

    def test_artifact_keys_and_test_records_have_strict_bounds(self):
        invalid_artifacts = [
            {"   ": "a" * 64},
            {"x" * 501: "a" * 64},
            {f"artifact-{index}": "a" * 64 for index in range(101)},
        ]
        for artifacts in invalid_artifacts:
            with self.subTest(artifacts=len(artifacts)), self.assertRaises(
                ValidationError
            ):
                GapAcceptanceRequest.model_validate(
                    acceptance_payload(artifact_hashes=artifacts)
                )
        with self.assertRaises(ValidationError):
            GapAcceptanceRequest.model_validate(
                acceptance_payload(
                    test_records=[
                        {
                            "command": f"command-{index}",
                            "exit_code": 0,
                            "output_sha256": "b" * 64,
                        }
                        for index in range(51)
                    ]
                )
            )

    def test_required_evidence_text_is_unicode_canonical_and_substantive(self):
        invalid_payloads = [
            acceptance_payload(project_path="\u200b"),
            acceptance_payload(
                test_records=[{
                    "command": "\u200b",
                    "exit_code": 0,
                    "output_sha256": "b" * 64,
                }]
            ),
            acceptance_payload(artifact_hashes={"\u200b": "a" * 64}),
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                GapAcceptanceRequest.model_validate(payload)

        request = GapAcceptanceRequest.model_validate(
            acceptance_payload(
                project_path="  Ｃ:/work/\u200b项目　 ",
                verified_claims=["  实现\u200b　评测  "],
                artifact_hashes={"　ｒｅｐｏｒｔ.json\u200b ": "a" * 64},
                test_records=[{
                    "command": "  ｐｙｔｈｏｎ\u200b　-m　unittest  ",
                    "exit_code": 0,
                    "output_sha256": "b" * 64,
                }],
            )
        )
        dumped = request.model_dump(mode="json")
        self.assertEqual(dumped["project_path"], "C:/work/项目")
        self.assertEqual(dumped["verified_claims"], ["实现 评测"])
        self.assertEqual(list(dumped["artifact_hashes"]), ["report.json"])
        self.assertEqual(
            dumped["test_records"][0]["command"], "python -m unittest"
        )

    def test_test_records_reject_duplicate_canonical_identity(self):
        duplicate_records = [
            {
                "command": "python\u200b  -m unittest",
                "exit_code": 0,
                "output_sha256": "b" * 64,
            },
            {
                "command": "ｐｙｔｈｏｎ　-m　unittest",
                "exit_code": 0,
                "output_sha256": "b" * 64,
            },
        ]
        with self.assertRaises(ValidationError):
            GapAcceptanceRequest.model_validate(
                acceptance_payload(test_records=duplicate_records)
            )

    def test_artifact_names_reject_duplicate_canonical_identity(self):
        with self.assertRaises(ValidationError):
            GapAcceptanceRequest.model_validate(
                acceptance_payload(
                    artifact_hashes={
                        "report.json": "a" * 64,
                        "ｒｅｐｏｒｔ.json\u200b": "b" * 64,
                    }
                )
            )

    def test_command_and_project_path_are_inert_evidence_text(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "must-not-exist"
            command = f"Set-Content -LiteralPath '{marker}' -Value unsafe"
            missing_project = Path(directory) / "missing-project"

            request = GapAcceptanceRequest.model_validate(
                acceptance_payload(
                    project_path=str(missing_project),
                    test_records=[
                        {
                            "command": command,
                            "exit_code": 0,
                            "output_sha256": "b" * 64,
                        }
                    ],
                )
            )

            self.assertEqual(request.project_path, str(missing_project))
            self.assertEqual(request.test_records[0].command, command)
            self.assertFalse(marker.exists())
            self.assertFalse(missing_project.exists())


class GapAcceptanceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.db"
        self.repository = Repository(self.database_path)
        self.candidate = self.repository.create_candidate("Candidate", [], "")
        self.other_candidate = self.repository.create_candidate("Other", [], "")

    def tearDown(self):
        self.temp_dir.cleanup()

    def assertSafeEvidenceError(self, error, expected_message):
        self.assertIs(type(error), ValueError)
        self.assertEqual(str(error), expected_message)
        self.assertNotIn("TOP_SECRET", str(error))
        self.assertNotIn("TOP_SECRET", repr(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_evidence_round_trips_and_list_order_is_stable(self):
        first_request = GapAcceptanceRequest.model_validate(acceptance_payload())
        second_request = GapAcceptanceRequest.model_validate(
            acceptance_payload(gap_id="gap-abcdef123456")
        )

        first = self.repository.create_gap_acceptance(
            self.candidate["id"], first_request.model_dump(mode="json")
        )
        second = self.repository.create_gap_acceptance(
            self.candidate["id"], second_request.model_dump(mode="json")
        )

        self.assertEqual(
            self.repository.get_gap_acceptance(self.candidate["id"], first["id"]),
            first,
        )
        listed_once = self.repository.list_gap_acceptances(self.candidate["id"])
        listed_twice = self.repository.list_gap_acceptances(self.candidate["id"])
        self.assertEqual(listed_once, listed_twice)
        self.assertEqual({item["id"] for item in listed_once}, {first["id"], second["id"]})
        self.assertEqual(first["evidence"], first_request.model_dump(mode="json"))
        self.assertEqual(first["gap_id"], first_request.gap_id)

    def test_candidate_scope_does_not_leak_acceptance_ids(self):
        accepted = self.repository.create_gap_acceptance(
            self.candidate["id"], acceptance_payload()
        )

        with self.assertRaises(NotFoundError):
            self.repository.get_gap_acceptance(
                self.other_candidate["id"], accepted["id"]
            )
        self.assertEqual(
            self.repository.list_gap_acceptances(self.other_candidate["id"]), []
        )

    def test_same_evidence_is_idempotent_but_conflicting_evidence_is_rejected(self):
        first = self.repository.create_gap_acceptance(
            self.candidate["id"], acceptance_payload()
        )
        repeated = self.repository.create_gap_acceptance(
            self.candidate["id"], acceptance_payload()
        )
        self.assertEqual(repeated, first)

        with self.assertRaises(ValueError):
            self.repository.create_gap_acceptance(
                self.candidate["id"],
                acceptance_payload(verified_claims=["不同的完成事实"]),
            )

    def test_concurrent_same_evidence_returns_one_acceptance(self):
        barrier = threading.Barrier(2)
        records = []
        errors = []

        def create():
            repository = Repository(self.database_path)
            try:
                barrier.wait(timeout=5)
                records.append(
                    repository.create_gap_acceptance(
                        self.candidate["id"], acceptance_payload()
                    )
                )
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["id"], records[1]["id"])
        self.assertEqual(len(self.repository.list_gap_acceptances(self.candidate["id"])), 1)

    def test_corrupt_stored_evidence_fails_closed(self):
        accepted = self.repository.create_gap_acceptance(
            self.candidate["id"], acceptance_payload()
        )
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "UPDATE gap_acceptances SET evidence_json = ? WHERE id = ?",
                ('{"verified_claims": ["TOP_SECRET"]}', accepted["id"]),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(ValueError) as captured:
            self.repository.get_gap_acceptance(self.candidate["id"], accepted["id"])
        self.assertSafeEvidenceError(
            captured.exception, "stored gap acceptance evidence is invalid"
        )
        with self.assertRaises(ValueError):
            self.repository.list_gap_acceptances(self.candidate["id"])

    def test_create_validation_error_does_not_expose_payload_or_exception_chain(self):
        payload = {
            **acceptance_payload(confirmed_at="0"),
            "unexpected": "TOP_SECRET",
        }

        with self.assertRaises(ValueError) as captured:
            self.repository.create_gap_acceptance(self.candidate["id"], payload)

        self.assertSafeEvidenceError(
            captured.exception, "invalid gap acceptance evidence"
        )

    def test_create_timezone_validation_catches_all_ordinary_exceptions_safely(self):
        utc_value = datetime(
            2026, 8, 26, 3, 0, tzinfo=CustomAcceptanceTimezone(timedelta(0))
        )
        accepted = self.repository.create_gap_acceptance(
            self.candidate["id"],
            acceptance_payload(completed_at=utc_value, confirmed_at=utc_value),
        )
        self.assertEqual(accepted["evidence"]["completed_at"], "2026-08-26T03:00:00Z")

        invalid_values = [
            datetime(
                2026,
                8,
                26,
                3,
                0,
                tzinfo=CustomAcceptanceTimezone(timedelta(hours=8)),
            ),
            datetime(
                2026,
                8,
                26,
                3,
                0,
                tzinfo=CustomAcceptanceTimezone(
                    error=RuntimeError("TOP_SECRET timezone detail")
                ),
            ),
        ]
        for value in invalid_values:
            with self.subTest(value_type=type(value.tzinfo).__name__):
                payload = acceptance_payload(
                    gap_id="gap-abcdef123456",
                    completed_at=value,
                    confirmed_at=value,
                )
                with self.assertRaises(ValueError) as captured:
                    self.repository.create_gap_acceptance(
                        self.candidate["id"], payload
                    )
                self.assertSafeEvidenceError(
                    captured.exception, "invalid gap acceptance evidence"
                )

    def test_additive_schema_migration_preserves_existing_candidate(self):
        migrated_path = Path(self.temp_dir.name) / "legacy.db"
        connection = sqlite3.connect(migrated_path)
        try:
            connection.execute(
                """
                CREATE TABLE candidates (
                    id TEXT PRIMARY KEY,
                    display_name TEXT,
                    target_roles_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?)",
                ("candidate_legacy", "Legacy", "[]", "keep", "2026-01-01T00:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()

        migrated = Repository(migrated_path)
        self.assertEqual(migrated.get_candidate("candidate_legacy")["notes"], "keep")
        accepted = migrated.create_gap_acceptance(
            "candidate_legacy", acceptance_payload()
        )
        self.assertEqual(accepted["candidate_id"], "candidate_legacy")


if __name__ == "__main__":
    unittest.main()
