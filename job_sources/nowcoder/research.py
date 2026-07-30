"""Persistence adapter kept outside Nowcoder fetching and parsing logic."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from job_sources.contracts import JobQuery, NormalizedJob
from resume_agent.repository import Repository


class NowcoderResearchSink:
    def __init__(self, repository: Repository):
        self._repository = repository

    def resolve_project(
        self,
        query: JobQuery,
        *,
        candidate_id: str | None,
        project_name: str | None,
    ) -> str:
        if candidate_id:
            self._repository.get_candidate(candidate_id)
            return candidate_id
        name = project_name or self._default_project_name(query)
        candidate = self._repository.create_candidate(
            display_name=name,
            target_roles=[query.keyword],
            notes="由牛客 JD 调研任务创建；未修改任何简历。",
        )
        return str(candidate["id"])

    def persist(
        self,
        candidate_id: str,
        query: JobQuery,
        jobs: tuple[NormalizedJob, ...],
    ) -> tuple[str, ...]:
        persisted_ids: list[str] = []
        for job in jobs:
            detail = self._research_detail(job)
            if not detail:
                continue
            fingerprint = hashlib.sha256(
                f"{job.source}:{job.source_job_id}".encode("utf-8")
            ).hexdigest()
            salary = (
                ""
                if job.employment_type == "intern"
                else (job.salary_text or "")
            )
            stored, _ = self._repository.create_job_if_absent(
                {
                    "candidate_id": candidate_id,
                    "external_job_id": job.source_job_id,
                    "source": job.source,
                    "title": job.title,
                    "salary": salary,
                    "detail": detail,
                    "company": job.company or "",
                    "score": job.score,
                    "qualified": job.qualified,
                    "search_keyword": query.keyword,
                    "job_fingerprint": fingerprint,
                    "source_logged_at": datetime.now(
                        timezone.utc
                    ).isoformat(timespec="seconds"),
                },
                fingerprint,
            )
            persisted_ids.append(str(stored["id"]))
        return tuple(persisted_ids)

    @staticmethod
    def _research_detail(job: NormalizedJob) -> str:
        parts: list[str] = []
        if job.employment_type == "intern" and job.salary_text:
            parts.append(
                f"实习薪资（保留原文，不纳入市场薪资统计）：{job.salary_text}"
            )
        if job.description:
            parts.append(job.description)
        elif job.responsibilities or job.requirements:
            parts.extend((*job.responsibilities, *job.requirements))
        return "\n\n".join(parts)

    @staticmethod
    def _default_project_name(query: JobQuery) -> str:
        scope = " / ".join(
            value
            for value in (
                query.keyword,
                query.city,
                query.recruitment_channel,
            )
            if value
        )
        return f"牛客 JD 调研：{scope}"
