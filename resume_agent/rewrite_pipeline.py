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
            recovery_lease = self._new_lease_expiration()
            claimed = self.repository.claim_interrupted_rewrite_run(
                run["id"],
                expected_owner_token=run["owner_token"],
                expected_lease_expires_at=run["lease_expires_at"],
                recovery_owner_token=self._owner_token,
                recovery_lease_expires_at=recovery_lease,
            )
            if not claimed:
                continue
            try:
                updated = self.repository.fenced_rewrite_transition(
                    run["id"],
                    expected_owner_token=self._owner_token,
                    expected_lease_expires_at=recovery_lease,
                    new_status="FAILED",
                    error_code="INTERRUPTED",
                    error_summary="上次进程在运行期间中断",
                    mutate_artifacts=lambda: self._remove_deployable_artifacts(
                        artifacts
                    ),
                )
            except Exception:
                updated = self._authoritative_run(run["id"])
                if not (
                    updated
                    and updated["status"] == "FAILED"
                    and updated["error_code"] == "INTERRUPTED"
                ):
                    try:
                        self.repository.transition_rewrite_run(
                            run["id"], {"RUNNING"}, "RUNNING",
                            owner_token=self._owner_token,
                            lease_expires_at=run["lease_expires_at"],
                            expected_owner_token=self._owner_token,
                            expected_lease_expires_at=recovery_lease,
                        )
                    except Exception:
                        pass
                    continue
            self._append_audit_best_effort(
                artifacts,
                run_id=run["id"],
                status="FAILED",
                event_code="RUN_RECOVERED",
                error_code="INTERRUPTED",
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
        updated = self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "NEEDS_FACTS",
            blocking_questions=blockers,
        )
        self._append_audit_best_effort(
            artifacts,
            run_id=run["id"], status="NEEDS_FACTS", event_code="FACTS_REQUIRED",
            question_ids=[blocker["question_id"] for blocker in blockers],
            source_ids=[
                item["id"] for item in materials["resumes"] + materials["jobs"]
            ],
            gate_outcome="BLOCKED",
            claim_count=0,
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
            try:
                result = self.codex_runner.run(
                    materials["task"], self.schema_path, temporary, self.project_root
                )
            except CodexRunError:
                raise
            except (KeyboardInterrupt, SystemExit) as error:
                try:
                    self._fail(
                        run, artifacts, error, error_code="CODEX_PROCESS_ERROR"
                    )
                except BaseException:
                    pass
                raise
            except Exception as error:
                self._fail(
                    run, artifacts, error, error_code="CODEX_PROCESS_ERROR"
                )
                return self.get(run["id"])
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
                questions = [item.model_dump() for item in gate.blockers]
                try:
                    updated = self.repository.fenced_rewrite_transition(
                        run["id"],
                        expected_owner_token=self._owner_token,
                        expected_lease_expires_at=run["lease_expires_at"],
                        new_status="NEEDS_FACTS",
                        blocking_questions=questions,
                        mutate_artifacts=lambda: self._remove_deployable_artifacts(
                            artifacts
                        ),
                    )
                except Exception:
                    updated = self._authoritative_run(run["id"])
                    if not (updated and updated["status"] == "NEEDS_FACTS"):
                        return self._decorate_run(updated or run)
                self._append_facts_required(
                    run, artifacts, materials, result, questions
                )
                return self._decorate_run(updated)
            validated_markdown = gate.rewritten_resume_markdown
            return self._render_publish_and_mark_ready(
                run, artifacts, materials, result, validated_markdown
            )
        except (CodexRunError, InvalidModelResult, RenderError, ValidationError) as error:
            self._fail(run, artifacts, error)
            return self.get(run["id"])
        finally:
            temporary.unlink(missing_ok=True)

    def _render_publish_and_mark_ready(
        self,
        run: dict,
        artifacts: RunArtifacts,
        materials: dict[str, Any],
        result: CodexRewriteResult,
        validated_markdown: str,
    ) -> dict:
        try:
            render_result, updated = self._render_and_publish(
                run, artifacts, materials, result, validated_markdown
            )
            self._append_audit_best_effort(
                artifacts,
                run_id=run["id"], status="RUNNING",
                event_code="RENDER_COMPLETED",
                content_hash=_hash_text(validated_markdown),
                page_count=render_result.page_count,
                source_ids=[
                    item["id"]
                    for item in materials["resumes"] + materials["jobs"]
                ],
                gate_outcome="PASS",
                claim_count=len(result.claim_audit),
            )
            self._append_audit_best_effort(
                artifacts,
                run_id=run["id"], status="READY", event_code="RUN_READY",
                source_ids=[item["id"] for item in materials["resumes"] + materials["jobs"]],
            )
            return self._decorate_run(updated)
        except (KeyboardInterrupt, SystemExit):
            self._settle_publication_failure(
                run, artifacts, RenderError("RENDER_ERROR", "Rendering interrupted.")
            )
            raise
        except Exception as error:
            return self._settle_publication_failure(run, artifacts, error)

    def _fail_validation(self, run: dict, artifacts: RunArtifacts) -> None:
        self._fail(
            run, artifacts, InvalidModelResult("invalid request state"),
            error_code="VALIDATION_ERROR",
        )

    def _fail(
        self, run: dict, artifacts: RunArtifacts, error: BaseException,
        error_code: str | None = None,
    ) -> dict | None:
        code = error_code or _safe_error_code(error)
        if run["status"] == "RUNNING":
            try:
                updated = self.repository.fenced_rewrite_transition(
                    run["id"],
                    expected_owner_token=self._owner_token,
                    expected_lease_expires_at=run["lease_expires_at"],
                    new_status="FAILED",
                    error_code=code,
                    mutate_artifacts=lambda: self._remove_deployable_artifacts(
                        artifacts
                    ),
                )
            except Exception:
                updated = self._authoritative_run(run["id"])
                if not (
                    updated
                    and updated["status"] == "FAILED"
                    and updated["error_code"] == code
                ):
                    return updated
            self._append_run_failed(artifacts, run["id"], code)
            return updated
        updated = self.repository.transition_rewrite_run(
            run["id"], {"QUEUED"}, "FAILED", error_code=code,
        )
        self._append_audit_best_effort(
            artifacts,
            run_id=run["id"], status="FAILED", event_code="RUN_FAILED",
            error_code=code, error_safe_hash=_hash_text(code),
        )
        return updated

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
            expected_lease_expires_at=(
                run["lease_expires_at"] if run["status"] == "RUNNING" else None
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

    def _append_facts_required(
        self,
        run: dict,
        artifacts: RunArtifacts,
        materials: dict[str, Any],
        result: CodexRewriteResult,
        questions: list[dict[str, Any]],
    ) -> None:
        self._append_audit_best_effort(
            artifacts,
            run_id=run["id"], status="NEEDS_FACTS", event_code="FACTS_REQUIRED",
            question_ids=[item["question_id"] for item in questions],
            source_ids=[
                item["id"] for item in materials["resumes"] + materials["jobs"]
            ],
            gate_outcome="BLOCKED",
            claim_count=len(result.claim_audit),
        )

    def _append_run_failed(
        self, artifacts: RunArtifacts, run_id: str, code: str
    ) -> None:
        self._append_audit_best_effort(
            artifacts,
            run_id=run_id, status="FAILED", event_code="RUN_FAILED",
            error_code=code, error_safe_hash=_hash_text(code),
        )

    @staticmethod
    def _append_audit_best_effort(
        artifacts: RunArtifacts, **event: Any
    ) -> None:
        try:
            artifacts.append_audit(**event)
        except Exception:
            pass

    def _publish_ready_artifacts(
        self,
        artifacts: RunArtifacts,
        stage: Path,
        render_result: Any,
    ) -> None:
        if not self._remove_publication_artifacts(
            artifacts, preserved_stage=stage
        ):
            raise OSError("deployable artifact cleanup is incomplete")
        self._publish_staged_deployables(artifacts, stage, render_result)

    def _remove_deployable_artifacts(self, artifacts: RunArtifacts) -> None:
        if not self._remove_publication_artifacts(artifacts):
            raise OSError("deployable artifact cleanup is incomplete")

    def _remove_publication_artifacts(
        self, artifacts: RunArtifacts, *, preserved_stage: Path | None = None
    ) -> bool:
        run_directory = artifacts.directory.resolve()
        try:
            resolved_preserved_stage = (
                preserved_stage.resolve() if preserved_stage is not None else None
            )
            targets = [
                artifacts.directory / "resume.md",
                artifacts.directory / "resume.pdf",
                artifacts.directory / ".preview",
                *(
                    target
                    for target in artifacts.directory.glob(".render-stage-*")
                    if target.resolve() != resolved_preserved_stage
                ),
            ]
        except OSError:
            return False
        for target in targets:
            try:
                if target.is_symlink():
                    target.unlink(missing_ok=True)
                elif target.is_dir():
                    if target.resolve().parent != run_directory:
                        return False
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            except Exception:
                continue
        try:
            official_targets = (
                artifacts.directory / "resume.md",
                artifacts.directory / "resume.pdf",
                artifacts.directory / ".preview",
            )
            return not any(
                target.exists() or target.is_symlink()
                for target in official_targets
            ) and not any(
                target.resolve() != resolved_preserved_stage
                for target in artifacts.directory.glob(".render-stage-*")
            )
        except OSError:
            return False

    def _settle_publication_failure(
        self, run: dict, artifacts: RunArtifacts, error: Exception
    ) -> dict:
        code = _publication_error_code(error)
        try:
            updated = self.repository.fenced_rewrite_transition(
                run["id"],
                expected_owner_token=self._owner_token,
                expected_lease_expires_at=run["lease_expires_at"],
                new_status="FAILED",
                error_code=code,
                mutate_artifacts=lambda: self._remove_deployable_artifacts(
                    artifacts
                ),
            )
        except Exception:
            updated = self._authoritative_run(run["id"])
            if not (
                updated
                and updated["status"] == "FAILED"
                and updated["error_code"] == code
            ):
                return self._decorate_run(updated or run)
        self._append_run_failed(artifacts, run["id"], code)
        return self._decorate_run(updated)

    def _authoritative_run(self, run_id: str) -> dict | None:
        try:
            return self.repository.get_rewrite_run(run_id)
        except Exception:
            return None

    def _render_and_publish(
        self,
        run: dict,
        artifacts: RunArtifacts,
        materials: dict[str, Any],
        result: CodexRewriteResult,
        validated_markdown: str,
    ):
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
                try:
                    updated = self.repository.fenced_rewrite_transition(
                        run["id"],
                        expected_owner_token=self._owner_token,
                        expected_lease_expires_at=run["lease_expires_at"],
                        new_status="READY",
                        blocking_questions=[],
                        mutate_artifacts=lambda: self._publish_ready_artifacts(
                            artifacts, stage, render_result
                        ),
                    )
                except Exception:
                    updated = self._authoritative_run(run["id"])
                    if not (updated and updated["status"] == "READY"):
                        raise
                return render_result, updated
        except RenderError:
            raise
        except Exception:
            raise RenderError(
                "RENDER_ERROR", "Unable to publish the rendered resume safely."
            ) from None

    def _publish_staged_deployables(
        self, artifacts: RunArtifacts, stage: Path, render_result: Any
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
        self._validate_staged_previews(
            render_result, staged_preview, resolved_stage
        )

        official_markdown = artifacts.path("resume.md")
        official_pdf = artifacts.path("resume.pdf")
        official_preview = artifacts.directory / ".preview"
        if official_markdown.exists() or official_pdf.exists() or official_preview.exists():
            raise RenderError("RENDER_ERROR", "Deployable artifacts already exist.")
        try:
            staged_markdown.replace(official_markdown)
            staged_pdf.replace(official_pdf)
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

    @classmethod
    def _validate_staged_previews(
        cls, render_result: Any, preview_dir: Path, stage: Path
    ) -> None:
        page_count = getattr(render_result, "page_count", None)
        preview_paths = getattr(render_result, "preview_paths", None)
        if (
            type(page_count) is not int
            or page_count < 1
            or not isinstance(preview_paths, (list, tuple))
            or len(preview_paths) != page_count
            or not cls._is_safe_preview_stage(preview_dir, stage)
        ):
            raise RenderError("RENDER_ERROR", "Rendered preview contract is invalid.")
        try:
            declared = [Path(path) for path in preview_paths]
        except TypeError:
            raise RenderError(
                "RENDER_ERROR", "Rendered preview contract is invalid."
            ) from None
        resolved_preview = preview_dir.resolve()
        if any(
            not cls._is_regular_staged_file(path, resolved_preview)
            for path in declared
        ):
            raise RenderError("RENDER_ERROR", "Rendered preview contract is invalid.")
        declared_paths = {path.resolve() for path in declared}
        actual_paths = {path.resolve() for path in preview_dir.iterdir()}
        if len(declared_paths) != page_count or declared_paths != actual_paths:
            raise RenderError("RENDER_ERROR", "Rendered preview contract is invalid.")

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


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, CodexRunError):
        return error.code if error.code in {"CODEX_UNAVAILABLE", "CODEX_TIMEOUT", "CODEX_PROCESS_ERROR", "INVALID_MODEL_RESULT"} else "CODEX_PROCESS_ERROR"
    if isinstance(error, RenderError):
        return "RENDER_ERROR" if error.code == "RENDER_ERROR" else "RENDER_VALIDATION_ERROR"
    return "INVALID_MODEL_RESULT"


def _publication_error_code(error: Exception) -> str:
    if isinstance(error, RenderError):
        return _safe_error_code(error)
    return "RENDER_ERROR"
