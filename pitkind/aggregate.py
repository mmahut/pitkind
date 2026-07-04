from __future__ import annotations

from collections import Counter

from .schemas import AggregationResult, ModelOutput, StageEntry


def aggregate(stage_entries: list[StageEntry]) -> AggregationResult:
    """Require a binary majority of all seats; missing evidence is not a NO."""
    valid = [e for e in stage_entries if isinstance(e.output, ModelOutput)]

    if not stage_entries or len(valid) < len(stage_entries) or any(e.persistence_errors for e in stage_entries):
        return AggregationResult(
            verdict=None,
            voting_models=len(valid),
            aggregation_status="failed",
        )

    counts = Counter(e.output.verdict for e in valid)
    winner = next((verdict for verdict in ("YES", "NO") if counts[verdict] > len(valid) / 2), "ABSTAIN")
    flags = []
    if counts["YES"] and counts["NO"]:
        flags.append("divided_votes")
    if counts["ABSTAIN"] or any(e.output.missing_information for e in valid):
        flags.append("missing_information")
    if any(r.assessment == "unresolved" for e in valid for r in e.output.argument_responses):
        flags.append("unresolved_arguments")

    return AggregationResult(
        verdict=winner,
        voting_models=len(valid),
        aggregation_status="ok",
        review_flags=flags,
    )
