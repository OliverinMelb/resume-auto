from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FactAnswer(StrictModel):
    question_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=5000)
    source_note: str = Field(min_length=1, max_length=2000)


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


class ClaimAudit(StrictModel):
    claim: str
    source_resume_ids: list[str]
    source_quotes: list[str]
    job_ids: list[str]
    change_type: Literal["unchanged", "compressed", "reordered", "strengthened"]
    fact_status: Literal["source_verified", "user_confirmed", "unverified"]
    measurement_basis: str | None = None


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
