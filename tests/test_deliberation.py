import json
import hashlib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from pitkind.aggregate import aggregate
from pitkind.client import call_model
from pitkind.mock import get_stub_response
from pitkind.report import render_report
from pitkind.schemas import CanonicalFile, ModelOutput, argument_catalog
from tests.test_aggregate import _entry, _failure
from tests.test_mock_e2e import _make_workspace, _run


def _abstention():
    data = json.loads(get_stub_response(0, 1))
    data["verdict"] = "ABSTAIN"
    data["engaged_provisions"][0]["assessment"] = "cannot_determine"
    data["engaged_provisions"][0]["reasoning"] = "The supplied evidence does not establish the required registration state."
    data["missing_information"] = [{
        "provision": "Article I §3",
        "question": "Was the supplied recipient address registered at the proposal's referenced epoch?",
        "source": "chain_snapshot", "why_decisive": "The applicable requirement depends on this state.",
        "if_true": "This requirement is satisfied; assess the remaining requirements for YES.",
        "if_false": "This requirement is violated, requiring NO.",
    }]
    return data


def test_abstention_requires_a_decisive_fact_and_cannot_override_a_violation():
    data = _abstention()
    context = {"round_name": "R0", "argument_ids": []}
    assert ModelOutput.model_validate(data, context=context).verdict == "ABSTAIN"
    missing = deepcopy(data)
    missing["missing_information"] = []
    with pytest.raises(ValidationError, match="specific decisive question"):
        ModelOutput.model_validate(missing, context=context)
    data["engaged_provisions"][0]["assessment"] = "violated"
    with pytest.raises(ValidationError, match="established violation requires NO"):
        ModelOutput.model_validate(data, context=context)
    data["verdict"] = "NO"
    assert ModelOutput.model_validate(data, context=context).verdict == "NO"


@pytest.mark.parametrize("verdicts,expected", [
    (["YES", "YES", "ABSTAIN"], "YES"),
    (["NO", "NO", "ABSTAIN"], "NO"),
    (["YES", "NO", "ABSTAIN"], "ABSTAIN"),
    (["YES", "YES", "NO", "ABSTAIN", "ABSTAIN"], "ABSTAIN"),
    (["ABSTAIN", "ABSTAIN", "ABSTAIN"], "ABSTAIN"),
])
def test_abstention_does_not_shrink_the_majority_denominator(verdicts, expected):
    entries = [_entry(f"seat-{i}", "YES" if verdict == "ABSTAIN" else verdict) for i, verdict in enumerate(verdicts)]
    for entry, verdict in zip(entries, verdicts):
        if verdict == "ABSTAIN":
            entry.output = ModelOutput.model_validate(_abstention())
    result = aggregate(entries)
    assert result.verdict == expected
    assert result.aggregation_status == "ok"
    assert result.voting_models == len(entries)
    assert "missing_information" in result.review_flags
    assert aggregate([*entries, _failure("broken")]).aggregation_status == "failed"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "invented", "no_revision", "no_counterargument", "empty_evidence"])
def test_every_argument_requires_an_explicit_response(mutation):
    context = {"round_name": "R1", "argument_ids": ["argument-one", "argument-two"]}
    data = json.loads(get_stub_response(0, 2, context))
    ModelOutput.model_validate(data, context=context)
    if mutation == "missing":
        data["argument_responses"].pop()
    elif mutation == "duplicate":
        data["argument_responses"].append(data["argument_responses"][0])
    elif mutation == "invented":
        data["argument_responses"][0]["argument_id"] = "invented"
    elif mutation == "no_revision":
        data["revision_reason"] = " "
    elif mutation == "no_counterargument":
        data["strongest_counterargument"] = " "
    else:
        data["argument_responses"][0]["evidence"] = " "
    with pytest.raises(ValidationError):
        ModelOutput.model_validate(data, context=context)


async def test_omitting_a_required_response_uses_schema_retry(monkeypatch):
    context = {"round_name": "R1", "argument_ids": ["argument-one"]}
    responses = iter([get_stub_response(0, 1), get_stub_response(0, 2, context)])
    counts = {}
    monkeypatch.setattr("pitkind.client.get_stub_response", lambda *args: next(responses))
    entry = await call_model("test", 0, "system", "user", {}, 1, 0, True, counts, context)
    assert isinstance(entry.output, ModelOutput)
    assert counts[0] == 2


