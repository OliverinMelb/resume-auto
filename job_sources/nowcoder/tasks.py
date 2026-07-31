"""Single-worker task coordination for browser-backed Nowcoder collection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import logging
from queue import Queue
from threading import Condition, Lock, Thread
from time import monotonic
from typing import Literal, Protocol, cast
from uuid import uuid4

from job_sources.contracts import (
    CollectionMode,
    CollectionResult,
    JobQuery,
    NormalizedJob,
)
from .models import NowcoderAcquisitionMode, NowcoderCollectionProgress
from .source import JobReference


TaskStatus = Literal[
    "pending",
    "running",
    "waiting_for_login",
    "verification_required",
    "paused",
    "completed",
    "failed",
    "stopped",
]
_logger = logging.getLogger(__name__)


class NowcoderCollectingSource(Protocol):
    def collect(
        self,
        query: JobQuery,
        *,
        mode: CollectionMode,
        acquisition_mode: NowcoderAcquisitionMode,
        filtered_url: str | None,
        should_stop: Callable[[], bool],
    ) -> CollectionResult: ...


class CheckpointStore(Protocol):
    def save(self, task_id: str, payload: dict[str, object]): ...

    def load_all(self) -> list[dict[str, object]]: ...


class ResearchSink(Protocol):
    def resolve_project(
        self,
        query: JobQuery,
        *,
        candidate_id: str | None,
        project_name: str | None,
    ) -> str: ...

    def persist(
        self,
        candidate_id: str,
        query: JobQuery,
        jobs: tuple[NormalizedJob, ...],
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class TaskSnapshot:
    task_id: str
    query: JobQuery
    mode: CollectionMode
    status: TaskStatus
    pages_read: int
    discovered_count: int
    deduplicated_count: int
    detail_success_count: int
    partial_detail_count: int
    detail_failure_count: int
    error_type: str | None
    stop_reason: str | None
    project_id: str | None = None
    persisted_count: int = 0
    acquisition_mode: NowcoderAcquisitionMode = "public"
    filtered_url: str | None = None


@dataclass
class _TaskRecord:
    task_id: str
    query: JobQuery
    mode: CollectionMode
    acquisition_mode: NowcoderAcquisitionMode = "public"
    filtered_url: str | None = None
    status: TaskStatus = "pending"
    result: CollectionResult | None = None
    stop_requested: bool = False
    error_type: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    persisted_job_ids: tuple[str, ...] = ()
    progress: NowcoderCollectionProgress | None = None


class NowcoderTaskManager:
    """Queues Nowcoder collection behind one source-local worker."""

    def __init__(
        self,
        source: NowcoderCollectingSource,
        *,
        checkpoint_store: CheckpointStore | None = None,
        research_sink: ResearchSink | None = None,
    ):
        self._source = source
        self._checkpoint_store = checkpoint_store
        self._research_sink = research_sink
        self._records: dict[str, _TaskRecord] = {}
        self._queue: Queue[str | None] = Queue()
        self._lock = Lock()
        self._changed = Condition(self._lock)
        self._closed = False
        self._restore_checkpoints()
        self._worker = Thread(
            target=self._work_loop,
            name="nowcoder-source-worker",
            daemon=True,
        )
        self._worker.start()

    def create_task(
        self,
        query: JobQuery,
        *,
        mode: CollectionMode,
        acquisition_mode: NowcoderAcquisitionMode = "public",
        filtered_url: str | None = None,
        candidate_id: str | None = None,
        project_name: str | None = None,
    ) -> TaskSnapshot:
        if mode not in {"quick", "research"}:
            raise ValueError(f"unsupported collection mode: {mode}")
        if acquisition_mode not in {"public", "browser"}:
            raise ValueError(
                f"unsupported acquisition mode: {acquisition_mode}"
            )
        with self._changed:
            if self._closed:
                raise RuntimeError("Nowcoder task manager is closed")
            task_id = uuid4().hex
            record = _TaskRecord(
                task_id=task_id,
                query=query,
                mode=mode,
                acquisition_mode=acquisition_mode,
                filtered_url=filtered_url,
                project_id=candidate_id,
                project_name=project_name,
            )
            self._records[task_id] = record
            self._save_checkpoint(record)
            snapshot = self._snapshot(record)
            self._queue.put(task_id)
            self._changed.notify_all()
            return snapshot

    def get_task(self, task_id: str) -> TaskSnapshot:
        with self._lock:
            return self._snapshot(self._get_record(task_id))

    def list_tasks(self) -> tuple[TaskSnapshot, ...]:
        with self._lock:
            return tuple(
                self._snapshot(record)
                for record in reversed(tuple(self._records.values()))
            )

    def list_jobs(self, task_id: str) -> tuple[NormalizedJob, ...]:
        with self._lock:
            record = self._get_record(task_id)
            return record.result.jobs if record.result is not None else ()

    def stop_task(self, task_id: str) -> TaskSnapshot:
        with self._changed:
            record = self._get_record(task_id)
            record.stop_requested = True
            if record.status == "pending":
                record.status = "stopped"
            self._save_checkpoint(record)
            self._changed.notify_all()
            return self._snapshot(record)

    def resume_task(self, task_id: str) -> TaskSnapshot:
        with self._changed:
            record = self._get_record(task_id)
            if record.status not in {
                "paused",
                "failed",
                "waiting_for_login",
                "verification_required",
            }:
                raise ValueError(
                    f"Nowcoder task cannot resume from {record.status}"
                )
            record.status = "pending"
            record.stop_requested = False
            record.error_type = None
            self._save_checkpoint(record)
            self._queue.put(task_id)
            self._changed.notify_all()
            return self._snapshot(record)

    def wait_for_task(
        self,
        task_id: str,
        *,
        timeout: float,
    ) -> TaskSnapshot:
        deadline = monotonic() + timeout
        terminal = {"completed", "failed", "stopped"}
        with self._changed:
            while True:
                record = self._get_record(task_id)
                if record.status in terminal:
                    return self._snapshot(record)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Nowcoder task {task_id} did not finish in time"
                    )
                self._changed.wait(remaining)

    def close(self):
        with self._changed:
            if self._closed:
                return
            self._closed = True
            self._queue.put(None)
            self._changed.notify_all()
        self._worker.join(timeout=2)

    def _work_loop(self):
        while True:
            task_id = self._queue.get()
            if task_id is None:
                return
            with self._changed:
                record = self._records.get(task_id)
                if record is None or record.status == "stopped":
                    self._changed.notify_all()
                    continue
                record.status = "running"
                self._save_checkpoint(record)
                self._changed.notify_all()
            started = monotonic()
            _logger.info(
                "source=nowcoder task_id=%s mode=%s started_at=%s",
                task_id,
                record.mode,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            try:
                if (
                    record.mode == "research"
                    and self._research_sink is not None
                    and record.project_id is None
                ):
                    project_id = self._research_sink.resolve_project(
                        record.query,
                        candidate_id=None,
                        project_name=record.project_name,
                    )
                    with self._changed:
                        record.project_id = project_id
                        self._save_checkpoint(record)
                        self._changed.notify_all()
                collect_resumable = getattr(
                    self._source,
                    "collect_resumable",
                    None,
                )
                if record.mode == "research" and callable(
                    collect_resumable
                ):
                    resumable_kwargs = {
                        "mode": record.mode,
                        "acquisition_mode": record.acquisition_mode,
                        "should_stop": lambda: self._is_stop_requested(
                            task_id
                        ),
                        "resume": record.progress,
                        "on_progress": lambda progress: (
                            self._handle_progress(task_id, progress)
                        ),
                    }
                    if record.filtered_url is not None:
                        resumable_kwargs["filtered_url"] = (
                            record.filtered_url
                        )
                    result = collect_resumable(
                        record.query,
                        **resumable_kwargs,
                    )
                else:
                    collect_kwargs = {
                        "mode": record.mode,
                        "acquisition_mode": record.acquisition_mode,
                        "should_stop": lambda: self._is_stop_requested(
                            task_id
                        ),
                    }
                    if record.filtered_url is not None:
                        collect_kwargs["filtered_url"] = record.filtered_url
                    result = self._source.collect(
                        record.query,
                        **collect_kwargs,
                    )
                persisted_job_ids: tuple[str, ...] = ()
                if (
                    record.mode == "research"
                    and self._research_sink is not None
                    and record.project_id is not None
                ):
                    persisted_job_ids = self._research_sink.persist(
                        record.project_id,
                        record.query,
                        result.jobs,
                    )
            except Exception as error:
                with self._changed:
                    record.error_type = type(error).__name__
                    record.status = self._error_status(record, error)
                    self._save_checkpoint(record)
                    _logger.warning(
                        "source=nowcoder task_id=%s finished_at=%s "
                        "status=%s error_type=%s elapsed_ms=%d",
                        task_id,
                        datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        record.status,
                        record.error_type,
                        int((monotonic() - started) * 1000),
                    )
                    self._changed.notify_all()
                continue
            with self._changed:
                record.result = result
                record.persisted_job_ids = persisted_job_ids
                if record.stop_requested:
                    record.status = "stopped"
                elif record.mode == "research" and not result.complete:
                    record.status = "paused"
                else:
                    record.status = "completed"
                self._save_checkpoint(record)
                _logger.info(
                    "source=nowcoder task_id=%s finished_at=%s status=%s "
                    "success_count=%d partial_count=%d failure_count=%d "
                    "elapsed_ms=%d",
                    task_id,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    record.status,
                    result.detail_success_count,
                    result.partial_detail_count,
                    result.detail_failure_count,
                    int((monotonic() - started) * 1000),
                )
                self._changed.notify_all()

    def _is_stop_requested(self, task_id: str) -> bool:
        with self._lock:
            record = self._records.get(task_id)
            return record is None or record.stop_requested or self._closed

    def _handle_progress(
        self,
        task_id: str,
        progress: NowcoderCollectionProgress,
    ):
        with self._lock:
            record = self._get_record(task_id)
            project_id = record.project_id
            query = record.query
        persisted_ids: tuple[str, ...] = ()
        if (
            self._research_sink is not None
            and project_id is not None
            and progress.jobs
        ):
            persisted_ids = self._research_sink.persist(
                project_id,
                query,
                progress.jobs,
            )
        with self._changed:
            record = self._get_record(task_id)
            record.progress = progress
            if persisted_ids:
                record.persisted_job_ids = persisted_ids
            self._save_checkpoint(record)
            self._changed.notify_all()

    def _get_record(self, task_id: str) -> _TaskRecord:
        record = self._records.get(task_id)
        if record is None:
            raise KeyError(f"unknown Nowcoder task: {task_id}")
        return record

    @staticmethod
    def _snapshot(record: _TaskRecord) -> TaskSnapshot:
        result = record.result
        progress = record.progress
        return TaskSnapshot(
            task_id=record.task_id,
            query=record.query,
            mode=record.mode,
            status=record.status,
            pages_read=(
                result.pages_read
                if result is not None
                else (progress.pages_read if progress is not None else 0)
            ),
            discovered_count=(
                result.discovered_count
                if result is not None
                else (
                    progress.discovered_count
                    if progress is not None
                    else 0
                )
            ),
            deduplicated_count=(
                result.deduplicated_count
                if result is not None
                else (
                    len(progress.references)
                    if progress is not None
                    else 0
                )
            ),
            detail_success_count=(
                result.detail_success_count
                if result is not None
                else (
                    sum(
                        job.detail_status == "complete"
                        for job in progress.jobs
                    )
                    if progress is not None
                    else 0
                )
            ),
            partial_detail_count=(
                result.partial_detail_count
                if result is not None
                else (
                    sum(
                        job.detail_status == "partial"
                        for job in progress.jobs
                    )
                    if progress is not None
                    else 0
                )
            ),
            detail_failure_count=(
                result.detail_failure_count
                if result is not None
                else (
                    len(progress.failed_detail_ids)
                    if progress is not None
                    else 0
                )
            ),
            error_type=record.error_type,
            stop_reason=result.stop_reason if result is not None else None,
            project_id=record.project_id,
            persisted_count=len(record.persisted_job_ids),
            acquisition_mode=record.acquisition_mode,
            filtered_url=record.filtered_url,
        )

    @staticmethod
    def _error_status(record: _TaskRecord, error: Exception) -> TaskStatus:
        if record.stop_requested:
            return "stopped"
        error_name = type(error).__name__
        if error_name == "BrowserLoginRequiredError":
            return "waiting_for_login"
        if error_name in {
            "BrowserVerificationRequiredError",
            "VerificationRequiredError",
        }:
            return "verification_required"
        if error_name == "SourceAccessRestrictedError":
            return "paused"
        return "failed"

    def _save_checkpoint(self, record: _TaskRecord):
        if self._checkpoint_store is None or record.mode != "research":
            return
        result = record.result
        self._checkpoint_store.save(
            record.task_id,
            {
                "version": 1,
                "task_id": record.task_id,
                "query": cast(dict[str, object], asdict(record.query)),
                "mode": record.mode,
                "acquisition_mode": record.acquisition_mode,
                "filtered_url": record.filtered_url,
                "status": record.status,
                "error_type": record.error_type,
                "project_id": record.project_id,
                "project_name": record.project_name,
                "persisted_job_ids": list(record.persisted_job_ids),
                "progress": (
                    _progress_to_dict(record.progress)
                    if record.progress is not None
                    else None
                ),
                "result": (
                    _result_to_dict(result)
                    if result is not None
                    else None
                ),
            },
        )

    def _restore_checkpoints(self):
        if self._checkpoint_store is None:
            return
        for payload in self._checkpoint_store.load_all():
            try:
                record = self._record_from_checkpoint(payload)
            except (KeyError, TypeError, ValueError):
                continue
            self._records[record.task_id] = record

    @staticmethod
    def _record_from_checkpoint(
        payload: dict[str, object],
    ) -> _TaskRecord:
        if payload.get("version") != 1:
            raise ValueError("unsupported checkpoint version")
        query_payload = cast(dict[str, object], payload["query"])
        query = JobQuery(**query_payload)
        mode = cast(CollectionMode, payload["mode"])
        acquisition_mode = cast(
            NowcoderAcquisitionMode,
            payload.get("acquisition_mode", "browser"),
        )
        if acquisition_mode not in {"public", "browser"}:
            raise ValueError("unsupported checkpoint acquisition mode")
        status = cast(TaskStatus, payload["status"])
        result_payload = payload.get("result")
        progress_payload = payload.get("progress")
        result = (
            _collection_result_from_dict(
                cast(dict[str, object], result_payload)
            )
            if isinstance(result_payload, dict)
            else None
        )
        if status not in {"completed", "stopped"}:
            status = "paused"
        return _TaskRecord(
            task_id=str(payload["task_id"]),
            query=query,
            mode=mode,
            acquisition_mode=acquisition_mode,
            filtered_url=(
                str(payload["filtered_url"])
                if payload.get("filtered_url")
                else None
            ),
            status=status,
            result=result,
            error_type=(
                str(payload["error_type"])
                if payload.get("error_type")
                else None
            ),
            project_id=(
                str(payload["project_id"])
                if payload.get("project_id")
                else None
            ),
            project_name=(
                str(payload["project_name"])
                if payload.get("project_name")
                else None
            ),
            persisted_job_ids=tuple(
                str(value)
                for value in cast(
                    list[object],
                    payload.get("persisted_job_ids", []),
                )
            ),
            progress=(
                _progress_from_dict(
                    cast(dict[str, object], progress_payload)
                )
                if isinstance(progress_payload, dict)
                else None
            ),
        )


def _collection_result_from_dict(
    payload: dict[str, object],
) -> CollectionResult:
    query = JobQuery(**cast(dict[str, object], payload["query"]))
    jobs = tuple(
        _normalized_job_from_dict(cast(dict[str, object], value))
        for value in cast(list[object], payload.get("jobs", []))
    )
    return CollectionResult(
        source=str(payload["source"]),
        query=query,
        jobs=jobs,
        pages_read=int(cast(int, payload["pages_read"])),
        discovered_count=int(cast(int, payload["discovered_count"])),
        deduplicated_count=int(cast(int, payload["deduplicated_count"])),
        detail_success_count=int(
            cast(int, payload["detail_success_count"])
        ),
        partial_detail_count=int(
            cast(int, payload["partial_detail_count"])
        ),
        detail_failure_count=int(
            cast(int, payload["detail_failure_count"])
        ),
        complete=bool(payload["complete"]),
        stop_reason=str(payload["stop_reason"]),
    )


def _normalized_job_from_dict(
    payload: dict[str, object],
) -> NormalizedJob:
    converted = dict(payload)
    for key in ("locations", "responsibilities", "requirements"):
        converted[key] = tuple(cast(list[object], converted.get(key, [])))
    return NormalizedJob(**converted)


def _progress_from_dict(
    payload: dict[str, object],
) -> NowcoderCollectionProgress:
    references = tuple(
        JobReference(**cast(dict[str, object], value))
        for value in cast(list[object], payload.get("references", []))
    )
    jobs = tuple(
        _normalized_job_from_dict(cast(dict[str, object], value))
        for value in cast(list[object], payload.get("jobs", []))
    )
    return NowcoderCollectionProgress(
        references=references,
        jobs=jobs,
        pages_read=int(cast(int, payload["pages_read"])),
        discovered_count=int(cast(int, payload["discovered_count"])),
        search_complete=bool(
            payload.get(
                "search_complete",
                payload.get("browser_complete", False),
            )
        ),
        search_stop_reason=str(
            payload.get(
                "search_stop_reason",
                payload.get("browser_stop_reason", ""),
            )
        ),
        failed_detail_ids=tuple(
            str(value)
            for value in cast(
                list[object],
                payload.get("failed_detail_ids", []),
            )
        ),
    )


def _safe_job_dict(job: NormalizedJob) -> dict[str, object]:
    payload = cast(dict[str, object], asdict(job))
    payload["raw_data"] = {}
    return payload


def _progress_to_dict(
    progress: NowcoderCollectionProgress,
) -> dict[str, object]:
    return {
        "references": [
            cast(dict[str, object], asdict(reference))
            for reference in progress.references
        ],
        "jobs": [_safe_job_dict(job) for job in progress.jobs],
        "pages_read": progress.pages_read,
        "discovered_count": progress.discovered_count,
        "search_complete": progress.search_complete,
        "search_stop_reason": progress.search_stop_reason,
        "failed_detail_ids": list(progress.failed_detail_ids),
    }


def _result_to_dict(result: CollectionResult) -> dict[str, object]:
    payload = cast(dict[str, object], asdict(result))
    payload["jobs"] = [_safe_job_dict(job) for job in result.jobs]
    return payload
