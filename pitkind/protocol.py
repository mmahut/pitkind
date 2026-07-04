from __future__ import annotations

import asyncio
import json
import random
from typing import Any, Awaitable, Callable

from .client import call_model
from .config import Config
from .schemas import ModelOutput, StageEntry, argument_catalog, model_output_json_schema

def _build_system_message(system_prompt: str, constitution: str) -> str:
    """Compose the system message sent with every call: operator-supplied system
    prompt, constitution, and the generated output JSON schema.

    The orchestrator adds no judging rules of its own: the system prompt must
    carry the mandatory deliberation rules (argument-catalog duties, ABSTAIN
    discipline, anti-conformity) — inputs/prompts/v0.1.txt is the reference.
    schemas.ModelOutput enforces the same rules at validation time, so a prompt
    that omits them surfaces as schema failures and, once retries are exhausted,
    a failed run.
    """
    return (
        f"{system_prompt}\n\n--- BEGIN CONSTITUTION ---\n{constitution}"
        f"\n--- END CONSTITUTION ---\n\n--- OUTPUT SCHEMA ---\n"
        f"{json.dumps(model_output_json_schema())}"
    )


def _reviewer_label(index: int) -> str:
    """Return spreadsheet-style reviewer labels: A..Z, AA..AZ, and so on."""
    label = ""
    while True:
        index, remainder = divmod(index, 26)
        label = chr(ord("A") + remainder) + label
        if index == 0:
            return label
        index -= 1


def _render_peer_block(peer_outputs: list[ModelOutput], rng: random.Random) -> str:
    shuffled = peer_outputs.copy()
    rng.shuffle(shuffled)
    parts = []
    for label_idx, output in enumerate(shuffled):
        label = f"Reviewer {_reviewer_label(label_idx)}"
        parts.append(f"--- {label} ---\n{json.dumps(output.model_dump(), indent=2)}")
    return "\n\n".join(parts)


def _render_deliberation_template(
    template: str,
    proposal: dict[str, Any],
    own_previous_output: ModelOutput,
    peer_reviews: str,
    round_name: str,
) -> str:
    return template.format_map({
        "proposal": json.dumps(proposal, indent=2),
        "own_previous_output": json.dumps(own_previous_output.model_dump(), indent=2),
        "peer_reviews": peer_reviews,
        "round": round_name,
    })


async def _reused(entry: StageEntry, seat_index: int,
                  on_complete: Callable[[StageEntry], Awaitable[None]] | None) -> StageEntry:
    """Return a checkpointed entry as-is, re-persisting it into the new run."""
    entry.seat_index = seat_index
    if on_complete is not None:
        await on_complete(entry)
    return entry


async def run_r0(
    proposal: dict[str, Any],
    system_message: str,
    config: Config,
    mock: bool,
    mock_call_counts: dict,
    on_complete: Callable[[StageEntry], Awaitable[None]] | None = None,
    cached: dict[str, StageEntry] | None = None,
) -> list[StageEntry]:
    user_message = "Current round: R0. Proposal and frozen evidence:\n" + json.dumps(proposal, indent=2)
    tasks = [
        _reused(cached[m.openrouter_id], i, on_complete)
        if cached and m.openrouter_id in cached
        else call_model(
            model_id=m.openrouter_id,
            provider=m.provider,
            model_index=i,
            system_message=system_message,
            user_message=user_message,
            decoding_params=m.decoding_params,
            max_retries=config.max_retries,
            max_api_retries=config.max_api_retries,
            mock=mock,
            _mock_call_count=mock_call_counts,
            validation_context={"round_name": "R0", "argument_ids": []},
            on_complete=on_complete,
        )
        for i, m in enumerate(config.models)
    ]
    return list(await asyncio.gather(*tasks))


async def run_deliberation_round(
    round_name: str,
    previous_entries: list[StageEntry],
    proposal: dict[str, Any],
    system_message: str,
    deliberation_template: str,
    config: Config,
    mock: bool,
    mock_call_counts: dict,
    earlier_entries: list[StageEntry] | None = None,
    on_complete: Callable[[StageEntry], Awaitable[None]] | None = None,
    cached: dict[str, StageEntry] | None = None,
) -> list[StageEntry]:
    catalog = argument_catalog(earlier_entries if earlier_entries is not None else previous_entries)
    all_outputs: list[ModelOutput] = []
    for e in previous_entries:
        if not isinstance(e.output, ModelOutput):
            raise ValueError(
                f"cannot run {round_name}: previous round output for {e.model} is a failure"
            )
        all_outputs.append(e.output)

    tasks = []
    for focal_idx, m in enumerate(config.models):
        if cached and m.openrouter_id in cached:
            tasks.append(_reused(cached[m.openrouter_id], focal_idx, on_complete))
            continue
        focal_prev = all_outputs[focal_idx]
        peers = all_outputs[:focal_idx] + all_outputs[focal_idx + 1:]
        rng = random.Random(f"{round_name}:{focal_idx}:{json.dumps(proposal, sort_keys=True)}")
        peer_block = _render_peer_block(peers, rng)

        user_message = _render_deliberation_template(
            template=deliberation_template,
            proposal=proposal,
            own_previous_output=focal_prev,
            peer_reviews=peer_block,
            round_name=round_name,
        )
        user_message += f"\n\nCurrent round: {round_name}. Required argument catalog (all earlier rounds):\n" + json.dumps(
            {key: argument.model_dump() for key, argument in catalog.items()}, indent=2,
        )

        tasks.append(
            call_model(
                model_id=m.openrouter_id,
                provider=m.provider,
                model_index=focal_idx,
                system_message=system_message,
                user_message=user_message,
                decoding_params=m.decoding_params,
                max_retries=config.max_retries,
                max_api_retries=config.max_api_retries,
                mock=mock,
                _mock_call_count=mock_call_counts,
                validation_context={"round_name": round_name, "argument_ids": list(catalog)},
                on_complete=on_complete,
            )
        )

    return list(await asyncio.gather(*tasks))
