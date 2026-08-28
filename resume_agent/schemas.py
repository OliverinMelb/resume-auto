from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .rewrite_schemas import GapAcceptanceRequest
from .targeted_jobs import JobTarget


class StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class BroadRewriteCreate(StrictApiModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    source_resume_ids: list[
        Annotated[str, Field(min_length=1, max_length=200)]
    ] = Field(min_length=1, max_length=50)


class TargetedRewriteCreate(StrictApiModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    source_resume_ids: list[
        Annotated[str, Field(min_length=1, max_length=200)]
    ] = Field(min_length=1, max_length=50)
    job_id: str | None = Field(default=None, min_length=1, max_length=200)
    jd_text: str | None = Field(default=None, min_length=1, max_length=100_000)
    jd_url: str | None = Field(default=None, min_length=1, max_length=8192)
    title: str | None = Field(default=None, max_length=500)
    company: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_target(self):
        JobTarget.from_payload({
            "job_id": self.job_id,
            "jd_text": self.jd_text,
            "jd_url": self.jd_url,
            "title": self.title,
            "company": self.company,
        })
        return self

    def to_job_target(self) -> JobTarget:
        return JobTarget.from_payload({
            "job_id": self.job_id,
            "jd_text": self.jd_text,
            "jd_url": self.jd_url,
            "title": self.title,
            "company": self.company,
        })


class GapAcceptanceCreate(StrictApiModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    evidence: GapAcceptanceRequest


class CandidateCreate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=5000)


class SourceResumeCreate(BaseModel):
    label: str = Field(default="原始简历", min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=200_000)


class JobCreate(BaseModel):
    candidate_id: str | None = None
    external_job_id: str | None = Field(default=None, max_length=200)
    source: str = Field(default="manual", min_length=1, max_length=50)
    title: str = Field(min_length=1, max_length=500)
    salary: str = Field(default="", max_length=200)
    detail: str = Field(min_length=1, max_length=100_000)
    company: str = Field(default="", max_length=500)
    score: float | None = Field(default=None, ge=0, le=100)
    qualified: bool | None = None
    search_keyword: str | None = Field(default=None, max_length=500)
    job_fingerprint: str | None = Field(default=None, max_length=128)
    source_logged_at: str | None = Field(default=None, max_length=100)


class ResearchFinding(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=5000)
    source_job_ids: list[str] = Field(default_factory=list, max_length=100)
    source_type: Literal["explicit", "manual_verified"] = "explicit"


class ResearchCreate(BaseModel):
    candidate_id: str
    target_role: str = Field(min_length=1, max_length=300)
    job_ids: list[str] = Field(min_length=1, max_length=100)
    manual_findings: list[ResearchFinding] = Field(default_factory=list, max_length=200)
    notes: str = Field(default="", max_length=20_000)


class PromptBuildRequest(BaseModel):
    candidate_id: str
    source_resume_id: str | None = None
    source_resume_ids: list[str] = Field(default_factory=list, max_length=50)
    research_report_id: str
    extra_instructions: str = Field(default="", max_length=10_000)

    @model_validator(mode="after")
    def validate_resume_sources(self):
        if not self.source_resume_id and not self.source_resume_ids:
            raise ValueError("至少提供一份原始简历")
        return self


class CandidateProfilePromptRequest(BaseModel):
    candidate_id: str
    source_resume_ids: list[str] = Field(default_factory=list, max_length=50)
    extra_instructions: str = Field(default="", max_length=10_000)


class JobDecisionImportRequest(BaseModel):
    candidate_id: str
    qualified_only: bool = True
    threshold: float = Field(default=50, ge=0, le=100)
    date_from: str | None = None
    date_to: str | None = None
    search_keywords: list[str] = Field(default_factory=list, max_length=100)


DeliveryEventType = Literal[
    "application_submitted",
    "recruiter_replied",
    "resume_sent",
    "interview",
    "rejected",
    "offer",
    "withdrawn",
]


class DeliveryEventCreate(BaseModel):
    candidate_id: str | None = None
    job_id: str | None = None
    event_type: DeliveryEventType
    event_at: datetime | None = None
    source: str = Field(default="manual", min_length=1, max_length=50)
