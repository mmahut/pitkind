import json
import random

from pitkind.config import Config
from pitkind.protocol import _render_peer_block, run_deliberation_round
from tests.test_aggregate import _entry


async def test_each_seat_receives_only_its_peers(monkeypatch):
    # Repeated model IDs still represent separate seats.
    entries = [_entry("same-model", "YES") for _ in range(3)]
    for i, entry in enumerate(entries):
        entry.output.rationale = f"Seat {i} reasoning"
    config = Config(models=[{"openrouter_id": e.model} for e in entries], ballot_categories=["YES", "NO", "ABSTAIN"])
    seen = []

    async def capture(**kwargs):
        i = kwargs["model_index"]
        own, peers = kwargs["user_message"].split("\nPEERS\n")
        assert json.loads(own) == entries[i].output.model_dump()
        for j, entry in enumerate(entries):
            assert (entry.output.rationale in peers) == (i != j)
        seen.append(i)
        return entries[i]

    monkeypatch.setattr("pitkind.protocol.call_model", capture)
    result = await run_deliberation_round(
        "R1", entries, {}, "system", "{own_previous_output}\nPEERS\n{peer_reviews}", config, True, {},
    )
    assert result == entries
    assert sorted(seen) == [0, 1, 2]


def test_shuffling_preserves_source_order():
    outputs = [_entry("seat", "YES").output for _ in range(4)]
    for i, output in enumerate(outputs):
        output.rationale = f"Seat {i} reasoning"
    original = outputs.copy()
    first = _render_peer_block(outputs, random.Random(1))
    second = _render_peer_block(outputs, random.Random(2))
    assert outputs == original
    assert first != second
    assert all(first.count(output.rationale) == 1 for output in outputs)
