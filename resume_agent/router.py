import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from .discovery_factory import create_discovery_dependencies
from .discovery_schemas import DiscoveryRequest
from .repository import NotFoundError, Repository
from .rewrite_factory import create_rewrite_pipeline
from .rewrite_cli import _redact_nested
from .rewrite_schemas import FactAnswersRequest, RewriteRequest
from .schemas import (
    BroadRewriteCreate,
    CandidateProfilePromptRequest,
    CandidateCreate,
    DeliveryEventCreate,
    GapAcceptanceCreate,
    JobDecisionImportRequest,
    JobCreate,
    PromptBuildRequest,
    ResearchCreate,
    SourceResumeCreate,
    TargetedRewriteCreate,
)
from .service import ResumeAgentService
from .settings import get_settings


_settings = get_settings()


def require_local_request(request: Request):
    if _settings.allow_remote:
        return
    host = request.client.host if request.client else ""
    if host == "testclient":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise HTTPException(
        status_code=403,
        detail="Resume Agent默认只允许本机访问",
    )


class _SafeValidationRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def safe_handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "INVALID_REQUEST"},
                )

        return safe_handler


router = APIRouter(
    prefix="/resume-agent/v1",
    tags=["resume-agent"],
    dependencies=[Depends(require_local_request)],
    route_class=_SafeValidationRoute,
)
_repository = Repository(_settings.database_path)
_service = ResumeAgentService(
    _repository,
    _settings.legacy_action_log_path,
    _settings.legacy_decision_log_path,
)
_rewrite_pipeline = create_rewrite_pipeline(_repository, _settings)
_workflow_dependencies = create_discovery_dependencies(
    _settings,
    repository=_repository,
    rewrite_pipeline=_rewrite_pipeline,
)


def _raise_api_error(error: Exception):
    if isinstance(error, NotFoundError):
        raise _api_exception(404, "RESOURCE_NOT_FOUND") from error
    if isinstance(error, ValueError):
        message = str(error)
        if message == "SOURCE_INTERACTION_REQUIRED":
            raise _api_exception(409, "SOURCE_INTERACTION_REQUIRED") from error
        if message == "PARTIAL_JD":
            raise _api_exception(409, "PARTIAL_JD") from error
        if message in {
            "SOURCE_ADAPTER_UNAVAILABLE", "SOURCE_ADAPTER_MISMATCH",
            "SOURCE_RESOLUTION_FAILED", "SOURCE_RESULT_INVALID",
            "SOURCE_RESULT_MISMATCH",
        }:
            raise _api_exception(502, "SOURCE_UNAVAILABLE") from error
        if any(token in message.casefold() for token in (
            "conflict", "mismatch", "not pollable", "busy",
        )):
            detail = (
                "GAP_ACCEPTANCE_CONFLICT"
                if "gap acceptance" in message.casefold()
                else "RESOURCE_CONFLICT"
            )
            raise _api_exception(409, detail) from error
        raise _api_exception(400, "INVALID_REQUEST") from error
    raise _api_exception(502, "WORKFLOW_FAILED") from error


def _api_exception(status_code: int, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=_redact_nested(detail),
    )


@router.get("/health")
def health():
    available = _rewrite_pipeline.available()
    return {
        "status": "ok",
        "model_enabled": available,
        "resume_modification_enabled": available,
    }


def _rewrite_response(run: dict):
    status = run.get("status")
    if status in {"READY", "RUNNING", "NEEDS_FACTS"}:
        return _redact_nested(run)
    if status == "FAILED" and run.get("error_code") == "CODEX_TIMEOUT":
        raise _api_exception(504, "Codex timed out")
    raise _api_exception(502, "Resume rewrite failed")


def _discovery_response(run: dict):
    return _redact_nested({
        "id": run.get("id"),
        "candidate_id": run.get("candidate_id"),
        "status": run.get("status", "FAILED"),
        "error_code": run.get("error_code"),
        "imported_job_ids": run.get("imported_job_ids", []),
        "source_statuses": run.get("source_statuses", {}),
    })


@router.post("/rewrite-runs")
def create_rewrite_run(payload: RewriteRequest):
    try:
        return _rewrite_response(_rewrite_pipeline.run(payload))
    except (NotFoundError, ValueError) as error:
        _raise_api_error(error)


@router.get("/rewrite-runs/{run_id}")
def get_rewrite_run(run_id: str):
    try:
        return _rewrite_response(_rewrite_pipeline.get(run_id))
    except (NotFoundError, ValueError) as error:
        _raise_api_error(error)


@router.post("/rewrite-runs/{run_id}/facts")
def resume_rewrite_run(run_id: str, payload: FactAnswersRequest):
    try:
        return _rewrite_response(_rewrite_pipeline.resume(run_id, payload.fact_answers))
    except (NotFoundError, ValueError) as error:
        _raise_api_error(error)


