import hashlib
import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


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

    @staticmethod
    def _row(row: sqlite3.Row | None, kind: str, object_id: str) -> dict[str, Any]:
        if row is None:
            raise NotFoundError(f"{kind}不存在: {object_id}")
        return dict(row)

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
