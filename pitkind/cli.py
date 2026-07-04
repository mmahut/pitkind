from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Optional

import typer

from .aggregate import aggregate
from . import __version__
from .config import Config, load_config
from .io_paths import atomic_write_bytes, atomic_write_json, output_filename, sha256_file, utc_now
from .protocol import run_deliberation_round, run_r0, _build_system_message
from .schemas import AggregationResult, CanonicalFile, ConfigFingerprint, ModelOutput, StageEntry, argument_catalog, outcome_summary

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """pitkind: LLM committee deliberation for Cardano governance proposals."""


def _validate_template(template: str) -> None:
    # {round} is optional: protocol.py appends the round name to every deliberation
    # message, but the other placeholders are the model's only source of that context.
    required = ("proposal", "own_previous_output", "peer_reviews")
    sentinels = {name: f"\x00{name}\x00" for name in (*required, "round")}
    try:
        rendered = template.format_map(sentinels)
    except (KeyError, ValueError, IndexError) as exc:
        typer.echo(
            f"Error: deliberation template is invalid (unknown placeholder or stray brace): {exc!r}",
            err=True,
        )
        raise typer.Exit(code=1)
    missing = [name for name in required if sentinels[name] not in rendered]
    if missing:
        typer.echo(
            "Error: deliberation template must use the placeholder(s) "
            + ", ".join("{" + name + "}" for name in missing)
            + "; deliberation rounds would otherwise silently lose that context.",
            err=True,
        )
        raise typer.Exit(code=1)


def _omit_duplicate_constitution(proposal_data: dict[str, Any], constitution_text: str) -> None:
    """Drop inlined document content that duplicates the supplied constitution.

    The constitution already reaches every seat via the system message;
    repeating it inside the proposal JSON roughly doubles that cost per call.
    """
    digest = hashlib.sha256(constitution_text.encode()).hexdigest()
    evidence = proposal_data.get("evidence")
    documents = evidence.get("documents") if isinstance(evidence, dict) else None
    for document in documents if isinstance(documents, list) else []:
        if isinstance(document, dict) and document.get("sha256") == digest and "content" in document:
            del document["content"]
            document["content_note"] = (
                "content is byte-identical to the constitution supplied with this review; "
                "omitted here to avoid duplication"
            )


def _load_resume_entries(path: Path) -> tuple[dict[str, dict[str, StageEntry]], dict[str, Any] | None]:
    """Read an events checkpoint; keep only seats with schema-valid output.

    Also returns the checkpoint's config fingerprint header when present, so
    the caller can refuse to reuse seats deliberated under different inputs.
    """
    import pydantic

    cache: dict[str, dict[str, StageEntry]] = {}
    fingerprint: dict[str, Any] | None = None
    with path.open() as stream:
        for line in stream:
            if not line.strip():
                continue
            data = json.loads(line)
            if "config_fingerprint" in data:
                fingerprint = data["config_fingerprint"]
                continue
            stage = data.pop("stage", None)
            try:
                entry = StageEntry.model_validate(data)
            except pydantic.ValidationError:
                continue
            if stage and isinstance(entry.output, ModelOutput):
                cache.setdefault(stage, {})[entry.model] = entry
    return cache, fingerprint


