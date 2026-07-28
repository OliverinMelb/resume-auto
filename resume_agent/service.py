import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .importer import import_job_decisions
from .market import build_market_report
from .repository import Repository, utc_now
from .research import (
    analyze_jobs,
    build_candidate_profile_prompt,
    build_external_resume_prompt,
    build_resume_evidence_index,
)
from .stats import build_stats, read_legacy_actions


def _normalize_date_bound(value: str | None, is_upper: bool) -> str | None:
    if value and len(value) == 10:
        return f"{value}T{'23:59:59' if is_upper else '00:00:00'}"
    return value


class ResumeAgentService:
    def __init__(
        self,
        repository: Repository,
        legacy_action_log_path: Path,
        legacy_decision_log_path: Path | None = None,
    ):
        self.repository = repository
        self.legacy_action_log_path = legacy_action_log_path
        self.legacy_decision_log_path = legacy_decision_log_path

    @staticmethod
    def _research_job_fingerprint(payload: dict[str, Any]) -> str:
        external_job_id = str(payload.get("external_job_id") or "").strip()
        if external_job_id:
            parsed = urlsplit(external_job_id)
            normalized_url = urlunsplit(
                (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", "")
            )
            identity = f"url:{normalized_url or external_job_id}"
        else:
            fields = [
                payload.get("title"),
                payload.get("company"),
                payload.get("salary"),
                payload.get("detail"),
            ]
            normalized_fields = [
                re.sub(r"\s+", " ", str(value or "")).strip().lower()
                for value in fields
            ]
            identity = "content:" + "\n".join(normalized_fields)
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def collect_research_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        fingerprint = self._research_job_fingerprint(payload)
        job, created = self.repository.create_job_if_absent(
            {
                **payload,
                "source": "boss_research",
                "score": None,
                "qualified": True,
                "job_fingerprint": fingerprint,
                "source_logged_at": utc_now(),
            },
            fingerprint,
        )
        return {
            "status": "created" if created else "duplicate",
            "created": created,
            "job": job,
        }

    def _resolve_resumes(
        self,
        candidate_id: str,
        resume_ids: list[str],
    ) -> list[dict[str, Any]]:
        unique_ids = list(dict.fromkeys(resume_ids))
        resumes = (
            [self.repository.get_source_resume(resume_id) for resume_id in unique_ids]
            if unique_ids
            else self.repository.list_source_resumes(candidate_id)
        )
        if not resumes:
            raise ValueError("候选人还没有原始简历")
        for resume in resumes:
            if resume["candidate_id"] != candidate_id:
                raise ValueError(f"原始简历不属于该候选人: {resume['id']}")
        return resumes

    def create_research(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate = self.repository.get_candidate(payload["candidate_id"])
        job_ids = list(dict.fromkeys(payload["job_ids"]))
        jobs = self.repository.get_jobs(job_ids)
        for job in jobs:
            if job["candidate_id"] not in (None, candidate["id"]):
                raise ValueError(f"岗位不属于该候选人: {job['id']}")
        selected_job_ids = set(job_ids)
        for finding in payload.get("manual_findings", []):
            unknown_sources = set(finding.get("source_job_ids", [])) - selected_job_ids
            if unknown_sources:
                raise ValueError(
                    "调研结论引用了未选择的岗位: "
                    + ", ".join(sorted(unknown_sources))
                )
        keyword_counts = analyze_jobs(jobs)
        return self.repository.create_research_report(
            candidate_id=candidate["id"],
            target_role=payload["target_role"],
            job_ids=job_ids,
            keyword_counts=keyword_counts,
            manual_findings=payload.get("manual_findings", []),
            notes=payload.get("notes", ""),
        )

    def build_prompt(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate = self.repository.get_candidate(payload["candidate_id"])
        resume_ids = list(payload.get("source_resume_ids", []))
        if payload.get("source_resume_id"):
            resume_ids.insert(0, payload["source_resume_id"])
        resumes = self._resolve_resumes(candidate["id"], resume_ids)
        report = self.repository.get_research_report(payload["research_report_id"])
        if report["candidate_id"] != candidate["id"]:
            raise ValueError("调研报告不属于该候选人")
        jobs = self.repository.get_jobs(report["job_ids"])
        prompt = build_external_resume_prompt(
            candidate=candidate,
            source_resumes=resumes,
            report=report,
            jobs=jobs,
            extra_instructions=payload.get("extra_instructions", ""),
        )
        return {
            "status": "prompt_ready",
            "candidate_id": candidate["id"],
            "source_resume_ids": [resume["id"] for resume in resumes],
            "research_report_id": report["id"],
            "prompt": prompt,
            "model_called": False,
            "resume_modified": False,
        }

    def build_candidate_profile_prompt(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        candidate = self.repository.get_candidate(payload["candidate_id"])
        resumes = self._resolve_resumes(
            candidate["id"], payload.get("source_resume_ids", [])
        )
        evidence_index = build_resume_evidence_index(resumes)
        prompt = build_candidate_profile_prompt(
            candidate=candidate,
            source_resumes=resumes,
            evidence_index=evidence_index,
            extra_instructions=payload.get("extra_instructions", ""),
        )
        return {
            "status": "prompt_ready",
            "candidate_id": candidate["id"],
            "source_resume_ids": [resume["id"] for resume in resumes],
            "evidence_index": evidence_index,
            "prompt": prompt,
            "model_called": False,
            "resume_modified": False,
        }

    def import_decisions(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.legacy_decision_log_path is None:
            raise ValueError("未配置岗位判断日志路径")
        return import_job_decisions(
            repository=self.repository,
            decision_log_path=self.legacy_decision_log_path,
            candidate_id=payload["candidate_id"],
            qualified_only=payload.get("qualified_only", True),
            threshold=payload.get("threshold", 50),
            date_from=payload.get("date_from"),
            date_to=payload.get("date_to"),
            search_keywords=payload.get("search_keywords", []),
        )

    def market_report(
        self,
        candidate_id: str,
        qualified_only: bool,
        search_keyword: str | None,
    ) -> dict[str, Any]:
        jobs = self.repository.list_jobs(
            candidate_id=candidate_id,
            qualified_only=qualified_only,
            search_keyword=search_keyword,
        )
        result = build_market_report(jobs)
        result["filters"] = {
            "candidate_id": candidate_id,
            "qualified_only": qualified_only,
            "search_keyword": search_keyword,
        }
        return result

    def delivery_stats(
        self,
        candidate_id: str | None,
        date_from: str | None,
        date_to: str | None,
        include_legacy: bool,
    ) -> dict[str, Any]:
        normalized_from = _normalize_date_bound(date_from, is_upper=False)
        normalized_to = _normalize_date_bound(date_to, is_upper=True)
        manual_events = self.repository.list_delivery_events(
            candidate_id=candidate_id,
            date_from=normalized_from,
            date_to=normalized_to,
        )
        legacy_events, malformed = ([], 0)
        if include_legacy and candidate_id is None:
            legacy_events, malformed = read_legacy_actions(
                self.legacy_action_log_path,
                date_from=normalized_from,
                date_to=normalized_to,
            )
        result = build_stats(manual_events, legacy_events, malformed)
        result["filters"] = {
            "candidate_id": candidate_id,
            "date_from": date_from,
            "date_to": date_to,
            "include_legacy": include_legacy,
        }
        return result
