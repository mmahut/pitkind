import pytest
from pydantic import ValidationError

from pitkind.schemas import AggregationResult, FailureRecord, ModelOutput, ProvisionAssessment


def _valid_output(**overrides) -> dict:
    base = {
        "engaged_provisions": [],
        "decisive_provision": None,
        "verdict": "YES",
        "confidence": "high",
        "rationale": "Short rationale within the limit.",
    }
    base.update(overrides)
    return base


def test_valid_model_output():
    out = ModelOutput.model_validate(_valid_output())
    assert out.verdict == "YES"
    assert out.engaged_provisions == []


def test_verdicts_must_be_supported():
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(_valid_output(verdict="MAYBE"))
    with pytest.raises(ValidationError):
        AggregationResult(verdict="MAYBE", voting_models=1, aggregation_status="ok")


def test_rationale_exactly_100_words():
    rationale = " ".join(["word"] * 100)
    out = ModelOutput.model_validate(_valid_output(rationale=rationale))
    assert out.rationale == rationale


def test_rationale_101_words_fails():
    rationale = " ".join(["word"] * 101)
    with pytest.raises(ValidationError, match="rationale exceeds 100 words"):
        ModelOutput.model_validate(_valid_output(rationale=rationale))


def test_summary_is_limited_to_300_characters():
    assert len(ModelOutput.model_validate(_valid_output(summary="x" * 300)).summary) == 300
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(_valid_output(summary="x" * 301))


def test_unknown_confidence_fails():
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(_valid_output(confidence="very_high"))


def test_wrong_assessment_enum_fails():
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(_valid_output(
            engaged_provisions=[{
                "provision": "Art I",
                "requirement": "req",
                "assessment": "maybe",
                "reasoning": "unclear",
            }]
        ))


def test_missing_required_field_fails():
    data = _valid_output()
    del data["verdict"]
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(data)


def test_failure_record_schema_failure():
    rec = FailureRecord(status="schema_failure", raw="bad", raw_truncated=True)
    assert rec.raw_truncated is True


def test_failure_record_api_failure():
    rec = FailureRecord(status="api_failure", error="timeout")
    assert rec.error == "timeout"
    assert rec.raw is None


def test_decisive_provision_nullable():
    out = ModelOutput.model_validate(_valid_output(decisive_provision=None))
    assert out.decisive_provision is None


def test_provision_assessment_valid():
    pa = ProvisionAssessment(
        provision="Art I §1",
        requirement="Must do X",
        assessment="satisfied",
        reasoning="Because Y.",
    )
    assert pa.assessment == "satisfied"