@router.post("/discovery-runs", status_code=201)
def create_discovery_run(payload: DiscoveryRequest):
    try:
        return _discovery_response(
            _workflow_dependencies.orchestrator.start(payload)
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/discovery-runs/{run_id}")
def get_discovery_run(run_id: str):
    try:
        return _discovery_response(
            _workflow_dependencies.orchestrator.get(run_id)
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/discovery-runs/{run_id}/poll")
def poll_discovery_run(run_id: str):
    try:
        return _discovery_response(
            _workflow_dependencies.orchestrator.poll(run_id)
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/discovery-runs/{run_id}/broad-rewrite")
def create_broad_rewrite(run_id: str, payload: BroadRewriteCreate):
    try:
        run = _workflow_dependencies.orchestrator.build_broad_rewrite(
            run_id,
            payload.candidate_id,
            payload.source_resume_ids,
            _workflow_dependencies.rewrite_pipeline,
        )
        return _rewrite_response(run)
    except Exception as error:
        _raise_api_error(error)


@router.post("/targeted-rewrite")
def create_targeted_rewrite(payload: TargetedRewriteCreate):
    try:
        job_id = _workflow_dependencies.target_resolver.resolve(
            payload.to_job_target(), payload.candidate_id
        )
        run = _workflow_dependencies.rewrite_pipeline.run(RewriteRequest(
            candidate_id=payload.candidate_id,
            job_ids=[job_id],
            source_resume_ids=payload.source_resume_ids,
            output_name="targeted-resume.pdf",
        ))
        return _rewrite_response(run)
    except Exception as error:
        _raise_api_error(error)


@router.post("/gap-acceptances", status_code=201)
def create_gap_acceptance(payload: GapAcceptanceCreate):
    try:
        acceptance = _workflow_dependencies.repository.create_gap_acceptance(
            payload.candidate_id, payload.evidence.model_dump(mode="json")
        )
        return {
            "id": acceptance["id"],
            "candidate_id": acceptance["candidate_id"],
            "gap_id": acceptance["gap_id"],
            "status": "ACCEPTED",
        }
    except Exception as error:
        _raise_api_error(error)


@router.post("/candidates", status_code=201)
def create_candidate(payload: CandidateCreate):
    return _repository.create_candidate(
        display_name=payload.display_name,
        target_roles=payload.target_roles,
        notes=payload.notes,
    )


@router.get("/candidates/{candidate_id}")
def get_candidate(candidate_id: str):
    try:
        return _repository.get_candidate(candidate_id)
    except Exception as error:
        _raise_api_error(error)


@router.post("/candidates/{candidate_id}/source-resumes", status_code=201)
def create_source_resume(candidate_id: str, payload: SourceResumeCreate):
    try:
        return _repository.create_source_resume(
            candidate_id=candidate_id,
            label=payload.label,
            content=payload.content,
        )
    except Exception as error:
        _raise_api_error(error)


@router.get("/source-resumes/{resume_id}")
def get_source_resume(resume_id: str):
    try:
        return _repository.get_source_resume(resume_id)
    except Exception as error:
        _raise_api_error(error)


@router.get("/candidates/{candidate_id}/source-resumes")
def list_source_resumes(candidate_id: str):
    try:
        return {"source_resumes": _repository.list_source_resumes(candidate_id)}
    except Exception as error:
        _raise_api_error(error)


@router.post("/jobs", status_code=201)
def create_job(payload: JobCreate):
    try:
        return _repository.create_job(payload.model_dump())
    except Exception as error:
        _raise_api_error(error)


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    try:
        return _repository.get_job(job_id)
    except Exception as error:
        _raise_api_error(error)


@router.post("/research-reports", status_code=201)
def create_research_report(payload: ResearchCreate):
    try:
        return _service.create_research(payload.model_dump())
    except Exception as error:
        _raise_api_error(error)


@router.get("/research-reports/{report_id}")
def get_research_report(report_id: str):
    try:
        return _repository.get_research_report(report_id)
    except Exception as error:
        _raise_api_error(error)


@router.post("/prompts/resume-rewrite")
def build_resume_rewrite_prompt(payload: PromptBuildRequest):
    try:
        return _service.build_prompt(payload.model_dump())
    except Exception as error:
        _raise_api_error(error)


@router.post("/prompts/candidate-profile")
def build_candidate_profile_prompt(payload: CandidateProfilePromptRequest):
    try:
        return _service.build_candidate_profile_prompt(payload.model_dump())
    except Exception as error:
        _raise_api_error(error)


@router.post("/imports/job-decisions")
def import_job_decision_log(payload: JobDecisionImportRequest):
    try:
        return _service.import_decisions(payload.model_dump())
    except Exception as error:
        _raise_api_error(error)


@router.get("/market-report")
def get_market_report(
    candidate_id: str,
    qualified_only: bool = True,
    search_keyword: str | None = None,
):
    try:
        return _service.market_report(
            candidate_id=candidate_id,
            qualified_only=qualified_only,
            search_keyword=search_keyword,
        )
    except Exception as error:
        _raise_api_error(error)


@router.post("/delivery-events", status_code=201)
def create_delivery_event(payload: DeliveryEventCreate):
    data = payload.model_dump()
    if data["event_at"] is not None:
        data["event_at"] = data["event_at"].isoformat(timespec="seconds")
    try:
        return _repository.create_delivery_event(data)
    except Exception as error:
        _raise_api_error(error)


@router.get("/delivery-events")
def list_delivery_events(
    candidate_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
):
    return {
        "events": _repository.list_delivery_events(
            candidate_id=candidate_id,
            date_from=date_from,
            date_to=date_to,
        )
    }


@router.get("/delivery-stats")
def get_delivery_stats(
    candidate_id: str | None = None,
    date_from: str | None = Query(default=None, description="ISO时间下界"),
    date_to: str | None = Query(default=None, description="ISO时间上界"),
    include_legacy: bool = True,
):
    return _service.delivery_stats(
        candidate_id=candidate_id,
        date_from=date_from,
        date_to=date_to,
        include_legacy=include_legacy,
    )