def test_r0_arguments_survive_into_r2_and_are_validated_on_reload(tmp_path):
    workspace = _make_workspace(tmp_path)
    result = _run(workspace)
    assert result.exit_code == 0, result.output
    data = json.loads(next((workspace / "output").glob("*.json")).read_text())
    record = CanonicalFile.model_validate(data)
    r0_ids = set(argument_catalog(record.stages["R0"]))
    assert r0_ids
    for name in ("R1", "R2"):
        for entry in record.stages[name]:
            assert r0_ids <= {r.argument_id for r in entry.output.argument_responses}
            assert all(key in entry.user_message for key in r0_ids)
    assert r0_ids <= record.review_arguments.keys()
    data["stages"]["R2"][0]["output"]["argument_responses"].pop()
    with pytest.raises(ValidationError, match="every required argument ID"):
        CanonicalFile.model_validate(data)


def test_changed_arguments_do_not_erase_r0_objections(tmp_path, monkeypatch):
    def changing_stub(index, count, context=None):
        data = json.loads(get_stub_response(index, count, context)) if not (index == 3 and count == 1) else None
        if data is None:
            return get_stub_response(index, count, context)
        if context["round_name"] == "R1":
            for provision in data["engaged_provisions"]:
                provision["reasoning"] += " Revised argument after discussion."
        return json.dumps(data)

    monkeypatch.setattr("pitkind.client.get_stub_response", changing_stub)
    workspace = _make_workspace(tmp_path)
    assert _run(workspace).exit_code == 0
    record = CanonicalFile.model_validate_json(next((workspace / "output").glob("*.json")).read_text())
    r0_ids = set(argument_catalog(record.stages["R0"]))
    r1_ids = set(argument_catalog(record.stages["R1"]))
    assert r0_ids.isdisjoint(r1_ids)
    for entry in record.stages["R2"]:
        assert {r.argument_id for r in entry.output.argument_responses} == r0_ids | r1_ids


def test_frozen_snapshot_produces_identical_prompts(tmp_path):
    workspace = _make_workspace(tmp_path)
    snapshot_path = workspace / "proposal.json"
    proposal = json.loads(snapshot_path.read_text())
    proposal["evidence"] = {"block_hash": "test-block", "recipient_registered": True}
    snapshot_path.write_text(json.dumps(proposal))
    for _ in range(2):
        assert _run(workspace).exit_code == 0
    records = [CanonicalFile.model_validate_json(path.read_text()) for path in (workspace / "output").glob("*.json")]
    assert len(records) == 2
    for record in records:
        assert record.config_fingerprint.proposal_sha256 == hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
    for name in ("R0", "R1", "R2"):
        assert [e.user_message for e in records[0].stages[name]] == [e.user_message for e in records[1].stages[name]]
        assert all('"test-block"' in e.user_message for e in records[0].stages[name])


def test_autonomous_abstention_completes_and_writes_a_ballot(tmp_path, monkeypatch):
    def abstain_stub(index, count, context=None):
        data = _abstention()
        data["argument_responses"] = json.loads(get_stub_response(0, count, context))["argument_responses"]
        return json.dumps(data)

    monkeypatch.setattr("pitkind.client.get_stub_response", abstain_stub)
    workspace = _make_workspace(tmp_path)
    result = _run(workspace)
    assert result.exit_code == 0, result.output
    assert "Committee ballot: ABSTAIN" in result.output
    record = CanonicalFile.model_validate_json(next((workspace / "output").glob("*.json")).read_text())
    assert record.run_status == "complete"
    assert record.official_verdict == "ABSTAIN"
    assert list(record.stages) == ["R0", "R1", "R2"]
    assert record.stages["R2"][0].output.missing_information[0].source == "chain_snapshot"
    html = render_report(record.model_dump(mode="json"))
    payload = json.loads(html.split('<script type="application/json" id="data">', 1)[1].split("</script>", 1)[0])
    assert payload["official_verdict"] == "ABSTAIN"
    assert payload["stages"]["R2"][0]["missing_information"]
    assert payload["stages"]["R2"][0]["argument_responses"]
    assert payload["review_arguments"]
    assert payload["aggregations"]["R2"]["review_flags"] == ["missing_information", "unresolved_arguments"]


def test_legacy_records_remain_readable(tmp_path):
    workspace = _make_workspace(tmp_path)
    assert _run(workspace).exit_code == 0
    data = json.loads(next((workspace / "output").glob("*.json")).read_text())
    data["schema_version"] = "1.0"
    data.pop("review_arguments")
    data["config_fingerprint"].pop("proposal_sha256")
    data["config_fingerprint"]["aggregation_rule"] = "plurality"
    data["config_fingerprint"]["ballot_categories"] = ["YES", "NO"]
    for entries in data["stages"].values():
        for entry in entries:
            for key in ("argument_responses", "strongest_counterargument", "revision_reason", "missing_information"):
                entry["output"].pop(key)
            for provision in entry["output"]["engaged_provisions"]:
                provision.pop("evidence")
    for aggregation in data["aggregations"].values():
        aggregation.pop("review_flags")
    assert CanonicalFile.model_validate(data).official_verdict == "YES"
    assert "Committee ballot" in render_report(data)
