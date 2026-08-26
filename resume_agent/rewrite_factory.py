from pathlib import Path

from .codex_runner import CodexCliAdapter
from .repository import Repository
from .resume_renderer import ReportLabAdapter
from .rewrite_pipeline import ResumeRewritePipeline
from .settings import PROJECT_ROOT, ResumeAgentSettings


def create_rewrite_pipeline(
    repository: Repository, settings: ResumeAgentSettings
) -> ResumeRewritePipeline:
    pipeline = ResumeRewritePipeline(
        repository=repository,
        runs_root=settings.runs_root,
        schema_path=Path(__file__).with_name("codex_resume_result.schema.json"),
        project_root=PROJECT_ROOT,
        codex_runner=CodexCliAdapter("codex", settings.codex_timeout_seconds),
        renderer=ReportLabAdapter(),
    )
    repository.recover_interrupted_rewrite_runs()
    return pipeline
