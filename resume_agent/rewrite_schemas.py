import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactAnswer(StrictModel):
    question_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=5000)
    source_note: str = Field(min_length=1, max_length=2000)
    user_confirmed: bool = Field(default=False, strict=True)
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_confirmation(self):
        if self.user_confirmed != (self.confirmed_at is not None):
            raise ValueError(
                "user_confirmed and confirmed_at must be supplied together"
            )
        if self.confirmed_at is not None:
            offset = self.confirmed_at.utcoffset()
            if self.confirmed_at.tzinfo is None or offset is None:
                raise ValueError("confirmed_at must include a timezone")
            if offset != timedelta(0):
                raise ValueError("confirmed_at must be UTC")
        return self


class FactAnswersRequest(StrictModel):
    fact_answers: list[FactAnswer] = Field(min_length=1, max_length=200)


class RewriteRequest(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=200)
    job_ids: list[str] = Field(min_length=1, max_length=100)
    source_resume_ids: list[str] = Field(min_length=1, max_length=50)
    output_name: str = Field(min_length=1, max_length=180)
    fact_answers: list[FactAnswer] = Field(default_factory=list, max_length=200)

    @field_validator("output_name")
    @classmethod
    def validate_output_name(cls, value: str) -> str:
        forbidden = {"<", ">", ":", '"', "/", "\\", "|", "?", "*"}
        if (
            any(character in value for character in forbidden)
            or value in {".", ".."}
            or value.rstrip(" .") != value
        ):
            raise ValueError("output_name 必须是安全文件名，不能包含路径")
        normalized = value if value.lower().endswith(".pdf") else f"{value}.pdf"
        stem = Path(normalized).stem.split(".", 1)[0].upper()
        reserved = {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
        if stem in reserved:
            raise ValueError("output_name 不能使用 Windows 保留名")
        return normalized


RewriteRunStatus = Literal["QUEUED", "RUNNING", "NEEDS_FACTS", "READY", "FAILED"]


class ScoreItem(StrictModel):
    dimension: str
    score: float = Field(ge=0, le=100)
    evidence: list[str]


class RiskItem(StrictModel):
    severity: Literal["low", "medium", "high"]
    text: str


class MissingInformation(StrictModel):
    question_id: str
    question: str
    blocking: bool
    reason: str


class RewriteReason(StrictModel):
    rewritten_text: str
    reason: str
    source_resume_ids: list[str]
    job_ids: list[str]


ClaimStatus = Literal["verified", "reframed", "in_progress"]


class GapAcceptancePlan(StrictModel):
    gap_id: str = Field(pattern=r"^gap-[0-9a-f]{12}$")
    jd_requirements: list[str] = Field(min_length=1, max_length=20)
    project_scope: str = Field(min_length=1, max_length=2000)
    required_artifacts: list[str] = Field(min_length=1, max_length=50)
    test_commands: list[str] = Field(min_length=1, max_length=20)
    acceptance_metrics: list[str] = Field(min_length=1, max_length=20)
    interview_topics: list[str] = Field(default_factory=list, max_length=50)


LOWER_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class TestRecord(StrictModel):
    command: str = Field(min_length=1, max_length=4000)
    exit_code: int = Field(strict=True, ge=-(2**31), le=2**31 - 1)
    output_sha256: str = Field(pattern=LOWER_SHA256_PATTERN)

    @field_validator("command")
    @classmethod
    def normalize_command(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("command must not be blank")
        return normalized


class GapAcceptanceRequest(StrictModel):
    gap_id: str = Field(pattern=r"^gap-[0-9a-f]{12}$")
    project_path: str = Field(min_length=1, max_length=2000)
    verified_claims: list[str] = Field(min_length=1, max_length=50)
    artifact_hashes: dict[str, str] = Field(min_length=1, max_length=100)
    test_records: list[TestRecord] = Field(min_length=1, max_length=50)
    completed_at: datetime
    user_confirmed: Literal[True]
    confirmed_at: datetime

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("project_path must not be blank")
        return normalized

    @field_validator("verified_claims")
    @classmethod
    def normalize_verified_claims(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        identities = [
            re.sub(
                r"\s+",
                " ",
                "".join(
                    character
                    for character in unicodedata.normalize("NFKC", value)
                    if unicodedata.category(character) != "Cf"
                ),
            ).strip().casefold()
            for value in normalized
        ]
        if any(
            not identity or len(value) > 2000
            for value, identity in zip(normalized, identities)
        ):
            raise ValueError("verified_claims entries must be 1 to 2000 characters")
        if len(identities) != len(set(identities)):
            raise ValueError("verified_claims must not contain duplicates")
        return normalized

    @field_validator("artifact_hashes")
    @classmethod
    def normalize_artifact_hashes(cls, values: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for raw_name, digest in values.items():
            name = raw_name.strip()
            if not name or len(name) > 500:
                raise ValueError("artifact hash names must be 1 to 500 characters")
            if name in normalized:
                raise ValueError("artifact hash names must be unique after trimming")
            if re.fullmatch(LOWER_SHA256_PATTERN, digest) is None:
                raise ValueError("artifact hashes must be lowercase SHA-256 values")
            normalized[name] = digest
        return normalized

    @field_validator("completed_at", "confirmed_at")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acceptance timestamps must include a timezone")
        if value.utcoffset() != timedelta(0):
            raise ValueError("acceptance timestamps must be UTC")
        return value.astimezone(timezone.utc)

    @field_validator("completed_at", "confirmed_at", mode="before")
    @classmethod
    def reject_implicit_timestamps(cls, value):
        if not isinstance(value, (str, datetime)):
            raise ValueError("acceptance timestamps must be explicit datetimes")
        return value

    @field_validator("user_confirmed", mode="before")
    @classmethod
    def require_explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("user_confirmed must be explicitly true")
        return value

    @model_validator(mode="after")
    def validate_confirmation_time(self):
        if self.confirmed_at < self.completed_at:
            raise ValueError("confirmed_at must not precede completed_at")
        return self


class ClaimAudit(StrictModel):
    claim: str
    source_resume_ids: list[str]
    source_quotes: list[str]
    job_ids: list[str]
    change_type: Literal["unchanged", "compressed", "reordered", "strengthened"]
    fact_status: Literal["source_verified", "user_confirmed", "unverified"]
    measurement_basis: str | None = None
    claim_status: ClaimStatus
    gap_id: str | None = Field(default=None, pattern=r"^gap-[0-9a-f]{12}$")
    acceptance_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_experience_status(self):
        if self.claim_status == "in_progress":
            if self.gap_id is None:
                raise ValueError("in_progress claim must include gap_id")
            if self.acceptance_id is not None:
                raise ValueError("in_progress claim cannot include acceptance_id")
        elif (self.gap_id is None) != (self.acceptance_id is None):
            raise ValueError(
                "verified or reframed claim must include both gap_id and "
                "acceptance_id when promoted from an accepted gap"
            )
        return self


class CodexRewriteResult(StrictModel):
    verdict: str
    scorecard: list[ScoreItem]
    risks: list[RiskItem]
    missing_information: list[MissingInformation]
    rewritten_resume_markdown: str
    rewrite_reasons: list[RewriteReason]
    depth_recommendations: list[str]
    interviewer_probes: list[str]
    claim_audit: list[ClaimAudit]
    gap_acceptance_plans: list[GapAcceptancePlan]

    @model_validator(mode="after")
    def validate_gap_acceptance_plans(self):
        in_progress_gap_ids = {
            audit.gap_id
            for audit in self.claim_audit
            if audit.claim_status == "in_progress"
        }
        plan_gap_ids = [plan.gap_id for plan in self.gap_acceptance_plans]
        if len(plan_gap_ids) != len(set(plan_gap_ids)):
            raise ValueError("gap_acceptance_plans must contain unique gap_id values")
        if set(plan_gap_ids) != in_progress_gap_ids:
            raise ValueError(
                "gap_acceptance_plans must match in_progress claim gap_id values"
            )
        return self
