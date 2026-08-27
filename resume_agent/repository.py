import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from resume_agent.discovery_schemas import (
    DISCOVERY_RUN_ID_PATTERN,
    DISCOVERY_RUN_STATUSES,
    DISCOVERY_STATUS_TRANSITIONS,
)


DISCOVERY_RUN_ID = DISCOVERY_RUN_ID_PATTERN


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class NotFoundError(LookupError):
    pass


class Repository:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.schema_path = Path(__file__).with_name("schema.sql")
        self._initialized = False
        self._init_lock = threading.Lock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=10)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            if not self._initialized:
                with self._init_lock:
                    if not self._initialized:
                        connection.execute("PRAGMA journal_mode = WAL")
                        connection.executescript(
                            self.schema_path.read_text(encoding="utf-8")
                        )
                        self._ensure_schema_compatibility(connection)
                        self._initialized = True
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _ensure_schema_compatibility(connection: sqlite3.Connection):
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        required_columns = {
            "score": "REAL",
            "qualified": "INTEGER",
            "search_keyword": "TEXT",
            "job_fingerprint": "TEXT",
            "source_logged_at": "TEXT",
        }
        for column, column_type in required_columns.items():
            if column not in existing_columns:
                connection.execute(
                    f"ALTER TABLE jobs ADD COLUMN {column} {column_type}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint "
            "ON jobs(candidate_id, job_fingerprint)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_qualified "
            "ON jobs(candidate_id, qualified)"
        )
        rewrite_run_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(rewrite_runs)").fetchall()
        }
        if "blocking_questions_json" not in rewrite_run_columns:
            connection.execute(
                "ALTER TABLE rewrite_runs ADD COLUMN "
                "blocking_questions_json TEXT NOT NULL DEFAULT '[]'"
            )
        for column in ("owner_token", "lease_expires_at"):
            if column not in rewrite_run_columns:
                connection.execute(
                    f"ALTER TABLE rewrite_runs ADD COLUMN {column} TEXT"
                )
        discovery_run_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(discovery_runs)"
            ).fetchall()
        }
        for column in ("poll_owner_token", "poll_lease_expires_at"):
            if column not in discovery_run_columns:
                connection.execute(
                    f"ALTER TABLE discovery_runs ADD COLUMN {column} TEXT"
                )

    @staticmethod
    def _row(row: sqlite3.Row | None, kind: str, object_id: str) -> dict[str, Any]:
        if row is None:
            raise NotFoundError(f"{kind}不存在: {object_id}")
        return dict(row)

    @staticmethod
    def _decode_rewrite_run(row: sqlite3.Row | None, run_id: str) -> dict[str, Any]:
        data = Repository._row(row, "简历改写任务", run_id)
        data["job_ids"] = json.loads(data.pop("job_ids_json"))
        data["source_resume_ids"] = json.loads(data.pop("source_resume_ids_json"))
        data["fact_answers"] = json.loads(data.pop("fact_answers_json"))
        data["blocking_questions"] = json.loads(
            data.pop("blocking_questions_json")
        )
        return data

    @staticmethod
    def _decode_discovery_run(
        row: sqlite3.Row | None, run_id: str
    ) -> dict[str, Any]:
        if row is None:
            raise NotFoundError("discovery run not found")
        data = dict(row)
        data.pop("poll_owner_token", None)
        data.pop("poll_lease_expires_at", None)
        for name in (
            "request",
            "source_checkpoints",
            "source_statuses",
            "imported_job_ids",
        ):
            data[name] = json.loads(data.pop(f"{name}_json"))
        return data

    @staticmethod
    def _validate_discovery_run_id(run_id: str) -> None:
        if not isinstance(run_id, str) or not DISCOVERY_RUN_ID.fullmatch(run_id):
            raise ValueError("invalid discovery run id")

    @staticmethod
    def _encode_discovery_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _validate_discovery_poll_owner(owner_token: str) -> None:
        if (
            not isinstance(owner_token, str)
            or not owner_token
            or len(owner_token) > 200
        ):
            raise ValueError("invalid discovery poll owner")

    @staticmethod
    def _canonical_discovery_poll_time(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("invalid discovery poll lease")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid discovery poll lease") from None
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("invalid discovery poll lease")
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")

    def create_candidate(
        self, display_name: str | None, target_roles: list[str], notes: str
    ) -> dict[str, Any]:
        candidate_id = new_id("candidate")
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO candidates(id, display_name, target_roles_json, notes, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    display_name,
                    json.dumps(target_roles, ensure_ascii=False),
                    notes,
                    created_at,
                ),
            )
        return self.get_candidate(candidate_id)

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        data = self._row(row, "候选人", candidate_id)
        data["target_roles"] = json.loads(data.pop("target_roles_json"))
        return data

    def create_source_resume(
        self, candidate_id: str, label: str, content: str
    ) -> dict[str, Any]:
        self.get_candidate(candidate_id)
        resume_id = new_id("source_resume")
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO source_resumes(
                    id, candidate_id, label, content, content_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    resume_id,
                    candidate_id,
                    label,
                    content,
                    content_sha256,
                    created_at,
                ),
            )
        return self.get_source_resume(resume_id)

    def get_source_resume(self, resume_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM source_resumes WHERE id = ?", (resume_id,)
            ).fetchone()
        return self._row(row, "原始简历", resume_id)

    def list_source_resumes(self, candidate_id: str) -> list[dict[str, Any]]:
        self.get_candidate(candidate_id)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM source_resumes
                WHERE candidate_id = ?
                ORDER BY created_at ASC
                """,
                (candidate_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = payload.get("candidate_id")
        if candidate_id:
            self.get_candidate(candidate_id)
        job_id = new_id("job")
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    id, candidate_id, external_job_id, source, title,
                    salary, detail, company, score, qualified, search_keyword,
                    job_fingerprint, source_logged_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    candidate_id,
                    payload.get("external_job_id"),
                    payload["source"],
                    payload["title"],
                    payload.get("salary", ""),
                    payload["detail"],
                    payload.get("company", ""),
                    payload.get("score"),
                    payload.get("qualified"),
                    payload.get("search_keyword"),
                    payload.get("job_fingerprint"),
                    payload.get("source_logged_at"),
                    created_at,
                ),
            )
        return self.get_job(job_id)

    def create_job_if_absent(
        self, payload: dict[str, Any], fingerprint: str
    ) -> tuple[dict[str, Any], bool]:
        candidate_id = payload.get("candidate_id")
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate_id is required")
        if (
            not isinstance(fingerprint, str)
            or not fingerprint
            or len(fingerprint) > 128
        ):
            raise ValueError("invalid job fingerprint")
        self.get_candidate(candidate_id)
        job_id = new_id("job")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE candidate_id = ? AND job_fingerprint = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (candidate_id, fingerprint),
            ).fetchone()
            if row is not None:
                return dict(row), False
            connection.execute(
                """
                INSERT INTO jobs(
                    id, candidate_id, external_job_id, source, title,
                    salary, detail, company, score, qualified, search_keyword,
                    job_fingerprint, source_logged_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    candidate_id,
                    payload.get("external_job_id"),
                    payload["source"],
                    payload["title"],
                    payload.get("salary", ""),
                    payload["detail"],
                    payload.get("company", ""),
                    payload.get("score"),
                    payload.get("qualified"),
                    payload.get("search_keyword"),
                    fingerprint,
                    payload.get("source_logged_at"),
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return dict(row), True

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return self._row(row, "岗位", job_id)

    def get_jobs(self, job_ids: list[str]) -> list[dict[str, Any]]:
        jobs = [self.get_job(job_id) for job_id in job_ids]
        return jobs

    def list_jobs(
        self,
        candidate_id: str,
        qualified_only: bool = False,
        search_keyword: str | None = None,
    ) -> list[dict[str, Any]]:
        self.get_candidate(candidate_id)
        conditions = ["candidate_id = ?"]
        parameters: list[Any] = [candidate_id]
        if qualified_only:
            conditions.append("qualified = 1")
        if search_keyword:
            conditions.append("search_keyword = ?")
            parameters.append(search_keyword)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE {' AND '.join(conditions)}
                ORDER BY COALESCE(source_logged_at, created_at) ASC
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def find_job_by_fingerprint(
        self, candidate_id: str, job_fingerprint: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE candidate_id = ? AND job_fingerprint = ?
                LIMIT 1
                """,
                (candidate_id, job_fingerprint),
            ).fetchone()
        return dict(row) if row else None

    def create_research_report(
        self,
        candidate_id: str,
        target_role: str,
        job_ids: list[str],
        keyword_counts: list[dict[str, Any]],
        manual_findings: list[dict[str, Any]],
        notes: str,
    ) -> dict[str, Any]:
        self.get_candidate(candidate_id)
        report_id = new_id("research")
        created_at = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO research_reports(
                    id, candidate_id, target_role, job_ids_json,
                    keyword_counts_json, manual_findings_json, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    candidate_id,
                    target_role,
                    json.dumps(job_ids, ensure_ascii=False),
                    json.dumps(keyword_counts, ensure_ascii=False),
                    json.dumps(manual_findings, ensure_ascii=False),
                    notes,
                    created_at,
                ),
            )
        return self.get_research_report(report_id)

    def get_research_report(self, report_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM research_reports WHERE id = ?", (report_id,)
            ).fetchone()
        data = self._row(row, "调研报告", report_id)
        data["job_ids"] = json.loads(data.pop("job_ids_json"))
        data["keyword_counts"] = json.loads(data.pop("keyword_counts_json"))
        data["manual_findings"] = json.loads(data.pop("manual_findings_json"))
        return data

    def create_discovery_run(
        self,
        run_id: str,
        *,
        candidate_id: str,
        request: dict[str, Any],
        artifact_dir: str,
    ) -> dict[str, Any]:
        self._validate_discovery_run_id(run_id)
        if not isinstance(request, dict):
            raise ValueError("invalid discovery request")
        if not isinstance(artifact_dir, str) or not artifact_dir:
            raise ValueError("invalid discovery artifact directory")
        self.get_candidate(candidate_id)
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO discovery_runs(
                    id, candidate_id, request_json, status,
                    source_checkpoints_json, source_statuses_json,
                    imported_job_ids_json, artifact_dir, error_code,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'CREATED', '{}', '{}', '[]', ?, NULL, ?, ?)
                """,
                (
                    run_id,
                    candidate_id,
                    self._encode_discovery_json(request),
                    artifact_dir,
                    now,
                    now,
                ),
            )
        return self.get_discovery_run(run_id)

    def get_discovery_run(self, run_id: str) -> dict[str, Any]:
        self._validate_discovery_run_id(run_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM discovery_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._decode_discovery_run(row, run_id)

    def transition_discovery_run(
        self,
        run_id: str,
        expected_statuses: set[str],
        new_status: str,
        *,
        source_checkpoints: dict[str, Any] | None = None,
        source_statuses: dict[str, str] | None = None,
        imported_job_ids: list[str] | None = None,
        error_code: str | None = None,
        expected_poll_owner: str | None = None,
        clear_poll_lease: bool = False,
    ) -> dict[str, Any]:
        self._validate_discovery_run_id(run_id)
        if (
            not isinstance(expected_statuses, set)
            or not expected_statuses
            or not expected_statuses <= DISCOVERY_RUN_STATUSES
            or new_status not in DISCOVERY_RUN_STATUSES
            or not all(
                new_status in DISCOVERY_STATUS_TRANSITIONS[status]
                for status in expected_statuses
            )
        ):
            raise ValueError("invalid discovery run status transition")
        if source_checkpoints is not None and not isinstance(
            source_checkpoints, dict
        ):
            raise ValueError("invalid discovery source checkpoints")
        if source_statuses is not None and not isinstance(source_statuses, dict):
            raise ValueError("invalid discovery source statuses")
        if imported_job_ids is not None and (
            not isinstance(imported_job_ids, list)
            or any(not isinstance(job_id, str) for job_id in imported_job_ids)
        ):
            raise ValueError("invalid imported job ids")
        if error_code is not None and not isinstance(error_code, str):
            raise ValueError("invalid discovery error code")
        if not isinstance(clear_poll_lease, bool):
            raise ValueError("invalid discovery poll lease")
        if expected_poll_owner is not None:
            self._validate_discovery_poll_owner(expected_poll_owner)
        if clear_poll_lease and expected_poll_owner is None:
            raise ValueError("invalid discovery poll lease")

        assignments = ["status = ?", "updated_at = ?", "error_code = ?"]
        parameters: list[Any] = [new_status, utc_now(), error_code]
        if clear_poll_lease:
            assignments.extend(
                ["poll_owner_token = NULL", "poll_lease_expires_at = NULL"]
            )
        for column, value in (
            ("source_checkpoints_json", source_checkpoints),
            ("source_statuses_json", source_statuses),
            ("imported_job_ids_json", imported_job_ids),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                parameters.append(self._encode_discovery_json(value))
        allowed = sorted(expected_statuses)
        status_placeholders = ", ".join("?" for _ in allowed)
        parameters.extend([run_id, *allowed])
        owner_condition = ""
        if expected_poll_owner is not None:
            owner_condition = " AND poll_owner_token = ?"
            parameters.append(expected_poll_owner)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE discovery_runs SET {', '.join(assignments)} "
                f"WHERE id = ? AND status IN ({status_placeholders})"
                f"{owner_condition}",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError("invalid discovery run state transition")
        return self.get_discovery_run(run_id)

    def publish_discovery_transition(
        self,
        run_id: str,
        expected_status: str,
        new_status: str,
        *,
        expected_poll_owner: str,
        source_checkpoints: dict[str, Any],
        source_statuses: dict[str, str],
        error_code: str | None,
        publish_results: Callable[[], None],
    ) -> dict[str, Any]:
        self._validate_discovery_run_id(run_id)
        self._validate_discovery_poll_owner(expected_poll_owner)
        if (
            expected_status not in DISCOVERY_RUN_STATUSES
            or new_status not in DISCOVERY_RUN_STATUSES
            or new_status not in DISCOVERY_STATUS_TRANSITIONS[expected_status]
        ):
            raise ValueError("invalid discovery run status transition")
        if not isinstance(source_checkpoints, dict):
            raise ValueError("invalid discovery source checkpoints")
        if not isinstance(source_statuses, dict):
            raise ValueError("invalid discovery source statuses")
        if error_code is not None and not isinstance(error_code, str):
            raise ValueError("invalid discovery error code")
        if not callable(publish_results):
            raise ValueError("invalid discovery results publisher")

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute(
                    """
                    SELECT status, poll_owner_token
                    FROM discovery_runs
                    WHERE id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if (
                    current is None
                    or current["status"] != expected_status
                    or current["poll_owner_token"] != expected_poll_owner
                ):
                    raise ValueError("invalid discovery publication owner")

                publish_results()
                cursor = connection.execute(
                    """
                    UPDATE discovery_runs
                    SET status = ?, updated_at = ?, error_code = ?,
                        source_checkpoints_json = ?, source_statuses_json = ?,
                        poll_owner_token = NULL, poll_lease_expires_at = NULL
                    WHERE id = ? AND status = ? AND poll_owner_token = ?
                    """,
                    (
                        new_status,
                        utc_now(),
                        error_code,
                        self._encode_discovery_json(source_checkpoints),
                        self._encode_discovery_json(source_statuses),
                        run_id,
                        expected_status,
                        expected_poll_owner,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("invalid discovery publication owner")
                published = connection.execute(
                    "SELECT * FROM discovery_runs WHERE id = ?", (run_id,)
                ).fetchone()
        except Exception:
            raise ValueError("discovery results publication failed") from None
        return self._decode_discovery_run(published, run_id)

    def claim_discovery_poll(
        self,
        run_id: str,
        owner_token: str,
        lease_expires_at: str,
        now: str,
    ) -> bool:
        self._validate_discovery_run_id(run_id)
        self._validate_discovery_poll_owner(owner_token)
        expires = self._canonical_discovery_poll_time(lease_expires_at)
        current = self._canonical_discovery_poll_time(now)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE discovery_runs
                SET poll_owner_token=?, poll_lease_expires_at=?, updated_at=?
                WHERE id=?
                  AND status IN ('COLLECTING', 'READY_FOR_IMPORT', 'PARTIAL')
                  AND (
                    poll_owner_token IS NULL
                    OR poll_lease_expires_at IS NULL
                    OR poll_lease_expires_at <= ?
                  )
                """,
                (owner_token, expires, current, run_id, current),
            )
        return cursor.rowcount == 1

    def renew_discovery_poll(
        self, run_id: str, owner_token: str, lease_expires_at: str
    ) -> bool:
        self._validate_discovery_run_id(run_id)
        self._validate_discovery_poll_owner(owner_token)
        expires = self._canonical_discovery_poll_time(lease_expires_at)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE discovery_runs
                SET poll_lease_expires_at=?, updated_at=?
                WHERE id=?
                  AND status IN ('COLLECTING', 'READY_FOR_IMPORT', 'PARTIAL')
                  AND poll_owner_token=?
                """,
                (expires, utc_now(), run_id, owner_token),
            )
        return cursor.rowcount == 1

    def release_discovery_poll(self, run_id: str, owner_token: str) -> bool:
        self._validate_discovery_run_id(run_id)
        self._validate_discovery_poll_owner(owner_token)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE discovery_runs
                SET poll_owner_token=NULL, poll_lease_expires_at=NULL, updated_at=?
                WHERE id=? AND poll_owner_token=?
                """,
                (utc_now(), run_id, owner_token),
            )
        return cursor.rowcount == 1

    def create_rewrite_run(
        self, payload: dict[str, Any], artifact_dir: str
    ) -> dict[str, Any]:
        return self.create_rewrite_run_with_id(
            new_id("rewrite_run"), payload, artifact_dir
        )

    def create_rewrite_run_with_id(
        self, run_id: str, payload: dict[str, Any], artifact_dir: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO rewrite_runs(
                    id, candidate_id, job_ids_json, source_resume_ids_json,
                    output_name, status, fact_answers_json, blocking_questions_json,
                    artifact_dir, attempt_count, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'QUEUED', ?, '[]', ?, 0, ?, ?)
                """,
                (
                    run_id,
                    payload["candidate_id"],
                    json.dumps(payload["job_ids"], ensure_ascii=False),
                    json.dumps(payload["source_resume_ids"], ensure_ascii=False),
                    payload["output_name"],
                    json.dumps(payload.get("fact_answers", []), ensure_ascii=False),
                    artifact_dir,
                    now,
                    now,
                ),
            )
        return self.get_rewrite_run(run_id)

    def get_rewrite_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM rewrite_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return self._decode_rewrite_run(row, run_id)

    def transition_rewrite_run(
        self,
        run_id: str,
        expected_statuses: set[str],
        new_status: str,
        *,
        fact_answers: list[dict[str, Any]] | None = None,
        blocking_questions: list[dict[str, Any]] | None = None,
        error_code: str | None = None,
        error_summary: str = "",
        increment_attempt: bool = False,
        owner_token: str | None = None,
        lease_expires_at: str | None = None,
        expected_owner_token: str | None = None,
    ) -> dict[str, Any]:
        if not expected_statuses:
            raise ValueError("expected_statuses 不能为空")
        assignments = [
            "status = ?", "updated_at = ?", "error_code = ?", "error_summary = ?",
            "owner_token = ?", "lease_expires_at = ?",
        ]
        parameters: list[Any] = [
            new_status, utc_now(), error_code, error_summary,
            owner_token, lease_expires_at,
        ]
        if fact_answers is not None:
            assignments.append("fact_answers_json = ?")
            parameters.append(json.dumps(fact_answers, ensure_ascii=False))
        if blocking_questions is not None:
            assignments.append("blocking_questions_json = ?")
            parameters.append(json.dumps(blocking_questions, ensure_ascii=False))
        if increment_attempt:
            assignments.append("attempt_count = attempt_count + 1")
        allowed = sorted(expected_statuses)
        status_placeholders = ", ".join("?" for _ in allowed)
        parameters.extend([run_id, *allowed])
        owner_condition = ""
        if expected_owner_token is not None:
            owner_condition = " AND owner_token = ?"
            parameters.append(expected_owner_token)
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE rewrite_runs SET {', '.join(assignments)} "
                f"WHERE id = ? AND status IN ({status_placeholders}){owner_condition}",
                parameters,
            )
            if cursor.rowcount != 1:
                raise ValueError("非法的简历改写状态迁移")
        return self.get_rewrite_run(run_id)

    def list_recoverable_rewrite_runs(
        self, *, now: str, legacy_stale_before: str
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM rewrite_runs
                WHERE status='RUNNING'
                  AND (
                    (lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                    OR (
                        (owner_token IS NULL OR lease_expires_at IS NULL)
                        AND updated_at <= ?
                    )
                  )
                ORDER BY created_at, id
                """,
                (now, legacy_stale_before),
            ).fetchall()
        return [self._decode_rewrite_run(row, row["id"]) for row in rows]

    def claim_interrupted_rewrite_run(
        self,
        run_id: str,
        *,
        expected_owner_token: str | None,
        expected_lease_expires_at: str | None,
        recovery_owner_token: str,
        recovery_lease_expires_at: str,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE rewrite_runs
                SET owner_token=?, lease_expires_at=?, updated_at=?
                WHERE id=? AND status='RUNNING'
                  AND owner_token IS ? AND lease_expires_at IS ?
                """,
                (
                    recovery_owner_token,
                    recovery_lease_expires_at,
                    utc_now(),
                    run_id,
                    expected_owner_token,
                    expected_lease_expires_at,
                ),
            )
        return cursor.rowcount == 1

    def recover_interrupted_rewrite_run(
        self,
        run_id: str,
        *,
        expected_owner_token: str | None,
        expected_lease_expires_at: str | None,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE rewrite_runs
                SET status='FAILED', error_code='INTERRUPTED',
                    error_summary='上次进程在运行期间中断', updated_at=?,
                    owner_token=NULL, lease_expires_at=NULL
                WHERE id=? AND status='RUNNING'
                  AND owner_token IS ? AND lease_expires_at IS ?
                """,
                (
                    utc_now(), run_id, expected_owner_token,
                    expected_lease_expires_at,
                ),
            )
        return cursor.rowcount == 1

    def create_delivery_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = payload.get("candidate_id")
        job_id = payload.get("job_id")
        if candidate_id:
            self.get_candidate(candidate_id)
        if job_id:
            self.get_job(job_id)
        event_id = new_id("delivery_event")
        created_at = utc_now()
        event_at = payload.get("event_at") or created_at
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO delivery_events(
                    id, candidate_id, job_id, event_type, event_at,
                    source, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    candidate_id,
                    job_id,
                    payload["event_type"],
                    event_at,
                    payload.get("source", "manual"),
                    created_at,
                ),
            )
        return self.get_delivery_event(event_id)

    def get_delivery_event(self, event_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM delivery_events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._row(row, "投递事件", event_id)

    def list_delivery_events(
        self,
        candidate_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if candidate_id:
            conditions.append("candidate_id = ?")
            parameters.append(candidate_id)
        if date_from:
            conditions.append("event_at >= ?")
            parameters.append(date_from)
        if date_to:
            conditions.append("event_at <= ?")
            parameters.append(date_to)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM delivery_events {where} ORDER BY event_at ASC",
                parameters,
            ).fetchall()
        events = []
        for row in rows:
            events.append(dict(row))
        return events
