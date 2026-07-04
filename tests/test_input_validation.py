from unittest.mock import AsyncMock

import json
import pytest
from pydantic import ValidationError

from pitkind.config import Config
from tests.test_mock_e2e import _make_workspace, _run


@pytest.mark.parametrize("field", ["max_retries", "max_api_retries"])
def test_retry_limits_are_nonnegative(field):
    data = {"models": [{"openrouter_id": "test"}], "ballot_categories": ["YES", "NO", "ABSTAIN"]}
    assert getattr(Config.model_validate({**data, field: 0}), field) == 0
    with pytest.raises(ValidationError, match=field):
        Config.model_validate({**data, field: -1})


@pytest.mark.parametrize("placeholder", ["proposal", "own_previous_output", "peer_reviews"])
def test_template_missing_required_placeholder_fails_before_model_calls(tmp_path, monkeypatch, placeholder):
    workspace = _make_workspace(tmp_path)
    template_file = workspace / "deliberation_template.txt"
    template_file.write_text(template_file.read_text().replace("{%s}" % placeholder, ""))
    run_r0 = AsyncMock()
    monkeypatch.setattr("pitkind.cli.run_r0", run_r0)
    result = _run(workspace)
    assert result.exit_code == 1
    assert "deliberation template must use the placeholder(s) {%s}" % placeholder in result.output
    run_r0.assert_not_called()
    assert not list((workspace / "output").iterdir())


def test_template_round_placeholder_is_optional(tmp_path):
    workspace = _make_workspace(tmp_path)
    template_file = workspace / "deliberation_template.txt"
    template_file.write_text(template_file.read_text().replace("{round}", ""))
    result = _run(workspace)
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("proposal", [[], None, {"id": 123}, {"id": None}])
def test_invalid_proposal_fails_before_model_calls(tmp_path, monkeypatch, proposal):
    workspace = _make_workspace(tmp_path)
    (workspace / "proposal.json").write_text(json.dumps(proposal))
    run_r0 = AsyncMock()
    monkeypatch.setattr("pitkind.cli.run_r0", run_r0)
    result = _run(workspace)
    assert result.exit_code == 1
    assert "proposal must be a JSON object" in result.output
    run_r0.assert_not_called()
    assert not list((workspace / "output").iterdir())
