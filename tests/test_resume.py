import json

import pytest

from pitkind.cli import _load_resume_entries
from pitkind.config import Config
from pitkind.mock import get_stub_response
from pitkind.protocol import run_r0
from pitkind.schemas import ModelOutput, StageEntry
from pitkind.io_paths import utc_now


def _entry(model, output):
    now = utc_now()
    return StageEntry(model=model, seat_index=0, started_at=now, completed_at=now,
                      user_message="msg", output=output)


def _valid_output():
    return ModelOutput.model_validate_json(get_stub_response(0, 1))


def test_load_resume_entries_keeps_only_valid_outputs(tmp_path):
    events = tmp_path / "run.events.jsonl"
    good = _entry("model-a", _valid_output())
    failed = _entry("model-b", {"status": "api_failure", "error": "boom"})
    lines = [
        json.dumps({"config_fingerprint": {"system_message_sha256": "abc"}}),
        json.dumps({"stage": "R0", **good.model_dump(mode="json")}),
        json.dumps({"stage": "R0", **failed.model_dump(mode="json")}),
        json.dumps({"stage": "R1", **good.model_dump(mode="json")}),
        "not json at all"[:0],  # blank line
    ]
    events.write_text("\n".join(lines) + "\n")

    cache, fingerprint = _load_resume_entries(events)
    assert set(cache) == {"R0", "R1"}
    assert set(cache["R0"]) == {"model-a"}
    assert isinstance(cache["R0"]["model-a"].output, ModelOutput)
    assert fingerprint == {"system_message_sha256": "abc"}


def test_load_resume_entries_without_fingerprint_header(tmp_path):
    events = tmp_path / "run.events.jsonl"
    good = _entry("model-a", _valid_output())
    events.write_text(json.dumps({"stage": "R0", **good.model_dump(mode="json")}) + "\n")
    cache, fingerprint = _load_resume_entries(events)
    assert set(cache["R0"]) == {"model-a"}
    assert fingerprint is None


async def test_run_r0_reuses_cached_seats(monkeypatch):
    config = Config.model_validate({
        "models": [{"openrouter_id": m} for m in ("model-a", "model-b", "model-c")],
        "ballot_categories": ["YES", "NO", "ABSTAIN"],
    })
    cached_entry = _entry("model-b", _valid_output())
    called = []

    async def fake_call_model(**kwargs):
        called.append(kwargs["model_id"])
        entry = _entry(kwargs["model_id"], _valid_output())
        if kwargs.get("on_complete") is not None:
            await kwargs["on_complete"](entry)
        return entry

    monkeypatch.setattr("pitkind.protocol.call_model", fake_call_model)
    persisted = []

    async def persist(entry):
        persisted.append(entry)

    entries = await run_r0({}, "system", config, False, {}, persist,
                           cached={"model-b": cached_entry})
    assert called == ["model-a", "model-c"]
    assert entries[1] is cached_entry
    assert entries[1].seat_index == 1
    assert cached_entry in persisted and len(persisted) == 3
