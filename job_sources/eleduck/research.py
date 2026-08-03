"""Lossy projection of accepted Eleduck jobs into the existing research database."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from job_sources.contracts import NormalizedJob
from resume_agent.repository import Repository


ELEDUCK_SOURCE_LABEL = "电鸭公开职位"
ELEDUCK_PROJECT_NOTE = "由电鸭公开职位列表的 JD 调研创建；未修改任何简历。"
_LEGACY_ELEDUCK_PROJECT_NOTE = "由电鸭公开 RSS 的 JD 调研创建；未修改任何简历。"
_RECOVERY_SCOPE_PREFIX = "恢复范围："
_RECOVERY_KEY_PREFIX = "恢复键："


@dataclass(frozen=True)
class PersistedEleduckJob:
    job_id: str
    created: bool


class EleduckResearchSink:
    def __init__(self, repository: Repository):
        self._repository = repository

    def resolve_project(
        self,
        *,
        candidate_id: str | None,
        project_name: str | None,
        default_name: str,
        jobs: tuple[NormalizedJob, ...],
    ) -> str:
        if candidate_id:
            self._repository.get_candidate(candidate_id)
            return candidate_id
        normalized_project_name = (project_name or "").strip()
        scope = "named" if normalized_project_name else "default"
        name = normalized_project_name or default_name
        recovery_note = _project_recovery_note(scope, name, jobs)
        candidates = self._repository.list_candidates()
        scoped_candidates = [
            candidate
            for candidate in candidates
            if _has_recovery_scope(candidate, scope)
            and (scope == "default" or candidate.get("display_name") == name)
        ]
        for candidate in scoped_candidates:
            if candidate.get("notes") == recovery_note:
                return str(candidate["id"])
        fingerprints = tuple(eleduck_job_fingerprint(job) for job in jobs)
        for candidate in scoped_candidates:
            resolved_id = str(candidate["id"])
            if any(
                self._repository.find_job_by_fingerprint(
                    resolved_id,
                    fingerprint,
                )
                for fingerprint in fingerprints
            ):
                return resolved_id
        for candidate in scoped_candidates:
            resolved_id = str(candidate["id"])
            if not self._repository.list_jobs(resolved_id):
                return resolved_id
        candidate = self._repository.create_candidate(
            display_name=name,
            target_roles=[ELEDUCK_SOURCE_LABEL],
            notes=recovery_note,
        )
        return str(candidate["id"])

    def persist(
        self,
        candidate_id: str,
        jobs: tuple[NormalizedJob, ...],
    ) -> tuple[PersistedEleduckJob, ...]:
        persisted: list[PersistedEleduckJob] = []
        for job in jobs:
            detail = eleduck_research_detail(job)
            if not detail:
                continue
            fingerprint = eleduck_job_fingerprint(job)
            stored, created = self._repository.create_job_if_absent(
                {
                    "candidate_id": candidate_id,
                    "external_job_id": job.source_job_id,
                    "source": "eleduck",
                    "title": job.title,
                    "salary": "",
                    "detail": detail,
                    "company": "",
                    "score": job.score,
                    "qualified": job.qualified,
                    "search_keyword": ELEDUCK_SOURCE_LABEL,
                    "job_fingerprint": fingerprint,
                    "source_logged_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
                fingerprint,
            )
            persisted.append(
                PersistedEleduckJob(job_id=str(stored["id"]), created=created)
            )
        return tuple(persisted)


def eleduck_research_detail(job: NormalizedJob) -> str:
    return job.description or "\n\n".join(
        (*job.responsibilities, *job.requirements)
    )


def eleduck_job_fingerprint(job: NormalizedJob) -> str:
    return hashlib.sha256(
        f"eleduck:{job.source_job_id}".encode("utf-8")
    ).hexdigest()


def _project_recovery_note(
    scope: str,
    project_name: str,
    jobs: tuple[NormalizedJob, ...],
) -> str:
    fingerprints = sorted(eleduck_job_fingerprint(job) for job in jobs)
    material = "\n".join((scope, project_name if scope == "named" else "", *fingerprints))
    recovery_key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return (
        f"{ELEDUCK_PROJECT_NOTE}\n"
        f"{_RECOVERY_SCOPE_PREFIX}{scope}\n"
        f"{_RECOVERY_KEY_PREFIX}{recovery_key}"
    )


def _has_recovery_scope(candidate: dict[str, object], scope: str) -> bool:
    notes = candidate.get("notes")
    return isinstance(notes, str) and any(
        notes.startswith(
            f"{project_note}\n{_RECOVERY_SCOPE_PREFIX}{scope}\n"
            f"{_RECOVERY_KEY_PREFIX}"
        )
        for project_note in (ELEDUCK_PROJECT_NOTE, _LEGACY_ELEDUCK_PROJECT_NOTE)
    )
