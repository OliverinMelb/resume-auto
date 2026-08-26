"""Resumable, source-degrading orchestration for JD discovery."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from resume_agent.discovery_artifacts import DiscoveryArtifacts
from resume_agent.discovery_schemas import (
    DiscoveryRequest,
    DiscoveredJob,
    deduplicate_discovered_jobs,
)
from resume_agent.discovery_sources import (
    DiscoverySourceAdapter,
    SourceCheckpoint,
    SourceInteractionRequiredError,
    SourcePollResult,
)
from resume_agent.repository import Repository, new_id


_SOURCE_STATUSES = {"collecting", "completed", "interaction_required", "failed"}
_SAFE_SOURCE_ERROR_CODES = {
    "SOURCE_UNAVAILABLE",
    "SOURCE_TASK_FAILED",
    "SOURCE_INTERACTION_REQUIRED",
    "INVALID_SOURCE_RESPONSE",
    "INVALID_SOURCE_CHECKPOINT",
}
_POLLABLE_RUN_STATUSES = {"COLLECTING", "READY_FOR_IMPORT", "PARTIAL"}
_MINIMUM_JOB_COUNT = 3


class JobDiscoveryOrchestrator:
    def __init__(
        self,
        repository: Repository,
        runs_root: Path,
        adapters: dict[str, DiscoverySourceAdapter],
        *,
        lease_duration_seconds: int = 300,
    ):
        if type(lease_duration_seconds) is not int or lease_duration_seconds < 1:
            raise ValueError("invalid discovery poll lease duration")
        self.repository = repository
        self.runs_root = Path(runs_root)
        self.adapters = dict(adapters)
        self._lease_duration_seconds = lease_duration_seconds

    def start(self, request: DiscoveryRequest) -> dict:
        if not isinstance(request, DiscoveryRequest):
            raise ValueError("invalid discovery request")
        self.repository.get_candidate(request.candidate_id)
        self._validate_requested_adapters(request.sources)

        run_id = new_id("discovery_run")
        artifacts = DiscoveryArtifacts.create(self.runs_root, run_id)
        try:
            run = self.repository.create_discovery_run(
                run_id,
                candidate_id=request.candidate_id,
                request=request.model_dump(mode="json"),
                artifact_dir=str(artifacts.directory),
            )
        except Exception:
            self._remove_unowned_artifacts(artifacts)
            raise

        try:
            artifacts.write_json(
                "input-snapshot.json", self._redacted_snapshot(run_id, request)
            )
            artifacts.append_audit(
                run_id=run_id, status="CREATED", event_code="RUN_CREATED"
            )
        except Exception:
            return self._fail_created_run(run_id, artifacts)

        checkpoints: dict[str, dict] = {}
        statuses: dict[str, str] = {}
        source_audit_events: list[dict[str, Any]] = []
        for source in request.sources:
            adapter = self.adapters[source]
            try:
                checkpoint = adapter.start(request)
                self._validate_checkpoint(checkpoint, source)
            except Exception as error:
                statuses[source] = "failed"
                source_audit_events.append(
                    {
                        "event_code": "SOURCE_FAILED",
                        "source": source,
                        "error_code": self._safe_start_error_code(error),
                    }
                )
                continue
            checkpoints[source] = checkpoint.to_dict()
            statuses[source] = "collecting"

        new_status = (
            "COLLECTING" if any(value == "collecting" for value in statuses.values()) else "FAILED"
        )
        run = self._persist_started_sources(
            run_id, new_status, checkpoints, statuses
        )
        for event in source_audit_events:
            self._append_audit_best_effort(
                artifacts, run_id=run_id, status=new_status, **event
            )
        self._append_audit_best_effort(
            artifacts,
            run_id=run_id,
            status=new_status,
            event_code=(
                "COLLECTION_STARTED" if new_status == "COLLECTING" else "RUN_FAILED"
            ),
            error_code="DISCOVERY_FAILED" if new_status == "FAILED" else None,
        )
        return run

    def poll(self, run_id: str) -> dict:
        owner_token = secrets.token_hex(32)
        now = datetime.now(timezone.utc)
        claimed = self.repository.claim_discovery_poll(
            run_id,
            owner_token,
            self._lease_expiration(now),
            now.isoformat(timespec="seconds"),
        )
        if not claimed:
            run = self.repository.get_discovery_run(run_id)
            if run["status"] in _POLLABLE_RUN_STATUSES:
                raise ValueError("discovery poll is busy")
            raise ValueError("discovery run is not pollable")
        try:
            return self._poll_owned(run_id, owner_token)
        finally:
            try:
                self.repository.release_discovery_poll(run_id, owner_token)
            except Exception:
                pass

    def _poll_owned(self, run_id: str, owner_token: str) -> dict:
        run = self.repository.get_discovery_run(run_id)
        artifacts = self._artifacts_for(run)
        if run["status"] not in _POLLABLE_RUN_STATUSES:
            raise ValueError("discovery run is not pollable")

        jobs = self._read_valid_results(artifacts)
        if run["status"] in {"READY_FOR_IMPORT", "PARTIAL"}:
            return run
        checkpoints = dict(run["source_checkpoints"])
        statuses = dict(run["source_statuses"])
        requested_sources = self._requested_sources(run)
        source_audit_events: list[dict[str, Any]] = []

        for source in requested_sources:
            previous_status = statuses.get(source)
            if previous_status in {"completed", "failed"}:
                continue
            raw_checkpoint = checkpoints.get(source)
            if raw_checkpoint is None:
                statuses[source] = "failed"
                source_audit_events.append(
                    self._source_failure_event(source, "INVALID_SOURCE_CHECKPOINT")
                )
                continue
            try:
                checkpoint = SourceCheckpoint.from_dict(raw_checkpoint)
                self._validate_checkpoint(checkpoint, source)
            except (TypeError, ValueError):
                statuses[source] = "failed"
                source_audit_events.append(
                    self._source_failure_event(source, "INVALID_SOURCE_CHECKPOINT")
                )
                continue

            adapter = self.adapters.get(source)
            if adapter is None:
                statuses[source] = "failed"
                source_audit_events.append(
                    self._source_failure_event(source, "SOURCE_UNAVAILABLE")
                )
                continue

            try:
                result = adapter.poll(checkpoint)
                self._validate_poll_result(result, source)
            except SourceInteractionRequiredError:
                statuses[source] = "interaction_required"
                source_audit_events.append(
                    {
                        "event_code": "SOURCE_INTERACTION_REQUIRED",
                        "source": source,
                        "error_code": "SOURCE_INTERACTION_REQUIRED",
                    }
                )
                continue
            except Exception as error:
                statuses[source] = "failed"
                source_audit_events.append(
                    self._source_failure_event(
                        source, self._safe_poll_error_code(error)
                    )
                )
                continue

            try:
                self._validate_checkpoint(result.checkpoint, source)
            except (TypeError, ValueError):
                statuses[source] = "failed"
                source_audit_events.append(
                    self._source_failure_event(source, "INVALID_SOURCE_CHECKPOINT")
                )
                continue

            checkpoints[source] = result.checkpoint.to_dict()
            statuses[source] = result.status
            jobs.extend(result.jobs)
            source_audit_events.append(
                {
                    "event_code": "SOURCE_POLLED",
                    "source": source,
                    "count": len(result.jobs),
                }
            )
            result_event = self._source_result_audit_event(result)
            if result_event is not None:
                source_audit_events.append(result_event)

        unique_jobs = deduplicate_discovered_jobs(jobs)
        if not self.repository.renew_discovery_poll(
            run_id,
            owner_token,
            self._lease_expiration(datetime.now(timezone.utc)),
        ):
            raise ValueError("discovery poll lease was lost")
        new_status = _resolve_discovery_status(statuses, unique_jobs)
        error_code = self._run_error_code(new_status, statuses)
        run = self.repository.publish_discovery_transition(
            run_id,
            run["status"],
            new_status,
            expected_poll_owner=owner_token,
            source_checkpoints=checkpoints,
            source_statuses=statuses,
            error_code=error_code,
            publish_results=lambda: artifacts.write_results(unique_jobs),
        )
        for event in source_audit_events:
            self._append_audit_best_effort(
                artifacts, run_id=run_id, status=new_status, **event
            )
        self._append_audit_best_effort(
            artifacts,
            run_id=run_id,
            status=new_status,
            event_code="RESULTS_WRITTEN",
            count=len(unique_jobs),
        )
        self._append_audit_best_effort(
            artifacts,
            run_id=run_id,
            status=new_status,
            event_code=self._run_event_code(new_status),
            count=len(unique_jobs),
            error_code=error_code,
        )
        return run

    def get(self, run_id: str) -> dict:
        run = self.repository.get_discovery_run(run_id)
        self._artifacts_for(run)
        return run

    def _validate_requested_adapters(self, sources: list[str]) -> None:
        for source in sources:
            adapter = self.adapters.get(source)
            if (
                adapter is None
                or getattr(adapter, "source", None) != source
                or not callable(getattr(adapter, "start", None))
                or not callable(getattr(adapter, "poll", None))
            ):
                raise ValueError("discovery source adapter unavailable")

    def _artifacts_for(self, run: dict[str, Any]) -> DiscoveryArtifacts:
        artifacts = DiscoveryArtifacts.open(self.runs_root, run["id"])
        artifact_dir = run.get("artifact_dir")
        try:
            configured = Path(artifact_dir)
            consistent = (
                isinstance(artifact_dir, str)
                and configured.is_absolute()
                and configured.resolve(strict=True) == artifacts.directory
            )
        except (OSError, TypeError, ValueError):
            consistent = False
        if not consistent:
            raise ValueError("invalid discovery artifact consistency")
        return artifacts

    @staticmethod
    def _redacted_snapshot(run_id: str, request: DiscoveryRequest) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "candidate_id": request.candidate_id,
            "sources": list(request.sources),
            "target_family": request.target_family,
            "recruitment_channel": request.recruitment_channel,
            "limit_per_source": request.limit_per_source,
            "keyword_count": len(request.keywords),
            "city_configured": request.city is not None,
            "boss_project_configured": request.boss_project_id is not None,
            "nowcoder_acquisition_mode": request.nowcoder_acquisition_mode,
        }

    @staticmethod
    def _validate_checkpoint(checkpoint: object, source: str) -> None:
        if not isinstance(checkpoint, SourceCheckpoint) or checkpoint.source != source:
            raise ValueError("invalid source checkpoint")
        try:
            encoded = json.dumps(
                checkpoint.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            encoded.encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("invalid source checkpoint") from None

    @staticmethod
    def _validate_poll_result(result: object, source: str) -> None:
        if (
            not isinstance(result, SourcePollResult)
            or result.source != source
            or result.status not in _SOURCE_STATUSES
            or not isinstance(result.checkpoint, SourceCheckpoint)
            or result.checkpoint.source != source
            or not isinstance(result.jobs, tuple)
            or any(not isinstance(item, DiscoveredJob) or item.source != source for item in result.jobs)
            or result.error_code not in _SAFE_SOURCE_ERROR_CODES | {None}
        ):
            raise ValueError("invalid source response")

    @staticmethod
    def _safe_start_error_code(error: Exception) -> str:
        if isinstance(error, (TypeError, ValueError)):
            return "INVALID_SOURCE_CHECKPOINT"
        return "SOURCE_UNAVAILABLE"

    @staticmethod
    def _safe_poll_error_code(error: Exception) -> str:
        if isinstance(error, (TypeError, ValueError)):
            return "INVALID_SOURCE_RESPONSE"
        return "SOURCE_UNAVAILABLE"

    @staticmethod
    def _source_failure_event(source: str, error_code: str) -> dict[str, Any]:
        return {
            "event_code": "SOURCE_FAILED",
            "source": source,
            "error_code": error_code,
        }

    @staticmethod
    def _source_result_audit_event(
        result: SourcePollResult,
    ) -> dict[str, Any] | None:
        if result.status == "completed":
            event_code = "SOURCE_COMPLETED"
            error_code = None
        elif result.status == "interaction_required":
            event_code = "SOURCE_INTERACTION_REQUIRED"
            error_code = result.error_code or "SOURCE_INTERACTION_REQUIRED"
        elif result.status == "failed":
            event_code = "SOURCE_FAILED"
            error_code = result.error_code or "SOURCE_TASK_FAILED"
        else:
            return None
        event: dict[str, Any] = {
            "event_code": event_code,
            "source": result.source,
            "count": len(result.jobs),
        }
        if error_code is not None:
            event["error_code"] = error_code
        return event

    @staticmethod
    def _requested_sources(run: dict[str, Any]) -> list[str]:
        request = run.get("request")
        sources = request.get("sources") if isinstance(request, dict) else None
        if (
            not isinstance(sources, list)
            or not sources
            or any(source not in {"boss", "nowcoder"} for source in sources)
            or len(sources) != len(set(sources))
        ):
            raise ValueError("invalid saved discovery request")
        return list(sources)

    @staticmethod
    def _read_valid_results(artifacts: DiscoveryArtifacts) -> list[DiscoveredJob]:
        path = artifacts.path("source-results.json")
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise TypeError
            return [DiscoveredJob.model_validate(item) for item in payload]
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("invalid discovery results artifact") from None

    def _persist_started_sources(
        self,
        run_id: str,
        new_status: str,
        checkpoints: dict[str, dict],
        statuses: dict[str, str],
    ) -> dict:
        arguments = {
            "source_checkpoints": checkpoints,
            "source_statuses": statuses,
            "error_code": "DISCOVERY_FAILED" if new_status == "FAILED" else None,
        }
        try:
            return self.repository.transition_discovery_run(
                run_id, {"CREATED"}, new_status, **arguments
            )
        except Exception:
            try:
                persisted = self.repository.get_discovery_run(run_id)
                if persisted["status"] != "CREATED":
                    return persisted
                return self.repository.transition_discovery_run(
                    run_id, {"CREATED"}, new_status, **arguments
                )
            except Exception:
                raise ValueError("discovery sources could not be persisted") from None

    def _lease_expiration(self, now: datetime) -> str:
        return (now + timedelta(seconds=self._lease_duration_seconds)).isoformat(
            timespec="seconds"
        )

    @staticmethod
    def _append_audit_best_effort(
        artifacts: DiscoveryArtifacts, **event: Any
    ) -> None:
        try:
            artifacts.append_audit(**event)
        except Exception:
            pass

    @staticmethod
    def _run_error_code(status: str, source_statuses: dict[str, str]) -> str | None:
        if status == "PARTIAL":
            return "DISCOVERY_FAILED"
        if status == "FAILED":
            return (
                "DISCOVERY_FAILED"
                if "failed" in source_statuses.values()
                else "INSUFFICIENT_JOBS"
            )
        return None

    @staticmethod
    def _run_event_code(status: str) -> str:
        return {
            "COLLECTING": "COLLECTION_STARTED",
            "READY_FOR_IMPORT": "RUN_READY",
            "PARTIAL": "RUN_PARTIAL",
            "FAILED": "RUN_FAILED",
        }[status]

    def _fail_created_run(
        self, run_id: str, artifacts: DiscoveryArtifacts
    ) -> dict[str, Any]:
        failed = self.repository.transition_discovery_run(
            run_id, {"CREATED"}, "FAILED", error_code="DISCOVERY_FAILED"
        )
        try:
            artifacts.append_audit(
                run_id=run_id,
                status="FAILED",
                event_code="RUN_FAILED",
                error_code="DISCOVERY_FAILED",
            )
        except Exception:
            pass
        return failed

    @staticmethod
    def _remove_unowned_artifacts(artifacts: DiscoveryArtifacts) -> None:
        try:
            audit = artifacts.path("audit.md")
            audit.unlink(missing_ok=True)
            artifacts.directory.rmdir()
        except OSError:
            pass


def _resolve_discovery_status(
    source_statuses: dict[str, str], jobs: list[DiscoveredJob]
) -> str:
    statuses = list(source_statuses.values())
    if any(status not in _SOURCE_STATUSES for status in statuses):
        raise ValueError("invalid discovery source status")
    if any(status in {"collecting", "interaction_required"} for status in statuses):
        return "COLLECTING"
    if len(jobs) < _MINIMUM_JOB_COUNT:
        return "FAILED"
    if "failed" in statuses:
        return "PARTIAL"
    if statuses and all(status == "completed" for status in statuses):
        return "READY_FOR_IMPORT"
    return "FAILED"
