import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ResumeAgentSettings:
    database_path: Path
    legacy_action_log_path: Path
    legacy_decision_log_path: Path
    allow_remote: bool
    runs_root: Path
    codex_timeout_seconds: int


def get_settings() -> ResumeAgentSettings:
    database_value = os.getenv("RESUME_AGENT_DB_PATH") or str(
        PROJECT_ROOT / "resume_agent.db"
    )
    action_log_value = os.getenv("RESUME_AGENT_ACTION_LOG_PATH") or str(
        PROJECT_ROOT / "job_actions.jsonl"
    )
    decision_log_value = os.getenv("RESUME_AGENT_DECISION_LOG_PATH") or str(
        PROJECT_ROOT / "job_decisions.jsonl"
    )
    runs_root_value = os.getenv("RESUME_AGENT_RUNS_ROOT") or str(
        PROJECT_ROOT / "analysis" / "runs"
    )
    timeout_value = os.getenv("RESUME_AGENT_CODEX_TIMEOUT_SECONDS") or "900"
    try:
        codex_timeout_seconds = int(timeout_value)
    except ValueError:
        raise ValueError("RESUME_AGENT_CODEX_TIMEOUT_SECONDS must be an integer") from None
    if not 30 <= codex_timeout_seconds <= 3600:
        raise ValueError(
            "RESUME_AGENT_CODEX_TIMEOUT_SECONDS must be between 30 and 3600"
        )
    database_path = Path(database_value).expanduser()
    legacy_action_log_path = Path(action_log_value).expanduser()
    return ResumeAgentSettings(
        database_path=database_path,
        legacy_action_log_path=legacy_action_log_path,
        legacy_decision_log_path=Path(decision_log_value).expanduser(),
        allow_remote=os.getenv("RESUME_AGENT_ALLOW_REMOTE", "false").lower()
        in {"1", "true", "yes"},
        runs_root=Path(runs_root_value).expanduser(),
        codex_timeout_seconds=codex_timeout_seconds,
    )
