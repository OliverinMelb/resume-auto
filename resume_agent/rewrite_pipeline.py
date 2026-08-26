"""Fact-gated rewrite workflow with one small public interface."""

from __future__ import annotations

import hashlib
import sqlite3
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from resume_agent.codex_runner import CodexRunError
from resume_agent.repository import NotFoundError, new_id
from resume_agent.research import analyze_jobs, build_external_resume_prompt
from resume_agent.resume_renderer import RenderError
from resume_agent.rewrite_artifacts import RunArtifacts
from resume_agent.rewrite_fact_gate import InvalidModelResult, validate_rewrite_facts
from resume_agent.rewrite_prompt import build_codex_task, render_jd_evidence
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer, RewriteRequest


class ResumeRewritePipeline:
    def __init__(
        self, repository, runs_root, schema_path, project_root, codex_runner, renderer
    ):
        self.repository = repository
        self.runs_root = Path(runs_root)
        self.schema_path = Path(schema_path)
        self.project_root = Path(project_root)
        self.codex_runner = codex_runner
        self.renderer = renderer

    def run(self, request: RewriteRequest) -> dict:
        try:
            self.repository.get_candidate(request.candidate_id)
        except NotFoundError:
            raise ValueError("candidate is unavailable") from None
        run_id = new_id("rewrite_run")
        artifacts = RunArtifacts.create(self.runs_root, run_id)
        try:
            run = self.repository.create_rewrite_run_with_id(
                run_id, request.model_dump(), str(artifacts.directory)
            )
        except sqlite3.Error:
            self._remove_created_artifacts(artifacts)
            raise ValueError("unable to create rewrite run") from None
        return self._execute(run, artifacts)

    def resume(self, run_id: str, fact_answers: list[FactAnswer]) -> dict:
        run = self.repository.get_rewrite_run(run_id)
        if run["status"] != "NEEDS_FACTS":
            raise ValueError("only NEEDS_FACTS runs can resume")
        artifacts = self._open_artifacts(run)
        self._validate_submitted_answers(run, fact_answers)
        merged = _merge_fact_answers(run["fact_answers"], fact_answers)
        run = self.repository.transition_rewrite_run(
            run_id, {"NEEDS_FACTS"}, "RUNNING",
            fact_answers=merged, increment_attempt=True,
        )
        return self._execute_running(run, artifacts)

    def get(self, run_id: str) -> dict:
        run = self.repository.get_rewrite_run(run_id)
        self._open_artifacts(run)
        return self._decorate_run(run)

    def available(self) -> bool:
        return self.codex_runner.available() and self.renderer.available()

    def _execute(self, run: dict, artifacts: RunArtifacts) -> dict:
        try:
            artifacts = self._open_artifacts(run)
            materials = self._prepare(run, artifacts)
        except (NotFoundError, ValueError):
            self._fail_validation(run, artifacts)
            raise ValueError("rewrite input is invalid") from None
        run = self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True
        )
        return self._run_with_materials(run, artifacts, materials)

    def _execute_running(self, run: dict, artifacts: RunArtifacts) -> dict:
        try:
            artifacts = self._open_artifacts(run)
            materials = self._prepare(run, artifacts)
        except (NotFoundError, ValueError):
            self._fail_validation(run, artifacts)
            raise ValueError("rewrite input is invalid") from None
        return self._run_with_materials(run, artifacts, materials)

    def _prepare(self, run: dict, artifacts: RunArtifacts) -> dict[str, Any]:
        candidate = self.repository.get_candidate(run["candidate_id"])
        job_ids = _ordered_unique(run["job_ids"])
        resume_ids = _ordered_unique(run["source_resume_ids"])
        if not job_ids or not resume_ids:
            raise ValueError("rewrite run requires jobs and source resumes")
        jobs = self.repository.get_jobs(job_ids)
        resumes = [self.repository.get_source_resume(item) for item in resume_ids]
        if any(job.get("candidate_id") != candidate["id"] for job in jobs):
            raise ValueError("all jobs must belong to the candidate")
        if any(resume["candidate_id"] != candidate["id"] for resume in resumes):
            raise ValueError("all source resumes must belong to the candidate")
        target_role = jobs[0]["title"] if len(jobs) == 1 else _target_role(candidate)
        keyword_counts = analyze_jobs(jobs)
        report = self.repository.create_research_report(
            candidate["id"], target_role, job_ids, keyword_counts, [], ""
        )
        snapshot = {
            "run_id": run["id"],
            "candidate_id": candidate["id"],
            "job_ids": job_ids,
            "source_resume_ids": resume_ids,
            "jobs": [
                {"id": job["id"], "content_sha256": _job_hash(job)} for job in jobs
            ],
            "source_resumes": [
                {"id": resume["id"], "content_sha256": resume["content_sha256"]}
                for resume in resumes
            ],
            "research_report_id": report["id"],
        }
        artifacts.write_json("input-snapshot.json", snapshot)
        jd_evidence = render_jd_evidence(jobs, keyword_counts)
        artifacts.write_text("jd-evidence.md", jd_evidence)
        base_prompt = build_external_resume_prompt(candidate, resumes, report, jobs, "")
        task = build_codex_task(
            base_prompt=base_prompt,
            fact_answers=run["fact_answers"],
            allowed_resume_ids=resume_ids,
            allowed_job_ids=job_ids,
        )
        artifacts.write_text("codex-task.md", task)
        return {"jobs": jobs, "resumes": resumes, "task": task}

    def _run_with_materials(
        self, run: dict, artifacts: RunArtifacts, materials: dict[str, Any]
    ) -> dict:
        artifacts.append_audit(
            run_id=run["id"], status="RUNNING", event_code="RUN_STARTED"
        )
        temporary = artifacts.directory / ".codex-output.tmp"
        try:
            result = self.codex_runner.run(
                materials["task"], self.schema_path, temporary, self.project_root
            )
            result = CodexRewriteResult.model_validate(result)
            artifacts.write_json("result.json", result.model_dump())
            artifacts.append_audit(
                run_id=run["id"], status="RUNNING", event_code="MODEL_COMPLETED",
                content_hash=_hash_text(result.rewritten_resume_markdown),
            )
            gate = validate_rewrite_facts(
                result, materials["resumes"], materials["jobs"], run["fact_answers"]
            )
            if gate.blockers:
                artifacts.remove_if_exists("resume.md")
                artifacts.remove_if_exists("resume.pdf")
                questions = [item.model_dump() for item in gate.blockers]
                artifacts.append_audit(
                    run_id=run["id"], status="NEEDS_FACTS", event_code="FACTS_REQUIRED",
                    question_ids=[item["question_id"] for item in questions],
                )
                updated = self.repository.transition_rewrite_run(
                    run["id"], {"RUNNING"}, "NEEDS_FACTS", blocking_questions=questions
                )
                return self._decorate_run(updated)
            artifacts.write_text("resume.md", result.rewritten_resume_markdown)
            render_result = self.renderer.render(
                result.rewritten_resume_markdown, artifacts.path("resume.pdf"),
                artifacts.directory / ".preview",
            )
            artifacts.append_audit(
                run_id=run["id"], status="RUNNING", event_code="RENDER_COMPLETED",
                content_hash=_hash_text(result.rewritten_resume_markdown),
                page_count=render_result.page_count,
            )
            updated = self.repository.transition_rewrite_run(
                run["id"], {"RUNNING"}, "READY", blocking_questions=[]
            )
            artifacts.append_audit(
                run_id=run["id"], status="READY", event_code="RUN_READY",
                source_ids=[item["id"] for item in materials["resumes"] + materials["jobs"]],
            )
            return self._decorate_run(updated)
        except (CodexRunError, InvalidModelResult, RenderError, ValidationError) as error:
            self._fail(run, artifacts, error)
            return self.get(run["id"])
        finally:
            temporary.unlink(missing_ok=True)

    def _fail_validation(self, run: dict, artifacts: RunArtifacts) -> None:
        self._fail(
            run, artifacts, InvalidModelResult("invalid request state"),
            error_code="VALIDATION_ERROR",
        )

    def _fail(
        self, run: dict, artifacts: RunArtifacts, error: Exception,
        error_code: str | None = None,
    ) -> None:
        artifacts.remove_if_exists("resume.md")
        artifacts.remove_if_exists("resume.pdf")
        preview_dir = artifacts.directory / ".preview"
        if preview_dir.is_symlink():
            preview_dir.unlink(missing_ok=True)
        elif preview_dir.is_dir():
            resolved_preview = preview_dir.resolve()
            if resolved_preview.parent != artifacts.directory.resolve():
                raise ValueError("preview directory escapes run artifacts")
            shutil.rmtree(resolved_preview)
        code = error_code or _safe_error_code(error)
        artifacts.append_audit(
            run_id=run["id"], status="FAILED", event_code="RUN_FAILED", error_code=code,
            error_safe_hash=_hash_text(code),
        )
        self.repository.transition_rewrite_run(
            run["id"], {"QUEUED", "RUNNING"}, "FAILED", error_code=code
        )

    def _open_artifacts(self, run: dict) -> RunArtifacts:
        expected = self.runs_root.resolve() / run["id"]
        stored = Path(run["artifact_dir"]).resolve()
        if stored != expected:
            self._fail_artifact_location(run)
            raise ValueError("rewrite artifacts are invalid")
        return RunArtifacts.open(self.runs_root, run["id"])

    def _fail_artifact_location(self, run: dict) -> None:
        self.repository.transition_rewrite_run(
            run["id"], {run["status"]}, "FAILED", error_code="VALIDATION_ERROR"
        )

    def _validate_submitted_answers(
        self, run: dict, fact_answers: list[FactAnswer]
    ) -> None:
        question_ids = [answer.question_id for answer in fact_answers]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("duplicate fact answers are not allowed")
        blockers = {item["question_id"] for item in run["blocking_questions"]}
        if not set(question_ids) <= blockers:
            raise ValueError("fact answers must address current blockers")

    def _remove_created_artifacts(self, artifacts: RunArtifacts) -> None:
        expected = self.runs_root.resolve() / artifacts.directory.name
        if artifacts.directory.resolve() != expected:
            raise ValueError("created artifacts are invalid")
        shutil.rmtree(expected)

    @staticmethod
    def _decorate_run(run: dict) -> dict:
        return dict(run)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _target_role(candidate: dict) -> str:
    roles = candidate.get("target_roles") or []
    if not roles or not isinstance(roles[0], str) or not roles[0].strip():
        raise ValueError("multi-job rewrites require a candidate target role")
    return roles[0]


def _merge_fact_answers(existing: list[dict], answers: list[FactAnswer]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in [*existing, *answers]:
        answer = FactAnswer.model_validate(item)
        merged[answer.question_id] = answer.model_dump()
    return list(merged.values())


def _job_hash(job: dict) -> str:
    content = "\n".join(str(job.get(field, "")) for field in ("id", "title", "company", "salary", "detail"))
    return _hash_text(content)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, CodexRunError):
        return error.code if error.code in {"CODEX_UNAVAILABLE", "CODEX_TIMEOUT", "CODEX_PROCESS_ERROR", "INVALID_MODEL_RESULT"} else "CODEX_PROCESS_ERROR"
    if isinstance(error, RenderError):
        return "RENDER_ERROR" if error.code == "RENDER_ERROR" else "RENDER_VALIDATION_ERROR"
    return "INVALID_MODEL_RESULT"
