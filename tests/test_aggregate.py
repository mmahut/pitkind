from datetime import datetime, timezone

import pytest

from pitkind.aggregate import aggregate
from pitkind.schemas import FailureRecord, ModelOutput, StageEntry

_NOW = datetime.now(timezone.utc)


def _entry(model: str, verdict: str) -> StageEntry:
    return StageEntry(
        model=model,
        started_at=_NOW,
        completed_at=_NOW,
        user_message="proposal text",
        output=ModelOutput(
            engaged_provisions=[],
            decisive_provision=None,
            verdict=verdict,
            confidence="high",
            rationale="Short rationale.",
        ),
    )


def _failure(model: str) -> StageEntry:
    return StageEntry(
        model=model,
        started_at=_NOW,
        completed_at=_NOW,
        user_message="proposal text",
        output=FailureRecord(status="schema_failure", raw="bad json"),
    )


def test_plurality_winner():
    entries = [_entry("m1", "YES"), _entry("m2", "YES"), _entry("m3", "NO"), _entry("m4", "YES"), _entry("m5", "NO")]
    result = aggregate(entries)
    assert result.verdict == "YES"
    assert result.voting_models == 5
    assert result.aggregation_status == "ok"


def test_plurality_no_wins():
    entries = [_entry("m1", "NO"), _entry("m2", "NO"), _entry("m3", "YES"), _entry("m4", "NO"), _entry("m5", "YES")]
    result = aggregate(entries)
    assert result.verdict == "NO"


def test_unanimous():
    entries = [_entry(f"m{i}", "YES") for i in range(5)]
    result = aggregate(entries)
    assert result.verdict == "YES"
    assert result.voting_models == 5


def test_single_failure_fails_stage():
    entries = [_entry("m1", "YES"), _entry("m2", "YES"), _entry("m3", "YES"), _entry("m4", "YES"), _failure("m5")]
    result = aggregate(entries)
    assert result.verdict is None
    assert result.aggregation_status == "failed"
    assert result.voting_models == 4


def test_api_failure_fails_stage():
    entries = [_entry("m1", "YES"), _entry("m2", "NO"), _entry("m3", "YES"), _entry("m4", "NO")]
    entries.append(StageEntry(
        model="m5",
        started_at=_NOW,
        completed_at=_NOW,
        user_message="proposal text",
        output=FailureRecord(status="api_failure", error="rate limited"),
    ))
    result = aggregate(entries)
    assert result.verdict is None
    assert result.aggregation_status == "failed"


def test_all_failures():
    entries = [_failure(f"m{i}") for i in range(5)]
    result = aggregate(entries)
    assert result.verdict is None
    assert result.aggregation_status == "failed"
    assert result.voting_models == 0