async def _run_stages(
    proposal: dict[str, Any],
    system_message: str,
    deliberation_template: str,
    config: Config,
    mock: bool,
    checkpoint_path: Path | None = None,
    resume: dict[str, dict[str, StageEntry]] | None = None,
) -> tuple[dict[str, list[StageEntry]], dict[str, AggregationResult]]:
    stages: dict[str, list[StageEntry]] = {}
    aggregations: dict[str, AggregationResult] = {}
    mock_call_counts: dict = {}
    checkpoint_lock = asyncio.Lock()

    async def checkpoint(round_name: str, entry: StageEntry) -> None:
        if checkpoint_path is None:
            return
        async with checkpoint_lock:
            base_name = checkpoint_path.name.removesuffix(".events.jsonl")
            seat_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", entry.model)
            atomic_write_json(
                checkpoint_path.parent / f"{base_name}.seats" / f"{round_name}__{entry.seat_index}__{seat_name}.json",
                {"stage": round_name, **entry.model_dump(mode="json")},
            )
            with checkpoint_path.open("a") as stream:
                stream.write(json.dumps({"stage": round_name, **entry.model_dump(mode="json")}, separators=(",", ":")) + "\n")
                stream.flush()

    async def persist(entry: StageEntry) -> None:
        try:
            await checkpoint(round_name, entry)
        except OSError as exc:
            entry.persistence_errors.append(str(exc))
            typer.echo(f"Checkpoint failed for seat {entry.seat_index}: {exc}", err=True)

    for round_name in ("R0", "R1", "R2"):
        cached = (resume or {}).get(round_name) or None
        reused = sum(1 for m in config.models if cached and m.openrouter_id in cached)
        if reused:
            typer.echo(f"{round_name}: reusing {reused}/{len(config.models)} checkpointed seats.")
        if round_name == "R0":
            typer.echo(f"Running R0 across {len(config.models)} models...")
            entries = await run_r0(
                proposal, system_message, config, mock, mock_call_counts,
                persist, cached=cached,
            )
        else:
            typer.echo(f"Running {round_name} deliberation round...")
            entries = await run_deliberation_round(
                round_name=round_name,
                previous_entries=entries,
                proposal=proposal,
                system_message=system_message,
                deliberation_template=deliberation_template,
                config=config,
                mock=mock,
                mock_call_counts=mock_call_counts,
                earlier_entries=[entry for previous in stages.values() for entry in previous],
                on_complete=persist,
                cached=cached,
            )
        stages[round_name] = entries
        aggregations[round_name] = aggregate(entries)
        if aggregations[round_name].aggregation_status == "failed":
            break
    return stages, aggregations


