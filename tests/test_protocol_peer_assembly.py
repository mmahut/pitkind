import json
import random
from datetime import datetime, timezone

import pytest

from pitkind.protocol import _build_system_message, _render_peer_block, _render_deliberation_template, _reviewer_label
from pitkind.schemas import ModelOutput, StageEntry, FailureRecord

_NOW = datetime.now(timezone.utc)


def test_system_message_keeps_judging_rules_in_supplied_prompt():
    message = _build_system_message("Custom judging instructions.", "Article I: Example.")
    framing, schema_text = message.split("--- OUTPUT SCHEMA ---\n")
    assert framing == (
        "Custom judging instructions.\n\n--- BEGIN CONSTITUTION ---\n"
        "Article I: Example.\n--- END CONSTITUTION ---\n\n"
    )
    properties = json.loads(schema_text)["properties"]
    assert "at most 100 words" in properties["rationale"]["description"]
    assert "null if no single provision is decisive" in properties["decisive_provision"]["description"]


def _make_output(verdict: str = "YES") -> ModelOutput:
    return ModelOutput(
        engaged_provisions=[],
        decisive_provision=None,
        verdict=verdict,
        confidence="high",
        rationale="Short rationale.",
    )


def _make_valid_peers(n: int) -> list[ModelOutput]:
    return [_make_output() for _ in range(n)]


def test_peer_block_excludes_focal():
    all_peers = _make_valid_peers(5)
    focal_idx = 2
    peers_for_focal = [o for i, o in enumerate(all_peers) if i != focal_idx]
    rng = random.Random(42)
    block = _render_peer_block(peers_for_focal, rng)
    assert len(peers_for_focal) == 4
    assert "Reviewer A" in block
    assert "Reviewer D" in block
    assert "Reviewer E" not in block


def test_peer_block_count():
    n = 5
    all_peers = _make_valid_peers(n)
    for focal_idx in range(n):
        peers = [o for i, o in enumerate(all_peers) if i != focal_idx]
        rng = random.Random(focal_idx)
        block = _render_peer_block(peers, rng)
        labels = [f"Reviewer {chr(65 + j)}" for j in range(n - 1)]
        for label in labels:
            assert label in block


def test_reviewer_labels_support_more_than_26_peers():
    assert _reviewer_label(0) == "A"
    assert _reviewer_label(25) == "Z"
    assert _reviewer_label(26) == "AA"
    assert _reviewer_label(51) == "AZ"


def test_peer_block_no_model_ids():
    peers = _make_valid_peers(4)
    rng = random.Random(99)
    block = _render_peer_block(peers, rng)
    assert "openrouter" not in block
    assert "anthropic" not in block
    assert "gpt" not in block


def test_peer_block_labels_randomized_across_calls():
    peers = [
        _make_output("YES"),
        _make_output("NO"),
        _make_output("YES"),
        _make_output("NO"),
    ]
    # All outputs alternate YES/NO — different orderings produce different blocks
    blocks = set()
    for _ in range(20):
        rng = random.Random(random.randbytes(8))
        block = _render_peer_block(peers, rng)
        blocks.add(block)
    assert len(blocks) > 1, "Peer blocks should not all be identical (labels should be randomized)"


def test_deliberation_template_substitution():
    template = "Round: {round}\nProposal: {proposal}\nMine: {own_previous_output}\nPeers: {peer_reviews}"
    proposal = {"id": "prop-1", "title": "Test", "type": "treasury", "body": "Details."}
    own = _make_output()
    peers_block = "--- Reviewer A ---\n{}"

    result = _render_deliberation_template(
        template=template,
        proposal=proposal,
        own_previous_output=own,
        peer_reviews=peers_block,
        round_name="R1",
    )
    assert "R1" in result
    assert "prop-1" in result
    assert peers_block in result
    assert json.dumps(own.model_dump(), indent=2) in result


def test_deliberation_template_r2():
    template = "Round: {round}"
    result = _render_deliberation_template(
        template=template,
        proposal={},
        own_previous_output=_make_output(),
        peer_reviews="",
        round_name="R2",
    )
    assert "R2" in result
