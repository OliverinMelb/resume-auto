import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import get_args

from resume_agent.discovery_schemas import DiscoveredJob
from resume_agent.repository import Repository


RUN_ID = "discovery_run_" + "a" * 32


def _artifacts_class():
    from resume_agent.discovery_artifacts import DiscoveryArtifacts

    return DiscoveryArtifacts


def _job() -> DiscoveredJob:
    return DiscoveredJob.model_validate(
        {
            "source": "nowcoder",
            "source_job_id": "nc-1",
            "canonical_url": "https://www.nowcoder.com/jobs/detail/1",
            "title": "AI Agent Engineer",
            "company": "Example",
            "detail": "Build Agent and RAG workflows.",
            "detail_status": "complete",
            "search_keyword": "AI Agent",
            "verified_at": "2026-08-26T01:00:00Z",
        }
    )


class DiscoveryRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.database_path = self.root / "resume-agent.db"
        self.repository = Repository(self.database_path)
        self.candidate = self.repository.create_candidate(
            "Candidate", ["AI Agent Engineer"], ""
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_run(self, **overrides):
        values = {
            "run_id": RUN_ID,
            "candidate_id": self.candidate["id"],
            "request": {"sources": ["boss", "nowcoder"], "city": "Shanghai"},
            "artifact_dir": str(self.root / "runs" / RUN_ID),
        }
        values.update(overrides)
        return self.repository.create_discovery_run(**values)

    def test_existing_database_initialization_adds_table_without_dropping_data(self):
        old_database = self.root / "old.db"
        with closing(sqlite3.connect(old_database)) as connection:
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
                ("candidate_old", "Existing", '["Engineer"]', "keep", "2025-01-01"),
            )
            connection.commit()

        migrated = Repository(old_database)
        self.assertEqual(migrated.get_candidate("candidate_old")["notes"], "keep")
        with closing(sqlite3.connect(old_database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            count = connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        self.assertIn("discovery_runs", tables)
        self.assertEqual(count, 1)

    def test_existing_discovery_table_adds_poll_lease_columns_without_data_loss(self):
        old_database = self.root / "old-discovery.db"
        with closing(sqlite3.connect(old_database)) as connection:
            connection.executescript(
                """
                CREATE TABLE candidates (
                    id TEXT PRIMARY KEY,
                    display_name TEXT,
                    target_roles_json TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE discovery_runs (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL REFERENCES candidates(id),
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_checkpoints_json TEXT NOT NULL DEFAULT '{}',
                    source_statuses_json TEXT NOT NULL DEFAULT '{}',
                    imported_job_ids_json TEXT NOT NULL DEFAULT '[]',
                    artifact_dir TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO candidates VALUES (?, ?, ?, ?, ?)",
                ("candidate_old", "Existing", '["Engineer"]', "keep", "2025-01-01"),
            )
            connection.execute(
                "INSERT INTO discovery_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    RUN_ID,
                    "candidate_old",
                    '{"sources":["boss"]}',
                    "COLLECTING",
                    '{"boss":{"remote_ids":["remote-1"],"source":"boss","state":{}}}',
                    '{"boss":"collecting"}',
                    "[]",
                    str(self.root / "runs" / RUN_ID),
                    None,
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-01T00:00:00+00:00",
                ),
            )
            connection.commit()

        migrated = Repository(old_database)
        run = migrated.get_discovery_run(RUN_ID)

        self.assertEqual(run["candidate_id"], "candidate_old")
        self.assertEqual(run["source_statuses"], {"boss": "collecting"})
        self.assertNotIn("poll_owner_token", run)
        self.assertNotIn("poll_lease_expires_at", run)
        with closing(sqlite3.connect(old_database)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(discovery_runs)")
            }
            count = connection.execute(
                "SELECT COUNT(*) FROM discovery_runs WHERE id=?", (RUN_ID,)
            ).fetchone()[0]
        self.assertTrue({"poll_owner_token", "poll_lease_expires_at"} <= columns)
        self.assertEqual(count, 1)

    def test_discovery_run_round_trips_only_named_json_fields_deterministically(self):
        run = self._create_run(request={"z": 1, "a": "中文"})
        self.assertEqual(run["status"], "CREATED")
        self.assertEqual(run["request"], {"a": "中文", "z": 1})
        self.assertEqual(run["source_checkpoints"], {})
        self.assertEqual(run["source_statuses"], {})
        self.assertEqual(run["imported_job_ids"], [])
        self.assertFalse(any(key.endswith("_json") for key in run))
        self.assertEqual(
            {
                "request",
                "source_checkpoints",
                "source_statuses",
                "imported_job_ids",
            },
            {
                key
                for key in run
                if key
                in {
                    "request",
                    "source_checkpoints",
                    "source_statuses",
                    "imported_job_ids",
                }
            },
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            encoded = connection.execute(
                "SELECT request_json FROM discovery_runs WHERE id=?", (RUN_ID,)
            ).fetchone()[0]
        self.assertEqual(encoded, '{"a":"中文","z":1}')

    def test_transition_updates_json_fields_and_uses_atomic_status_cas(self):
        self._create_run()
        updated = self.repository.transition_discovery_run(
            RUN_ID,
            {"CREATED"},
            "COLLECTING",
            source_checkpoints={
                "boss": {"source": "boss", "remote_ids": ["p1"], "state": {}}
            },
            source_statuses={"boss": "collecting"},
        )
        self.assertEqual(updated["status"], "COLLECTING")
        self.assertEqual(updated["source_checkpoints"]["boss"]["remote_ids"], ["p1"])

        stale_error = None
        with self.assertRaises(ValueError) as stale:
            self.repository.transition_discovery_run(
                RUN_ID, {"CREATED"}, "FAILED", error_code="SOURCE_UNAVAILABLE"
            )
        stale_error = str(stale.exception)
        with self.assertRaises(ValueError) as missing:
            self.repository.transition_discovery_run(
                "discovery_run_" + "b" * 32,
                {"CREATED"},
                "FAILED",
                error_code="SOURCE_UNAVAILABLE",
            )
        self.assertEqual(stale_error, str(missing.exception))
        self.assertNotIn(RUN_ID, stale_error)
        self.assertEqual(self.repository.get_discovery_run(RUN_ID)["status"], "COLLECTING")

    def test_poll_lease_claim_renew_release_and_owner_guarded_transition(self):
        self._create_run()
        self.repository.transition_discovery_run(RUN_ID, {"CREATED"}, "COLLECTING")
        contender = Repository(self.database_path)
        now = "2026-08-27T00:00:00+00:00"

        self.assertTrue(
            self.repository.claim_discovery_poll(
                RUN_ID,
                "owner-one",
                "2026-08-27T00:05:00+00:00",
                now,
            )
        )
        self.assertFalse(
            contender.claim_discovery_poll(
                RUN_ID,
                "owner-two",
                "2026-08-27T00:05:00+00:00",
                now,
            )
        )
        self.assertTrue(
            self.repository.renew_discovery_poll(
                RUN_ID, "owner-one", "2026-08-27T00:06:00+00:00"
            )
        )
        self.assertFalse(
            contender.renew_discovery_poll(
                RUN_ID, "owner-two", "2026-08-27T00:06:00+00:00"
            )
        )
        with self.assertRaisesRegex(ValueError, "discovery run"):
            contender.transition_discovery_run(
                RUN_ID,
                {"COLLECTING"},
                "READY_FOR_IMPORT",
                expected_poll_owner="owner-two",
                clear_poll_lease=True,
            )
        self.assertFalse(contender.release_discovery_poll(RUN_ID, "owner-two"))

        updated = self.repository.transition_discovery_run(
            RUN_ID,
            {"COLLECTING"},
            "READY_FOR_IMPORT",
            expected_poll_owner="owner-one",
            clear_poll_lease=True,
        )
        self.assertEqual(updated["status"], "READY_FOR_IMPORT")
        self.assertNotIn("poll_owner_token", updated)
        self.assertFalse(self.repository.release_discovery_poll(RUN_ID, "owner-one"))

    def test_expired_or_incomplete_poll_lease_can_be_reclaimed(self):
        self._create_run()
        self.repository.transition_discovery_run(RUN_ID, {"CREATED"}, "COLLECTING")
        contender = Repository(self.database_path)
        self.assertTrue(
            self.repository.claim_discovery_poll(
                RUN_ID,
                "owner-old",
                "2026-08-27T00:01:00+00:00",
                "2026-08-27T00:00:00+00:00",
            )
        )

        self.assertTrue(
            contender.claim_discovery_poll(
                RUN_ID,
                "owner-new",
                "2026-08-27T00:10:00+00:00",
                "2026-08-27T00:02:00+00:00",
            )
        )
        self.assertFalse(
            self.repository.renew_discovery_poll(
                RUN_ID, "owner-old", "2026-08-27T00:11:00+00:00"
            )
        )
        self.assertFalse(self.repository.release_discovery_poll(RUN_ID, "owner-old"))
        self.assertTrue(contender.release_discovery_poll(RUN_ID, "owner-new"))

    def test_repository_rejects_invalid_run_ids_and_statuses(self):
        for invalid_id in ("../run", "discovery_run_" + "A" * 32, "rewrite_run_" + "a" * 32):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaisesRegex(ValueError, "invalid discovery run id"):
                    self._create_run(run_id=invalid_id)
        self._create_run()
        for expected, new_status in [
            (set(), "COLLECTING"),
            ({"UNKNOWN"}, "COLLECTING"),
            ({"CREATED"}, "UNKNOWN"),
            ({"CREATED"}, "IMPORTED"),
        ]:
            with self.subTest(expected=expected, new_status=new_status):
                with self.assertRaisesRegex(ValueError, "discovery run"):
                    self.repository.transition_discovery_run(
                        RUN_ID, expected, new_status
                    )

    def test_mixed_expected_statuses_cannot_revive_an_imported_run(self):
        self._create_run()
        self.repository.transition_discovery_run(RUN_ID, {"CREATED"}, "COLLECTING")
        self.repository.transition_discovery_run(
            RUN_ID, {"COLLECTING"}, "READY_FOR_IMPORT"
        )
        self.repository.transition_discovery_run(
            RUN_ID, {"READY_FOR_IMPORT"}, "IMPORTED"
        )

        with self.assertRaisesRegex(ValueError, "discovery run"):
            self.repository.transition_discovery_run(
                RUN_ID, {"CREATED", "IMPORTED"}, "COLLECTING"
            )
        self.assertEqual(
            self.repository.get_discovery_run(RUN_ID)["status"], "IMPORTED"
        )

    def test_run_id_status_and_schema_policy_have_one_shared_contract(self):
        from resume_agent import discovery_artifacts, discovery_schemas, repository

        run_pattern = getattr(discovery_schemas, "DISCOVERY_RUN_ID_PATTERN", None)
        statuses = getattr(discovery_schemas, "DISCOVERY_RUN_STATUSES", None)
        transitions = getattr(discovery_schemas, "DISCOVERY_STATUS_TRANSITIONS", None)
        self.assertIsNotNone(run_pattern, "shared discovery run ID policy is missing")
        self.assertIsNotNone(statuses, "shared discovery status policy is missing")
        self.assertIsNotNone(transitions, "shared discovery transition policy is missing")
        self.assertIs(repository.DISCOVERY_RUN_ID, run_pattern)
        self.assertIs(repository.DISCOVERY_RUN_STATUSES, statuses)
        self.assertIs(repository.DISCOVERY_STATUS_TRANSITIONS, transitions)
        self.assertIs(discovery_artifacts.DISCOVERY_RUN_ID, run_pattern)
        self.assertIs(discovery_artifacts.AUDIT_STATUSES, statuses)
        self.assertEqual(set(get_args(discovery_schemas.DiscoveryRunStatus)), statuses)

        schema = (Path(repository.__file__).with_name("schema.sql")).read_text(
            encoding="utf-8"
        )
        definition = re.search(
            r"CREATE TABLE IF NOT EXISTS discovery_runs .*?"
            r"status TEXT NOT NULL CHECK\(status IN \((.*?)\)\)",
            schema,
            re.DOTALL,
        )
        self.assertIsNotNone(definition)
        self.assertEqual(set(re.findall(r"'([^']+)'", definition.group(1))), statuses)


class DiscoveryArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runs_root = self.root / "runs"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_open_and_writes_are_deterministic_utf8(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        artifacts.write_json("input-snapshot.json", {"z": 1, "a": "中文"})
        artifacts.write_results([_job()])
        artifacts.write_text("discovery-report.md", "# 发现报告\n")
        reopened = DiscoveryArtifacts.open(self.runs_root, RUN_ID)

        self.assertEqual(
            reopened.path("input-snapshot.json").read_text(encoding="utf-8"),
            '{\n  "a": "中文",\n  "z": 1\n}\n',
        )
        results = json.loads(
            reopened.path("source-results.json").read_text(encoding="utf-8")
        )
        self.assertEqual(results[0]["source_job_id"], "nc-1")
        self.assertFalse(list(reopened.directory.glob(".tmp-*")))

    def test_open_does_not_create_a_missing_run(self):
        DiscoveryArtifacts = _artifacts_class()
        with self.assertRaisesRegex(ValueError, "run directory"):
            DiscoveryArtifacts.open(self.runs_root, RUN_ID)
        self.assertFalse((self.runs_root / RUN_ID).exists())

    def test_unknown_binary_and_reserved_artifact_writes_are_rejected(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        with self.assertRaisesRegex(ValueError, "artifact"):
            artifacts.write_text("cookie.txt", "secret")
        with self.assertRaisesRegex(ValueError, "artifact"):
            artifacts.write_text("audit.md", "arbitrary message")
        with self.assertRaisesRegex(ValueError, "UTF-8 text"):
            artifacts.write_text("discovery-report.md", b"\x00\xff")

    def test_symlinked_or_tampered_paths_cannot_escape_runs_root(self):
        DiscoveryArtifacts = _artifacts_class()
        outside = self.root / "outside"
        outside.mkdir()
        self.runs_root.mkdir()
        run_path = self.runs_root / RUN_ID
        try:
            run_path.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "run directory"):
            DiscoveryArtifacts.open(self.runs_root, RUN_ID)
        run_path.unlink()

        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        object.__setattr__(artifacts, "directory", outside)
        with self.assertRaisesRegex(ValueError, "run directory"):
            artifacts.write_text("discovery-report.md", "must stay contained")
        self.assertFalse((outside / "discovery-report.md").exists())

    def test_artifact_symlink_cannot_overwrite_an_external_file(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        external = self.root / "external.json"
        external.write_text("keep", encoding="utf-8")
        link = artifacts.directory / "source-results.json"
        try:
            link.symlink_to(external)
        except OSError as error:
            self.skipTest(f"file symlinks unavailable: {error}")
        with self.assertRaisesRegex(ValueError, "artifact path"):
            artifacts.write_results([_job()])
        self.assertEqual(external.read_text(encoding="utf-8"), "keep")

    def test_audit_is_structured_whitelist_only_and_rejects_secrets(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        artifacts.append_audit(
            run_id=RUN_ID,
            status="COLLECTING",
            event_code="SOURCE_POLLED",
            source="boss",
            count=3,
            error_code="SOURCE_INTERACTION_REQUIRED",
        )
        event = json.loads(artifacts.path("audit.md").read_text(encoding="utf-8"))
        self.assertEqual(
            set(event),
            {
                "run_id",
                "status",
                "event_code",
                "timestamp_utc",
                "source",
                "count",
                "error_code",
            },
        )
        self.assertTrue(event["timestamp_utc"].endswith("+00:00"))

        before = artifacts.path("audit.md").read_text(encoding="utf-8")
        unsafe_values = [
            {"source": "https://jobs.test/?token=secret"},
            {"error_code": "cookie=session-secret"},
            {"event_code": "person@example.com"},
        ]
        for values in unsafe_values:
            arguments = {
                "run_id": RUN_ID,
                "status": "FAILED",
                "event_code": "RUN_FAILED",
            }
            arguments.update(values)
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, "audit"):
                    artifacts.append_audit(**arguments)
        with self.assertRaises(TypeError):
            artifacts.append_audit(
                run_id=RUN_ID,
                status="FAILED",
                event_code="RUN_FAILED",
                message="raw JD, phone 13800138000 and Cookie=secret",
            )
        self.assertEqual(
            artifacts.path("audit.md").read_text(encoding="utf-8"), before
        )

    def test_append_rejects_a_tampered_audit_timestamp_without_copying_it(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        tampered = {
            "run_id": RUN_ID,
            "status": "CREATED",
            "event_code": "RUN_CREATED",
            "timestamp_utc": "person@example.com+00:00",
        }
        artifacts.path("audit.md").write_text(
            json.dumps(tampered) + "\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(ValueError, "audit timestamp"):
            artifacts.append_audit(
                run_id=RUN_ID,
                status="COLLECTING",
                event_code="COLLECTION_STARTED",
            )
        self.assertEqual(
            artifacts.path("audit.md").read_text(encoding="utf-8"),
            json.dumps(tampered) + "\n",
        )

    def test_append_rejects_duplicate_audit_keys_hiding_forbidden_content(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        raw = (
            '{"run_id":"' + RUN_ID + '","status":"CREATED",'
            '"event_code":"RUN_CREATED",'
            '"timestamp_utc":"2026-08-26T00:00:00+00:00",'
            '"source":"https://jobs.test/?token=secret","source":"boss"}\n'
        )
        artifacts.path("audit.md").write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "audit"):
            artifacts.append_audit(
                run_id=RUN_ID,
                status="COLLECTING",
                event_code="COLLECTION_STARTED",
            )
        self.assertEqual(artifacts.path("audit.md").read_text("utf-8"), raw)

    def test_append_rejects_existing_audit_row_for_another_run(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        other_run_id = "discovery_run_" + "b" * 32
        raw = json.dumps(
            {
                "run_id": other_run_id,
                "status": "CREATED",
                "event_code": "RUN_CREATED",
                "timestamp_utc": "2026-08-26T00:00:00+00:00",
            }
        ) + "\n"
        artifacts.path("audit.md").write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "audit run id"):
            artifacts.append_audit(
                run_id=RUN_ID,
                status="COLLECTING",
                event_code="COLLECTION_STARTED",
            )
        self.assertEqual(artifacts.path("audit.md").read_text("utf-8"), raw)

    def test_append_canonically_reserializes_valid_existing_rows(self):
        DiscoveryArtifacts = _artifacts_class()
        artifacts = DiscoveryArtifacts.create(self.runs_root, RUN_ID)
        previous = {
            "timestamp_utc": "2026-08-26T00:00:00+00:00",
            "event_code": "RUN_CREATED",
            "status": "CREATED",
            "run_id": RUN_ID,
        }
        raw = json.dumps(previous, separators=(", ", ": ")) + "   \n"
        artifacts.path("audit.md").write_text(raw, encoding="utf-8")
        artifacts.append_audit(
            run_id=RUN_ID,
            status="COLLECTING",
            event_code="COLLECTION_STARTED",
        )
        lines = artifacts.path("audit.md").read_text("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotEqual(lines[0], raw.rstrip("\n"))
        for line in lines:
            self.assertEqual(line, json.dumps(json.loads(line), sort_keys=True))


if __name__ == "__main__":
    unittest.main()
