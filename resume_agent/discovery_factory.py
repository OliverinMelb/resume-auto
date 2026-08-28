"""Construct one linked discovery/rewrite dependency graph per caller."""

from __future__ import annotations

from dataclasses import dataclass

from .discovery_http import LocalJsonClient
from .discovery_orchestrator import JobDiscoveryOrchestrator
from .discovery_sources import BossResearchAdapter, NowcoderResearchAdapter
from .repository import Repository
from .rewrite_factory import create_rewrite_pipeline
from .settings import ResumeAgentSettings, normalize_discovery_endpoint
from .targeted_jobs import TargetedJobResolver


@dataclass(frozen=True)
class DiscoveryDependencies:
    repository: Repository
    rewrite_pipeline: object
    orchestrator: JobDiscoveryOrchestrator
    target_resolver: TargetedJobResolver


def create_discovery_dependencies(
    settings: ResumeAgentSettings,
    *,
    repository: Repository | None = None,
    rewrite_pipeline: object | None = None,
) -> DiscoveryDependencies:
    """Build independent source clients sharing one repository and orchestrator."""
    if (
        type(settings.discovery_timeout_seconds) is not int
        or not 1 <= settings.discovery_timeout_seconds <= 120
    ):
        raise ValueError("discovery timeout must be between 1 and 120 seconds")
    boss_endpoint = normalize_discovery_endpoint(
        settings.boss_research_endpoint
    )
    nowcoder_endpoint = normalize_discovery_endpoint(
        settings.nowcoder_research_endpoint
    )
    owned_repository = repository or Repository(settings.database_path)
    pipeline = rewrite_pipeline or create_rewrite_pipeline(
        owned_repository, settings
    )
    boss = BossResearchAdapter(LocalJsonClient(
        boss_endpoint,
        timeout_seconds=settings.discovery_timeout_seconds,
    ))
    nowcoder = NowcoderResearchAdapter(LocalJsonClient(
        nowcoder_endpoint,
        timeout_seconds=settings.discovery_timeout_seconds,
    ))
    adapters = {"boss": boss, "nowcoder": nowcoder}
    return DiscoveryDependencies(
        repository=owned_repository,
        rewrite_pipeline=pipeline,
        orchestrator=JobDiscoveryOrchestrator(
            owned_repository, settings.runs_root, adapters
        ),
        target_resolver=TargetedJobResolver(owned_repository, adapters),
    )
