"""Fact-gated rewrite workflow with one small public interface."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import shutil
import tempfile
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from resume_agent.codex_runner import CodexRunError
from resume_agent.job_family import known_job_family
from resume_agent.repository import NotFoundError, new_id
from resume_agent.research import analyze_jobs, build_external_resume_prompt
from resume_agent.resume_renderer import RenderError
from resume_agent.rewrite_artifacts import RunArtifacts
from resume_agent.rewrite_fact_gate import InvalidModelResult, validate_rewrite_facts
from resume_agent.rewrite_prompt import build_codex_task, render_jd_evidence
from resume_agent.rewrite_schemas import CodexRewriteResult, FactAnswer, RewriteRequest

PIPELINE_SCHEMA_EXECUTION_VERSION = "resume-rewrite-pipeline/v1"
MULTI_JOB_TITLE_FAMILY_BLOCKER_ID = (
    "gap-" + hashlib.sha256(b"multi-job-title-family").hexdigest()[:12]
)


class ResumeRewritePipeline:
    def __init__(
        self, repository, runs_root, schema_path, project_root, codex_runner, renderer,
        *, lease_duration_seconds: int = 1200,
    ):
        self.repository = repository
        self.runs_root = Path(runs_root)
        self.schema_path = Path(schema_path)
        self.project_root = Path(project_root)
        self.codex_runner = codex_runner
        self.renderer = renderer
        self._owner_token = secrets.token_hex(32)
        self._lease_duration_seconds = lease_duration_seconds

    def recover_interrupted_runs(self) -> int:
        now = datetime.now(timezone.utc)
        stale = self.repository.list_recoverable_rewrite_runs(
            now=now.isoformat(timespec="seconds"),
            legacy_stale_before=(
                now - timedelta(seconds=self._lease_duration_seconds)
            ).isoformat(timespec="seconds"),
        )
        recovered = 0
        for run in stale:
            try:
                artifacts = self._open_recovery_artifacts(run)
            except ValueError:
                continue
            claimed = self.repository.claim_interrupted_rewrite_run(
                run["id"],
                expected_owner_token=run["owner_token"],
                expected_lease_expires_at=run["lease_expires_at"],
                recovery_owner_token=self._owner_token,
                recovery_lease_expires_at=self._new_lease_expiration(),
            )
            if not claimed:
                continue
            try:
                self._remove_deployable_artifacts(artifacts)
                artifacts.append_audit(
                    run_id=run["id"], status="FAILED", event_code="RUN_RECOVERED",
                    error_code="INTERRUPTED",
                )
            except (OSError, ValueError):
                self.repository.transition_rewrite_run(
                    run["id"], {"RUNNING"}, "RUNNING",
                    owner_token=self._owner_token,
                    lease_expires_at=run["lease_expires_at"],
                    expected_owner_token=self._owner_token,
                )
                continue
            self.repository.transition_rewrite_run(
                run["id"], {"RUNNING"}, "FAILED", error_code="INTERRUPTED",
                expected_owner_token=self._owner_token,
            )
            recovered += 1
        return recovered

    def run(self, request: RewriteRequest) -> dict:
        try:
            self.repository.get_candidate(request.candidate_id)
        except NotFoundError:
            raise ValueError("candidate is unavailable") from None
        run_id = new_id("rewrite_run")
        artifacts = RunArtifacts.create(self.runs_root, run_id)
        try:
            run = self.repository.create_rewrite_run_with_id(
                run_id, request.model_dump(mode="json"), str(artifacts.directory)
            )
        except sqlite3.Error:
            self._remove_created_artifacts(artifacts)
            raise ValueError("unable to create rewrite run") from None
        artifacts.append_audit(
            run_id=run["id"], status="QUEUED", event_code="RUN_CREATED"
        )
        return self._execute(run, artifacts)

    def resume(self, run_id: str, fact_answers: list[FactAnswer]) -> dict:
        run = self.repository.get_rewrite_run(run_id)
        if run["status"] != "NEEDS_FACTS":
            raise ValueError("only NEEDS_FACTS runs can resume")
        if _has_title_family_blocker(run["blocking_questions"]):
            return self._decorate_run(run)
        artifacts = self._open_artifacts(run)
        self._validate_submitted_answers(run, fact_answers)
        merged = _merge_fact_answers(run["fact_answers"], fact_answers)
        run = self.repository.transition_rewrite_run(
            run_id, {"NEEDS_FACTS"}, "RUNNING",
            fact_answers=merged, increment_attempt=True,
            owner_token=self._owner_token,
            lease_expires_at=self._new_lease_expiration(),
        )
        artifacts.append_audit(
            run_id=run["id"], status="RUNNING", event_code="FACTS_ACCEPTED",
            question_ids=[answer.question_id for answer in fact_answers],
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
        if materials["preflight_blockers"]:
            return self._block_preflight(run, artifacts, materials)
        run = self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "RUNNING", increment_attempt=True,
            owner_token=self._owner_token,
            lease_expires_at=self._new_lease_expiration(),
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
        preflight_blockers = _title_family_blockers(jobs)
        target_role = jobs[0]["title"] if len(jobs) == 1 else _target_role(candidate)
        keyword_counts = analyze_jobs(jobs)
        report = self.repository.create_research_report(
            candidate["id"], target_role, job_ids, keyword_counts, [], ""
        )
        snapshot = {
            "execution_version": PIPELINE_SCHEMA_EXECUTION_VERSION,
            "schema_sha256": hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
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
            "preflight_blocker_ids": [
                blocker["question_id"] for blocker in preflight_blockers
            ],
            "fact_answers": [
                FactAnswer.model_validate(answer).model_dump(mode="json")
                for answer in run["fact_answers"]
            ],
            "attempt_context": {
                "attempt_count": (
                    run["attempt_count"] + 1
                    if run["status"] == "QUEUED" and not preflight_blockers
                    else run["attempt_count"]
                ),
                "resumed": run["status"] == "RUNNING",
            },
        }
        artifacts.write_json("input-snapshot.json", snapshot)
        jd_evidence = render_jd_evidence(jobs, keyword_counts)
        artifacts.write_text("jd-evidence.md", jd_evidence)
        if preflight_blockers:
            return {
                "jobs": jobs,
                "resumes": resumes,
                "task": None,
                "preflight_blockers": preflight_blockers,
            }
        base_prompt = build_external_resume_prompt(candidate, resumes, report, jobs, "")
        task = build_codex_task(
            base_prompt=base_prompt,
            fact_answers=run["fact_answers"],
            allowed_resume_ids=resume_ids,
            allowed_job_ids=job_ids,
        )
        artifacts.write_text("codex-task.md", task)
        return {
            "jobs": jobs,
            "resumes": resumes,
            "task": task,
            "preflight_blockers": [],
        }

    def _block_preflight(
        self, run: dict, artifacts: RunArtifacts, materials: dict[str, Any]
    ) -> dict:
        blockers = materials["preflight_blockers"]
        artifacts.remove_if_exists("result.json")
        self._remove_deployable_artifacts(artifacts)
        artifacts.append_audit(
            run_id=run["id"], status="NEEDS_FACTS", event_code="FACTS_REQUIRED",
            question_ids=[blocker["question_id"] for blocker in blockers],
            source_ids=[
                item["id"] for item in materials["resumes"] + materials["jobs"]
            ],
            gate_outcome="BLOCKED",
            claim_count=0,
        )
        updated = self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "NEEDS_FACTS",
            blocking_questions=blockers,
        )
        return self._decorate_run(updated)

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
                    source_ids=[
                        item["id"] for item in materials["resumes"] + materials["jobs"]
                    ],
                    gate_outcome="BLOCKED",
                    claim_count=len(result.claim_audit),
                )
                updated = self.repository.transition_rewrite_run(
                    run["id"], {"RUNNING"}, "NEEDS_FACTS", blocking_questions=questions,
                    expected_owner_token=self._owner_token,
                )
                return self._decorate_run(updated)
            validated_markdown = gate.rewritten_resume_markdown
            render_result = self._render_and_publish(
                artifacts, validated_markdown
            )
            artifacts.append_audit(
                run_id=run["id"], status="RUNNING", event_code="RENDER_COMPLETED",
                content_hash=_hash_text(validated_markdown),
                page_count=render_result.page_count,
                source_ids=[
                    item["id"] for item in materials["resumes"] + materials["jobs"]
                ],
                gate_outcome="PASS",
                claim_count=len(result.claim_audit),
            )
            try:
                updated = self.repository.transition_rewrite_run(
                    run["id"], {"RUNNING"}, "READY", blocking_questions=[],
                    expected_owner_token=self._owner_token,
                )
            except ValueError:
                self._remove_deployable_artifacts(artifacts)
                return self.get(run["id"])
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
        self._remove_deployable_artifacts(artifacts)
        code = error_code or _safe_error_code(error)
        artifacts.append_audit(
            run_id=run["id"], status="FAILED", event_code="RUN_FAILED", error_code=code,
            error_safe_hash=_hash_text(code),
        )
        self.repository.transition_rewrite_run(
            run["id"], {"QUEUED", "RUNNING"}, "FAILED", error_code=code,
            expected_owner_token=(
                self._owner_token if run["status"] == "RUNNING" else None
            ),
        )

    def _open_artifacts(self, run: dict) -> RunArtifacts:
        expected = self.runs_root.resolve() / run["id"]
        stored = Path(run["artifact_dir"]).resolve()
        if stored != expected:
            self._fail_artifact_location(run)
            raise ValueError("rewrite artifacts are invalid")
        return RunArtifacts.open(self.runs_root, run["id"])

    def _open_recovery_artifacts(self, run: dict) -> RunArtifacts:
        expected = self.runs_root.resolve() / run["id"]
        if Path(run["artifact_dir"]).resolve() != expected:
            raise ValueError("rewrite recovery artifacts are invalid")
        return RunArtifacts.open(self.runs_root, run["id"])

    def _fail_artifact_location(self, run: dict) -> None:
        if run["status"] in {"READY", "FAILED"}:
            return
        self.repository.transition_rewrite_run(
            run["id"], {run["status"]}, "FAILED", error_code="VALIDATION_ERROR",
            expected_owner_token=(
                self._owner_token if run["status"] == "RUNNING" else None
            ),
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

    def _remove_deployable_artifacts(self, artifacts: RunArtifacts) -> None:
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

    def _render_and_publish(
        self, artifacts: RunArtifacts, validated_markdown: str
    ):
        self._remove_deployable_artifacts(artifacts)
        try:
            with tempfile.TemporaryDirectory(
                prefix=".render-stage-", dir=artifacts.directory
            ) as raw_stage:
                stage = Path(raw_stage)
                staged_markdown = stage / "resume.md"
                staged_pdf = stage / "resume.pdf"
                staged_preview = stage / "preview"
                staged_markdown.write_text(
                    validated_markdown, encoding="utf-8", newline="\n"
                )
                try:
                    render_result = self.renderer.render(
                        validated_markdown, staged_pdf, staged_preview
                    )
                except RenderError:
                    raise
                except Exception:
                    raise RenderError(
                        "RENDER_ERROR", "Unable to render the resume safely."
                    ) from None
                self._publish_staged_deployables(artifacts, stage)
                return render_result
        except RenderError:
            raise
        except Exception:
            raise RenderError(
                "RENDER_ERROR", "Unable to publish the rendered resume safely."
            ) from None

    def _publish_staged_deployables(
        self, artifacts: RunArtifacts, stage: Path
    ) -> None:
        expected_parent = artifacts.directory.resolve()
        resolved_stage = stage.resolve()
        if (
            resolved_stage.parent != expected_parent
            or not stage.name.startswith(".render-stage-")
            or stage.is_symlink()
        ):
            raise RenderError("RENDER_ERROR", "Render staging path is invalid.")

        staged_markdown = stage / "resume.md"
        staged_pdf = stage / "resume.pdf"
        staged_preview = stage / "preview"
        if not self._is_regular_staged_file(staged_markdown, resolved_stage):
            raise RenderError("RENDER_ERROR", "Rendered Markdown staging is invalid.")
        if not self._is_regular_staged_file(staged_pdf, resolved_stage):
            raise RenderError("RENDER_ERROR", "Rendered PDF staging is invalid.")
        if staged_preview.exists() and not self._is_safe_preview_stage(
            staged_preview, resolved_stage
        ):
            raise RenderError("RENDER_ERROR", "Rendered preview staging is invalid.")

        official_markdown = artifacts.path("resume.md")
        official_pdf = artifacts.path("resume.pdf")
        official_preview = artifacts.directory / ".preview"
        if official_markdown.exists() or official_pdf.exists() or official_preview.exists():
            raise RenderError("RENDER_ERROR", "Deployable artifacts already exist.")
        try:
            staged_markdown.replace(official_markdown)
            staged_pdf.replace(official_pdf)
            if staged_preview.exists():
                staged_preview.replace(official_preview)
        except OSError:
            self._remove_deployable_artifacts(artifacts)
            raise RenderError(
                "RENDER_ERROR", "Unable to publish deployable artifacts safely."
            ) from None

    @staticmethod
    def _is_regular_staged_file(path: Path, stage: Path) -> bool:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.resolve().parent == stage
        )

    @staticmethod
    def _is_safe_preview_stage(path: Path, stage: Path) -> bool:
        if path.is_symlink() or not path.is_dir() or path.resolve().parent != stage:
            return False
        children = list(path.iterdir())
        return all(
            child.is_file()
            and not child.is_symlink()
            and child.resolve().parent == path.resolve()
            for child in children
        )

    def _new_lease_expiration(self) -> str:
        return (
            datetime.now(timezone.utc)
            + timedelta(seconds=self._lease_duration_seconds)
        ).isoformat(timespec="seconds")

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


def _title_family_blockers(jobs: list[dict]) -> list[dict[str, Any]]:
    if len(jobs) < 2:
        return []
    families = {
        family for job in jobs
        if (family := known_job_family(str(job.get("title", "")))) is not None
    }
    if len(families) < 2:
        return []
    return [{
        "question_id": MULTI_JOB_TITLE_FAMILY_BLOCKER_ID,
        "question": "Please reselect jobs from one title family and create a new run.",
        "blocking": True,
        "reason": "Selected job titles span multiple known career families.",
    }]


def _has_title_family_blocker(blockers: list[dict]) -> bool:
    return any(
        blocker.get("question_id") == MULTI_JOB_TITLE_FAMILY_BLOCKER_ID
        for blocker in blockers
    )


def _merge_fact_answers(existing: list[dict], answers: list[FactAnswer]) -> list[dict]:
    merged: dict[str, dict] = {}
    for item in [*existing, *answers]:
        answer = FactAnswer.model_validate(item)
        merged[answer.question_id] = answer.model_dump(mode="json")
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
