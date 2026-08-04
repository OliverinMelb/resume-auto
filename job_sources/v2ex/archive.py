"""Durable local archive for bounded V2EX jobs Feed snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from job_sources.contracts import NormalizedJob

from .source import (
    MAX_CURSOR_IDENTITIES,
    V2exCursor,
    V2exFetchResult,
    V2exItemOutcome,
    V2exJobSource,
    V2exSignals,
)


DEFAULT_DATABASE_PATH = Path(".runtime/v2ex/jobs.db")
_BUSY_TIMEOUT_MILLISECONDS = 5_000


class V2exArchiveJobSource:
    """Fetch one Feed snapshot and return the complete local archive."""

    def __init__(
        self,
        *,
        database_path: str | Path | None = None,
        feed_source: V2exJobSource | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self._database_path = Path(
            database_path
            or os.environ.get("V2EX_ARCHIVE_DB_PATH")
            or DEFAULT_DATABASE_PATH
        )
        self._feed_source = feed_source or V2exJobSource()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def fetch(self) -> V2exFetchResult:
        observed_at = _utc_timestamp(self._now())
        feed_result = self._feed_source.fetch()
        connection: sqlite3.Connection | None = None
        try:
            self._database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = _connect(self._database_path)
            _create_schema(connection)
            existing = {
                row["canonical_topic_url"]
                for row in connection.execute(
                    "SELECT canonical_topic_url FROM archived_jobs"
                )
            }
            archived_by_topic_url: dict[str, V2exItemOutcome] = {}
            for outcome in feed_result.outcomes:
                if (
                    outcome.identity is not None
                    and outcome.source_url is not None
                ):
                    archived_by_topic_url[outcome.source_url] = outcome
            archived = tuple(archived_by_topic_url.values())
            with connection:
                for outcome in archived:
                    _upsert_outcome(connection, outcome, observed_at)
                connection.execute(
                    """
                    INSERT INTO fetch_runs (
                        started_at, finished_at, status,
                        discovered_count, outcome_count,
                        new_count, updated_count, skipped_count,
                        duplicate_count, seen_count, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observed_at,
                        observed_at,
                        feed_result.status,
                        feed_result.discovered_count,
                        len(feed_result.outcomes),
                        sum(
                            outcome.source_url not in existing
                            for outcome in archived
                        ),
                        sum(
                            outcome.source_url in existing
                            for outcome in archived
                        ),
                        len(feed_result.outcomes) - len(archived),
                        feed_result.duplicate_count,
                        feed_result.seen_count,
                        feed_result.error_code,
                    ),
                )
            outcomes = _read_outcomes(connection)
            new_count = sum(
                outcome.source_url not in existing
                for outcome in archived
            )
            updated_count = len(archived) - new_count
            identities = tuple(
                outcome.identity
                for outcome in outcomes
                if outcome.identity is not None
            )[:MAX_CURSOR_IDENTITIES]
            return V2exFetchResult(
                status=feed_result.status,
                outcomes=outcomes,
                discovered_count=feed_result.discovered_count,
                next_cursor=V2exCursor(identities),
                duplicate_count=feed_result.duplicate_count,
                seen_count=feed_result.seen_count,
                error_code=feed_result.error_code,
                new_count=new_count,
                updated_count=updated_count,
                skipped_count=len(feed_result.outcomes) - len(archived),
                stale=(feed_result.error_code is not None and bool(outcomes)),
            )
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            return V2exFetchResult(
                status="failed",
                outcomes=(),
                discovered_count=feed_result.discovered_count,
                next_cursor=V2exCursor(),
                duplicate_count=feed_result.duplicate_count,
                seen_count=feed_result.seen_count,
                error_code="local_storage_error",
            )
        finally:
            if connection is not None:
                connection.close()


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}"
    )
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS archived_jobs (
            canonical_topic_url TEXT PRIMARY KEY,
            feed_item_identity TEXT NOT NULL,
            source_url TEXT NOT NULL,
            classification TEXT NOT NULL,
            confidence TEXT NOT NULL,
            disposition TEXT NOT NULL,
            risk_tags_json TEXT NOT NULL,
            signals_json TEXT NOT NULL,
            safe_text TEXT NOT NULL,
            job_json TEXT,
            reason_codes_json TEXT NOT NULL,
            published_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fetch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            status TEXT NOT NULL,
            discovered_count INTEGER NOT NULL,
            outcome_count INTEGER NOT NULL,
            new_count INTEGER NOT NULL,
            updated_count INTEGER NOT NULL,
            skipped_count INTEGER NOT NULL,
            duplicate_count INTEGER NOT NULL,
            seen_count INTEGER NOT NULL,
            error_code TEXT
        );
        """
    )


def _upsert_outcome(
    connection: sqlite3.Connection,
    outcome: V2exItemOutcome,
    observed_at: str,
) -> None:
    if outcome.identity is None or outcome.source_url is None:
        return
    connection.execute(
        """
        INSERT INTO archived_jobs (
            canonical_topic_url, feed_item_identity, source_url,
            classification, confidence, disposition,
            risk_tags_json, signals_json, safe_text, job_json,
            reason_codes_json, published_at, first_seen_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(canonical_topic_url) DO UPDATE SET
            feed_item_identity = excluded.feed_item_identity,
            source_url = excluded.source_url,
            classification = excluded.classification,
            confidence = excluded.confidence,
            disposition = excluded.disposition,
            risk_tags_json = excluded.risk_tags_json,
            signals_json = excluded.signals_json,
            safe_text = excluded.safe_text,
            job_json = excluded.job_json,
            reason_codes_json = excluded.reason_codes_json,
            published_at = COALESCE(
                excluded.published_at,
                archived_jobs.published_at
            ),
            last_seen_at = excluded.last_seen_at
        """,
        (
            outcome.source_url,
            outcome.identity,
            outcome.source_url,
            outcome.classification,
            outcome.confidence,
            outcome.disposition,
            _json(outcome.risk_tags),
            _json(asdict(outcome.signals)),
            outcome.safe_text,
            None if outcome.job is None else _json(asdict(outcome.job)),
            _json(outcome.reason_codes),
            outcome.published_at,
            observed_at,
            observed_at,
        ),
    )


def _read_outcomes(
    connection: sqlite3.Connection,
) -> tuple[V2exItemOutcome, ...]:
    rows = connection.execute(
        """
        SELECT feed_item_identity, source_url, classification, confidence,
               disposition, risk_tags_json, signals_json, safe_text,
               job_json, reason_codes_json, published_at,
               first_seen_at, last_seen_at
        FROM archived_jobs
        ORDER BY COALESCE(published_at, last_seen_at) DESC,
                 last_seen_at DESC,
                 canonical_topic_url DESC
        """
    )
    return tuple(_row_to_outcome(row) for row in rows)


def _row_to_outcome(row: sqlite3.Row) -> V2exItemOutcome:
    signals_data = json.loads(str(row["signals_json"]))
    signals_data["locations"] = tuple(signals_data["locations"])
    job_json = row["job_json"]
    job_data = None if job_json is None else json.loads(str(job_json))
    if job_data is not None:
        for field in ("locations", "responsibilities", "requirements"):
            job_data[field] = tuple(job_data[field])
    return V2exItemOutcome(
        identity=str(row["feed_item_identity"]),
        source_url=str(row["source_url"]),
        classification=str(row["classification"]),
        confidence=str(row["confidence"]),
        disposition=str(row["disposition"]),
        risk_tags=tuple(json.loads(str(row["risk_tags_json"]))),
        signals=V2exSignals(**signals_data),
        safe_text=str(row["safe_text"]),
        job=None if job_data is None else NormalizedJob(**job_data),
        reason_codes=tuple(
            json.loads(str(row["reason_codes_json"]))
        ),
        published_at=(
            None
            if row["published_at"] is None
            else str(row["published_at"])
        ),
        first_seen_at=str(row["first_seen_at"]),
        last_seen_at=str(row["last_seen_at"]),
    )


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("archive clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat()


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    feed_source: V2exJobSource | None = None,
    now: Callable[[], datetime] | None = None,
    output: Callable[[str], object] = print,
) -> int:
    """Run one bounded archive attempt and print only safe counters."""
    parser = argparse.ArgumentParser(
        description="Archive one V2EX jobs Feed snapshot.",
    )
    parser.add_argument(
        "--database",
        help="SQLite archive path (overrides V2EX_ARCHIVE_DB_PATH)",
    )
    arguments = parser.parse_args(argv)
    environment = os.environ if environ is None else environ
    database_path = (
        arguments.database
        or environment.get("V2EX_ARCHIVE_DB_PATH")
        or DEFAULT_DATABASE_PATH
    )
    result = V2exArchiveJobSource(
        database_path=database_path,
        feed_source=feed_source,
        now=now,
    ).fetch()
    summary = (
        f"status={result.status} "
        f"discovered={result.discovered_count} "
        f"archived={len(result.outcomes)} "
        f"new={result.new_count} "
        f"updated={result.updated_count} "
        f"skipped={result.skipped_count} "
        f"duplicates={result.duplicate_count}"
    )
    if result.error_code is not None:
        summary += f" error={result.error_code}"
    output(summary)
    return 0 if result.error_code is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
