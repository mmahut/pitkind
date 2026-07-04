import asyncio
import json

import pytest

from pitkind.schemas import FailureRecord
from tests.test_mock_e2e import _make_workspace, _run


def test_checkpoint_error_preserves_results_and_stops_rounds(tmp_path, monkeypatch):
    from pitkind import cli
    original = cli.atomic_write_json

    def write(path, data):
        if path.parent.name.endswith('.seats'):
            raise OSError('checkpoint unavailable')
        original(path, data)

    monkeypatch.setattr(cli, 'atomic_write_json', write)
    workspace = _make_workspace(tmp_path)
    result = _run(workspace)
    assert result.exit_code == 1
    data = json.loads(next((workspace / 'output').glob('*.json')).read_text())
    assert list(data['stages']) == ['R0']
    assert data['official_verdict'] is None
    assert len(data['stages']['R0']) == 5
    assert all(e['persistence_errors'] for e in data['stages']['R0'])
    assert all('verdict' in e['output'] for e in data['stages']['R0'])


async def test_colliding_model_names_keep_separate_seats(tmp_path):
    from pitkind.cli import _run_stages
    from pitkind.config import Config
    config = Config(models=[{'openrouter_id': s} for s in ['a/b', 'a_b', 'a/b']],
                    ballot_categories=['YES', 'NO', 'ABSTAIN'])
    stages, _ = await _run_stages({}, 'system', '{round}', config, True,
                                 tmp_path / 'run.json.events.jsonl')
    files = list((tmp_path / 'run.json.seats').glob('*.json'))
    assert len(files) == 9
    assert {json.loads(p.read_text())['seat_index'] for p in files} == {0, 1, 2}


@pytest.mark.parametrize("failed_round", ["R0", "R1", "R2", None])
def test_stages_share_one_loop_and_stop_on_failure(tmp_path, monkeypatch, failed_round):
    from pitkind.protocol import call_model

    loops = []

    async def failing_call(**kwargs):
        loops.append(asyncio.get_running_loop())
        entry = await call_model(**kwargs)
        round_name = next((r for r in ("R1", "R2") if f"Round: {r}" in kwargs["user_message"]), "R0")
        if round_name == failed_round and kwargs["model_index"] == 0:
            entry.output = FailureRecord(status="api_failure", error="test failure")
        return entry

    monkeypatch.setattr("pitkind.protocol.call_model", failing_call)
    workspace = _make_workspace(tmp_path)
    result = _run(workspace)
    assert result.exit_code == (1 if failed_round else 0), result.output
    outputs = list((workspace / "output").glob("*.json"))
    assert len(outputs) == 1
    data = json.loads(outputs[0].read_text())
    rounds = ["R0", "R1", "R2"]
    expected = rounds[:rounds.index(failed_round) + 1] if failed_round else rounds
    assert list(data["stages"]) == list(data["aggregations"]) == expected
    assert data["run_status"] == ("failed" if failed_round else "complete")
    assert data["official_verdict"] == (None if failed_round else "YES")
    assert len(loops) == 5 * len(expected)
    assert all(loop is loops[0] for loop in loops)
