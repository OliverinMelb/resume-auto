import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ResumeAgentSettings:
    database_path: Path
    legacy_action_log_path: Path
    legacy_decision_log_path: Path
    allow_remote: bool
    runs_root: Path
    codex_timeout_seconds: int
    boss_research_endpoint: str = "http://127.0.0.1:8011"
    nowcoder_research_endpoint: str = "http://127.0.0.1:8012"
    discovery_timeout_seconds: int = 30


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
    discovery_timeout_value = (
        os.getenv("RESUME_AGENT_DISCOVERY_TIMEOUT_SECONDS") or "30"
    )
    try:
        discovery_timeout_seconds = int(discovery_timeout_value)
    except ValueError:
        raise ValueError("discovery timeout must be an integer") from None
    if not 1 <= discovery_timeout_seconds <= 120:
        raise ValueError("discovery timeout must be between 1 and 120 seconds")
    boss_research_endpoint = _discovery_endpoint(
        "RESUME_AGENT_BOSS_RESEARCH_ENDPOINT", "http://127.0.0.1:8011"
    )
    nowcoder_research_endpoint = _discovery_endpoint(
        "RESUME_AGENT_NOWCODER_RESEARCH_ENDPOINT", "http://127.0.0.1:8012"
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
        boss_research_endpoint=boss_research_endpoint,
        nowcoder_research_endpoint=nowcoder_research_endpoint,
        discovery_timeout_seconds=discovery_timeout_seconds,
    )


def _discovery_endpoint(name: str, default: str) -> str:
    return normalize_discovery_endpoint(os.getenv(name) or default)


def normalize_discovery_endpoint(value: str) -> str:
    """Validate and normalize one root-only local discovery endpoint."""
    try:
        if not isinstance(value, str):
            raise TypeError
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
        valid = (
            parsed.scheme == "http"
            and hostname in {"localhost", "127.0.0.1", "::1"}
            and (port is None or 1 <= port <= 65535)
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and not parsed.netloc.endswith(":")
            and "?" not in value
            and "#" not in value
        )
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("discovery endpoint must be loopback HTTP")
    rendered_host = f"[{hostname}]" if hostname == "::1" else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"http://{rendered_host}{rendered_port}"
