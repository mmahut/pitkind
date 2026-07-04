from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints, ValidationInfo, field_validator, model_validator

from . import __version__

NonemptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


class ProvisionAssessment(BaseModel):
    provision: str
    requirement: str
    assessment: Literal["satisfied", "violated", "not_applicable", "cannot_determine"]
    reasoning: str
    evidence: str = ""


class ArgumentResponse(BaseModel):
    argument_id: NonemptyText
    assessment: Literal["accepted", "rebutted", "unresolved"]
    reasoning: NonemptyText
    evidence: NonemptyText


class MissingInformation(BaseModel):
    provision: NonemptyText
    question: NonemptyText
    source: Literal["chain_snapshot", "proposal", "other"]
    why_decisive: NonemptyText
    if_true: NonemptyText
    if_false: NonemptyText

    @model_validator(mode="after")
    def different_consequences(self) -> "MissingInformation":
        if self.if_true == self.if_false:
            raise ValueError("explain how the possible answers change the constitutional assessment")
        return self


class ModelOutput(BaseModel):
    engaged_provisions: list[ProvisionAssessment]
    decisive_provision: str | None = Field(description="Exact provision identifier from engaged_provisions that determines the verdict, or null if no single provision is decisive.")
    verdict: Literal["YES", "NO", "ABSTAIN"]
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(default="", max_length=300, description="CIP-136-compatible summary of the verdict and decisive reasoning, at most 300 characters.")
    rationale: str = Field(description="Overall rationale, at most 100 words.")
    argument_responses: list[ArgumentResponse] = Field(default_factory=list)
    strongest_counterargument: str = ""
    revision_reason: str = ""
    missing_information: list[MissingInformation] = Field(default_factory=list)

    @model_validator(mode="after")
    def deliberation_requirements(self, info: ValidationInfo) -> "ModelOutput":
        context = info.context or {}
        if self.verdict == "ABSTAIN" and not self.missing_information:
            raise ValueError("insufficient information requires a specific decisive question and its possible consequences")
        if not context.get("round_name"):
            return self  # Version 1 records predate mandatory argument responses.
        if not self.engaged_provisions:
            raise ValueError("assess at least one constitutional provision and explain its applicability")
        for provision in self.engaged_provisions:
            if not all(value.strip() for value in (
                provision.provision, provision.requirement, provision.reasoning, provision.evidence,
            )):
                raise ValueError("each constitutional argument needs its provision, requirement, reasoning, and supplied evidence")
        assessments = {p.assessment for p in self.engaged_provisions}
        if "violated" in assessments and self.verdict != "NO":
            raise ValueError("an established violation requires NO, even if other facts are missing")
        if self.verdict == "NO" and "violated" not in assessments:
            raise ValueError("NO requires an identified constitutional violation")
        if "cannot_determine" in assessments and not self.missing_information:
            raise ValueError("cannot_determine requires a missing-information question")
        missing_provisions = {item.provision for item in self.missing_information}
        if any(p.assessment == "cannot_determine" and p.provision not in missing_provisions for p in self.engaged_provisions):
            raise ValueError("every indeterminate provision needs a matching missing-information question")
        if self.verdict == "YES" and "cannot_determine" in assessments:
            raise ValueError("unresolved constitutional requirements cannot establish YES")
        if self.verdict == "ABSTAIN" and "cannot_determine" not in assessments:
            raise ValueError("identify the constitutional requirement that cannot be determined")
        required = set(context.get("argument_ids", []))
        actual = [response.argument_id for response in self.argument_responses]
        if len(actual) != len(set(actual)) or set(actual) != required:
            raise ValueError("respond exactly once to every required argument ID, without invented IDs")
        if context["round_name"] != "R0":
            if not self.strongest_counterargument.strip() or not self.revision_reason.strip():
                raise ValueError("explain the strongest counterargument and why your verdict changed or stayed the same")
        return self

    @field_validator("rationale")
    @classmethod
    def rationale_word_limit(cls, v: str) -> str:
        if len(v.split()) > 100:
            raise ValueError(f"rationale exceeds 100 words ({len(v.split())} words)")
        return v


def model_output_json_schema() -> dict:
    """Strict provider schema, generated from the same model used for validation."""
    schema = ModelOutput.model_json_schema()

    def strict(node):
        if isinstance(node, dict):
            node.pop("default", None)
            # pattern is a validation-only constraint: JSON Schema treats it as an
            # unanchored search, but provider-side grammar engines anchor it during
            # generation, turning r"\S" into "exactly one non-whitespace character".
            node.pop("pattern", None)
            if node.get("type") == "object":
                node["additionalProperties"] = False
                node["required"] = list(node.get("properties", {}))
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    strict(schema)
    return schema


