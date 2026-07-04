import json

import pytest
from pydantic import ValidationError

from pitkind.schemas import CanonicalFile
from tests.test_mock_e2e import _make_workspace, _run


@pytest.mark.parametrize("path,value", [
    (("stages",), {}),
    (("aggregations",), {}),
    (("stages", "R1"), []),
    (("stages", "R1", 0, "model"), "wrong-seat"),
    (("stages", "R1", 0, "output"), {"status": "api_failure", "error": "timeout"}),
    (("aggregations", "R2", "aggregation_status"), "failed"),
    (("aggregations", "R0", "verdict"), "NO"),
    (("aggregations", "R0", "voting_models"), 4),
    (("official_verdict",), None),
    (("official_verdict",), "NO"),
    (("official_verdict",), "ABSTAIN"),
    (("config_fingerprint", "models"), []),
])
def test_reject_inconsistent_complete_record(tmp_path, path, value):
    workspace = _make_workspace(tmp_path)
    result = _run(workspace)
    assert result.exit_code == 0, result.output
    data = json.loads(next((workspace / "output").glob("*.json")).read_text())
    CanonicalFile.model_validate(data)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        CanonicalFile.model_validate(data)