@app.command("run")
def run(
    constitution: Annotated[Path, typer.Option("--constitution", help="Path to constitution text file.")],
    system_prompt: Annotated[Path, typer.Option("--system-prompt", help="Path to reviewer system prompt; must carry the deliberation rules (see inputs/prompts/v0.1.txt).")],
    deliberation_template: Annotated[Path, typer.Option("--deliberation-prompt-template", help="Path to deliberation prompt template.")],
    proposal: Annotated[Path, typer.Option("--proposal", help="Path to proposal JSON file.")],
    config_path: Annotated[Path, typer.Option("--config", help="Path to YAML config file.")],
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir", help="Override output directory from config.")] = None,
    mock: Annotated[bool, typer.Option("--mock", help="Offline mock mode; no API calls.")] = False,
    debug: Annotated[bool, typer.Option("--debug", help="Write full API request/response records to a .debug.jsonl file next to the checkpoint.")] = False,
    resume_from: Annotated[Optional[Path], typer.Option("--resume-from", help="Events checkpoint (.events.jsonl) of a failed run; seats with valid output are reused instead of re-called.")] = None,
) -> None:
    for path, label in [
        (constitution, "constitution"),
        (system_prompt, "system-prompt"),
        (deliberation_template, "deliberation-prompt-template"),
        (proposal, "proposal"),
        (config_path, "config"),
    ]:
        if not path.exists():
            typer.echo(f"Error: {label} file not found: {path}", err=True)
            raise typer.Exit(code=1)

    config = load_config(config_path)
    if not mock:
        required_keys = {
            {"openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[m.provider]
            for m in config.models
        }
        missing = sorted(key for key in required_keys if not os.environ.get(key))
        if missing:
            typer.echo(f"Error: environment variable(s) not set: {', '.join(missing)}", err=True)
            raise typer.Exit(code=1)
    if output_dir is not None:
        config.output_dir = str(output_dir)

    constitution_text = constitution.read_text()
    system_prompt_text = system_prompt.read_text()
    deliberation_template_text = deliberation_template.read_text()
    proposal_bytes = proposal.read_bytes()
    proposal_data = json.loads(proposal_bytes)

    if not isinstance(proposal_data, dict) or not isinstance(proposal_data.get("id", "unknown"), str):
        typer.echo("Error: proposal must be a JSON object with a string id when supplied.", err=True)
        raise typer.Exit(code=1)

    _validate_template(deliberation_template_text)
    _omit_duplicate_constitution(proposal_data, constitution_text)

    system_message = _build_system_message(system_prompt_text, constitution_text)
    if config.ncl is not None:
        system_message += (
            "\n\nConfigured NCL (operator-supplied allowance for this run, in lovelace):\n"
            + config.ncl.model_dump_json()
            + "\nThe operator attests that this allowance is the Net Change Limit currently "
            "in force: it was agreed by DReps through an on-chain governance action with "
            "the support required by the constitution (Article II, Section 7(3) and "
            "guardrail TREASURY-01a). Treat the existence and DRep approval of the NCL as "
            "established for this review; do not mark it as missing information solely "
            "because the snapshot omits that action's voting record. "
            "Compare the proposal's total requested withdrawal with this supplied allowance. "
            "Period selection and cumulative spending accounting are outside v0.1; "
            "do not invent a period or subtract historical spending from this value. "
            "Passing this amount check does not establish overall constitutional compliance."
        )

    fingerprint = ConfigFingerprint(
        proposal_sha256=hashlib.sha256(proposal_bytes).hexdigest(),
        constitution_sha256=sha256_file(constitution),
        system_prompt_sha256=sha256_file(system_prompt),
        deliberation_template_sha256=sha256_file(deliberation_template),
        system_message_sha256=hashlib.sha256(system_message.encode("utf-8")).hexdigest(),
        ncl=config.ncl.model_dump() if config.ncl else None,
        models=[
            {"provider": m.provider, "openrouter_id": m.openrouter_id, "decoding_params": m.decoding_params}
            for m in config.models
        ],
        ballot_categories=config.ballot_categories,
        aggregation_rule="strict_majority_all_seats_else_abstain",
        rounds=2,
    )

    resume = None
    if resume_from is not None:
        if not resume_from.exists():
            typer.echo(f"Error: resume-from file not found: {resume_from}", err=True)
            raise typer.Exit(code=1)
        resume, resume_fingerprint = _load_resume_entries(resume_from)
        if resume_fingerprint is None:
            typer.echo(
                "Warning: checkpoint has no config fingerprint; reuse assumes the "
                "prompts, constitution, proposal, and protocol are unchanged.",
                err=True,
            )
        else:
            current = fingerprint.model_dump(mode="json")
            # Decoding params may be retuned between attempts; everything else
            # defines the deliberation the checkpointed seats took part in.
            mismatched = [
                key for key in current
                if key != "models" and resume_fingerprint.get(key) != current[key]
            ]
            if mismatched:
                typer.echo(
                    "Error: cannot resume — inputs changed since the checkpointed run: "
                    + ", ".join(sorted(mismatched)),
                    err=True,
                )
                raise typer.Exit(code=1)

    started_at = utc_now()
    checkpoint_path = Path(config.output_dir) / (output_filename(proposal_data.get("id", "unknown"), started_at) + ".events.jsonl")
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with checkpoint_path.open("a") as stream:
        stream.write(json.dumps({"config_fingerprint": fingerprint.model_dump(mode="json")},
                                separators=(",", ":")) + "\n")
    if debug:
        from .client import set_debug_log

        debug_path = checkpoint_path.with_name(
            checkpoint_path.name.removesuffix(".events.jsonl") + ".debug.jsonl"
        )
        set_debug_log(debug_path)
        typer.echo(f"Debug log: {debug_path}")
    stages, aggregations = asyncio.run(
        _run_stages(
            proposal=proposal_data,
            system_message=system_message,
            deliberation_template=deliberation_template_text,
            config=config,
            mock=mock,
            checkpoint_path=checkpoint_path,
            resume=resume,
        )
    )
    failed_stage = next(
        (name for name, agg in aggregations.items() if agg.aggregation_status == "failed"), None
    )
    run_status = "failed" if failed_stage else "complete"
    canonical = CanonicalFile(
        pitkind_version=__version__,
        schema_version="2.0",
        proposal=proposal_data,
        config_fingerprint=fingerprint,
        timestamps={
            "started_at": started_at.isoformat(),
            "completed_at": utc_now().isoformat(),
        },
        run_status=run_status,
        stages=stages,
        aggregations=aggregations,
        official_verdict=aggregations["R2"].verdict if run_status == "complete" else None,
        summary=outcome_summary(stages.get("R2", []), aggregations["R2"].verdict) if run_status == "complete" else None,
        review_arguments=argument_catalog([entry for entries in stages.values() for entry in entries]),
    )
    out_path = Path(config.output_dir) / output_filename(proposal_data.get("id", "unknown"), started_at)
    try:
        atomic_write_json(out_path, canonical.model_dump(mode="json"))
    except OSError as exc:
        typer.echo(f"Error: could not save canonical output: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Written: {out_path}")
    typer.echo(f"Checkpoint: {checkpoint_path}")
    if run_status == "complete":
        final = aggregations["R2"]
        typer.echo(f"Committee ballot: {final.verdict} (agreement is not calibrated confidence).")
        if final.review_flags:
            typer.echo("Deliberation flags: " + ", ".join(final.review_flags))

    usages = [e.usage for entries in stages.values() for e in entries if e.usage is not None]
    if usages:
        calls = sum(u.api_calls for u in usages)
        pt = sum(u.prompt_tokens for u in usages)
        ct = sum(u.completion_tokens for u in usages)
        costs = [u.cost_usd for u in usages if u.cost_usd is not None]
        cost_str = f"${sum(costs):.4f}" if costs else "n/a"
        typer.echo(f"Cost: {cost_str} ({calls} API calls, {pt:,} prompt + {ct:,} completion tokens)")

    if run_status == "failed":
        typer.echo(
            f"Error: run failed — model or persistence failure at {failed_stage}; official_verdict is null.",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command("report")
def report(
    output_file: Annotated[Path, typer.Argument(help="Canonical output JSON file to render.")],
    out: Annotated[Optional[Path], typer.Option("--out", "-o", help="Destination HTML file (default: alongside the JSON).")] = None,
    metadata: Annotated[bool, typer.Option("--metadata", help="Also write CIP-136 JSON-LD vote metadata.")] = False,
) -> None:
    """Render a canonical output file as a self-contained HTML deliberation report."""
    from .report import render_report

    if not output_file.exists():
        typer.echo(f"Error: file not found: {output_file}", err=True)
        raise typer.Exit(code=1)

    data = json.loads(output_file.read_text())
    CanonicalFile.model_validate(data)

    html = render_report(data)
    dest = out if out is not None else output_file.with_suffix(".html")
    dest.write_text(html)
    typer.echo(f"Written: {dest}")
    if metadata:
        from .report import render_metadata

        try:
            metadata_bytes = (json.dumps(render_metadata(data), ensure_ascii=False, indent=2) + "\n").encode()
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        metadata_path = dest.with_suffix(".metadata.jsonld")
        atomic_write_bytes(metadata_path, metadata_bytes)
        typer.echo(f"Written: {metadata_path}")
        typer.echo(f"Blake2b-256: {hashlib.blake2b(metadata_bytes, digest_size=32).hexdigest()}")


@app.command("snapshot")
def snapshot(
    action_id: Annotated[str, typer.Argument(help="Governance action identifier.")],
    network: Annotated[str, typer.Option(help="mainnet, preprod, or preview.")] = "mainnet",
    env_file: Annotated[Path, typer.Option(help="Blockfrost API key file.")] = Path(".env"),
    out: Annotated[Optional[Path], typer.Option(help="New snapshot path; never overwritten.")] = None,
) -> None:
    """Download a frozen proposal and evidence snapshot."""
    from .download import main as download_main

    if not re.fullmatch(r"gov_action1[023456789acdefghjklmnpqrstuvwxyz]{20,100}", action_id):
        raise typer.BadParameter("Expected a gov_action1… identifier")
    destination = out or Path("inputs") / f"{action_id}.json"
    download_main([action_id, "--network", network, "--env-file", str(env_file),
                   "--out", str(destination)])