class FailureRecord(BaseModel):
    status: Literal["schema_failure", "api_failure", "incomplete_response"]
    raw: str | None = None
    raw_truncated: bool = False
    error: str | None = None


class UsageRecord(BaseModel):
    api_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None
    generation_ids: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    finish_reasons: list[str] = Field(default_factory=list)
    response_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class StageEntry(BaseModel):
    model: str
    seat_index: int | None = None
    persistence_errors: list[str] = Field(default_factory=list)
    started_at: datetime
    completed_at: datetime
    user_message: str
    usage: UsageRecord | None = None
    output: ModelOutput | FailureRecord


def argument_catalog(entries: list[StageEntry]) -> dict[str, ProvisionAssessment]:
    """Stable, anonymous references; identical arguments share one reference."""
    arguments = {}
    for entry in entries:
        if isinstance(entry.output, ModelOutput):
            for argument in entry.output.engaged_provisions:
                key = "arg-" + hashlib.sha256(argument.model_dump_json().encode()).hexdigest()[:16]
                arguments[key] = argument
    return arguments


def outcome_summary(entries: list[StageEntry], verdict: str | None) -> str | None:
    """Use the first final winning seat's summary; seat order is deterministic."""
    return next((
        entry.output.summary for entry in entries
        if isinstance(entry.output, ModelOutput)
        and entry.output.verdict == verdict
        and entry.output.summary
    ), None)


class AggregationResult(BaseModel):
    verdict: Literal["YES", "NO", "ABSTAIN"] | None
    voting_models: int
    aggregation_status: Literal["ok", "failed"]
    review_flags: list[Literal["divided_votes", "missing_information", "unresolved_arguments"]] = Field(default_factory=list)


class ConfigFingerprint(BaseModel):
    ncl: dict[str, int] | None = None
    proposal_sha256: str = ""
    constitution_sha256: str
    system_prompt_sha256: str
    deliberation_template_sha256: str
    system_message_sha256: str
    models: list[dict[str, Any]]
    ballot_categories: list[str]
    aggregation_rule: str
    rounds: int


class CanonicalFile(BaseModel):
    pitkind_version: str = __version__
    schema_version: Literal["1.0", "2.0"]
    proposal: dict[str, Any]
    config_fingerprint: ConfigFingerprint
    timestamps: dict[str, str]
    run_status: Literal["complete", "failed"]
    stages: dict[str, list[StageEntry]]
    aggregations: dict[str, AggregationResult]
    official_verdict: Literal["YES", "NO", "ABSTAIN"] | None
    summary: str | None = Field(default=None, max_length=300)
    review_arguments: dict[str, ProvisionAssessment] = Field(default_factory=dict)

    @model_validator(mode="after")
    def official_verdict_consistent(self) -> "CanonicalFile":
        if self.run_status == "failed":
            if self.official_verdict is not None:
                raise ValueError("official_verdict must be null when run_status is failed")
            return self

        from .aggregate import aggregate

        if self.schema_version == "2.0":
            history = []
            for name in ("R0", "R1", "R2"):
                required = argument_catalog(history)
                for entry in self.stages.get(name, []):
                    if isinstance(entry.output, ModelOutput):
                        ModelOutput.model_validate(entry.output.model_dump(), context={
                            "round_name": name, "argument_ids": list(required),
                        })
                history.extend(self.stages.get(name, []))
            if self.review_arguments != argument_catalog(history):
                raise ValueError("review_arguments must preserve all recorded constitutional arguments")

        rounds = {"R0", "R1", "R2"}
        if self.stages.keys() != rounds or self.aggregations.keys() != rounds:
            raise ValueError("a complete run must include exactly R0, R1, and R2 stages and aggregations")
        models = [m.get("openrouter_id") for m in self.config_fingerprint.models]
        if not models or len(models) % 2 == 0:
            raise ValueError("a complete run must have a nonempty, odd number of model seats")
        for name, entries in self.stages.items():
            if [e.model for e in entries] != models:
                raise ValueError(f"{name} entries must match the configured model seats in order")
            if any(isinstance(e.output, FailureRecord) or e.persistence_errors for e in entries):
                raise ValueError(f"a complete run must not contain failures in {name}")
            excluded = {"review_flags"} if self.schema_version == "1.0" else set()
            if self.aggregations[name].model_dump(exclude=excluded) != aggregate(entries).model_dump(exclude=excluded):
                raise ValueError(f"{name} aggregation must match its stage votes")
        if self.official_verdict != self.aggregations["R2"].verdict:
            raise ValueError("official_verdict must match R2 aggregation verdict")
        expected_summary = outcome_summary(self.stages["R2"], self.official_verdict)
        if expected_summary is not None and self.summary != expected_summary:
            raise ValueError("summary must match the first final winning seat summary")
        return self
